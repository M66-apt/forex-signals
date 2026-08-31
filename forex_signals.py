"""
Forex Signal Engine
====================
Polls a free market-data API (Twelve Data), computes a handful of classic
technical indicators, and combines them into a rule-based BUY / SELL / HOLD
signal per currency pair. Results are written to signals.json (read by
dashboard.html) and optionally pushed to Telegram when a signal changes.

IMPORTANT — READ THIS
----------------------
This is a technical-analysis tool, not a prediction engine. Indicators are
computed from past price action; they describe momentum and trend, they do
not know the future. Nothing here is financial advice, and no rule-based
signal should be your sole reason to place a trade. Free-tier market data
is delayed / rate-limited, not tick-level "real-time" data — for anything
you'd actually trade on, use your broker's own live feed.

Setup
-----
1. pip install -r requirements.txt
2. Get a free API key: https://twelvedata.com/pricing (free tier is enough
   to run a handful of pairs on a multi-minute interval).
3. (Optional, for alerts) Create a Telegram bot via @BotFather, then get
   your chat id by messaging the bot and visiting:
   https://api.telegram.org/bot<TOKEN>/getUpdates
4. Copy .env.example to .env and fill in your keys, or export the same
   variables in your shell.
5. Run:  python forex_signals.py
"""

import os
import sys
import json
import time
import datetime
import requests
import pandas as pd

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Currency pairs to watch. Twelve Data format: "EUR/USD"
PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CHF"]

# Candle interval: 1min, 5min, 15min, 30min, 1h, 4h, 1day ...
INTERVAL = "15min"

# How many candles of history to pull each time (need enough for EMA21/MACD)
OUTPUT_SIZE = 100

# How often to poll, in seconds. Keep this generous on the free tier —
# Twelve Data's free plan allows 8 requests/minute and 800/day.
# 5 pairs every 300s = 60 requests/hour, well inside the free quota.
POLL_SECONDS = 300

SIGNALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.json")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_signals.json")

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"


# --------------------------------------------------------------------------
# Data fetching
# --------------------------------------------------------------------------

def fetch_candles(pair: str, interval: str = INTERVAL, outputsize: int = OUTPUT_SIZE) -> pd.DataFrame:
    """Fetch OHLC candles for one pair from Twelve Data."""
    params = {
        "symbol": pair,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
        "format": "JSON",
    }
    resp = requests.get(TWELVE_DATA_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if "values" not in data:
        raise RuntimeError(f"API error for {pair}: {data.get('message', data)}")

    df = pd.DataFrame(data["values"])
    df = df.rename(columns={"datetime": "time"})
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# Indicators
# --------------------------------------------------------------------------

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger_bands(series: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema9"] = ema(df["close"], 9)
    df["ema21"] = ema(df["close"], 21)
    df["rsi14"] = rsi(df["close"], 14)
    df["macd"], df["macd_signal"], df["macd_hist"] = macd(df["close"])
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = bollinger_bands(df["close"])
    return df


# --------------------------------------------------------------------------
# Signal generation (simple rule-based scoring, -4 .. +4)
# --------------------------------------------------------------------------

def generate_signal(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0
    reasons = []

    # 1) EMA trend / crossover
    if last["ema9"] > last["ema21"]:
        score += 1
        if prev["ema9"] <= prev["ema21"]:
            score += 1
            reasons.append("EMA9 เพิ่งตัดขึ้นเหนือ EMA21 (golden cross ระยะสั้น)")
        else:
            reasons.append("EMA9 อยู่เหนือ EMA21 (เทรนด์ระยะสั้นเป็นขาขึ้น)")
    else:
        score -= 1
        if prev["ema9"] >= prev["ema21"]:
            score -= 1
            reasons.append("EMA9 เพิ่งตัดลงใต้ EMA21 (death cross ระยะสั้น)")
        else:
            reasons.append("EMA9 อยู่ใต้ EMA21 (เทรนด์ระยะสั้นเป็นขาลง)")

    # 2) RSI
    if last["rsi14"] < 30:
        score += 1
        reasons.append(f"RSI {last['rsi14']:.1f} เข้าเขต oversold")
    elif last["rsi14"] > 70:
        score -= 1
        reasons.append(f"RSI {last['rsi14']:.1f} เข้าเขต overbought")

    # 3) MACD
    if last["macd_hist"] > 0 and prev["macd_hist"] <= 0:
        score += 1
        reasons.append("MACD histogram เพิ่งกลับเป็นบวก")
    elif last["macd_hist"] < 0 and prev["macd_hist"] >= 0:
        score -= 1
        reasons.append("MACD histogram เพิ่งกลับเป็นลบ")

    # 4) Bollinger position
    if last["close"] <= last["bb_lower"]:
        score += 1
        reasons.append("ราคาแตะ/หลุดกรอบล่าง Bollinger Band")
    elif last["close"] >= last["bb_upper"]:
        score -= 1
        reasons.append("ราคาแตะ/หลุดกรอบบน Bollinger Band")

    if score >= 2:
        signal = "BUY"
    elif score <= -2:
        signal = "SELL"
    else:
        signal = "HOLD"

    confidence = min(abs(score) / 4.0, 1.0)

    return {
        "signal": signal,
        "score": score,
        "confidence": round(confidence, 2),
        "reasons": reasons,
        "price": round(float(last["close"]), 5),
        "rsi14": round(float(last["rsi14"]), 2) if pd.notna(last["rsi14"]) else None,
        "ema9": round(float(last["ema9"]), 5),
        "ema21": round(float(last["ema21"]), 5),
        "macd_hist": round(float(last["macd_hist"]), 6) if pd.notna(last["macd_hist"]) else None,
        "time": last["time"].isoformat(),
    }


# --------------------------------------------------------------------------
# Telegram alerts
# --------------------------------------------------------------------------

def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except requests.RequestException as e:
        print(f"[warn] Telegram alert failed: {e}")


def load_last_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_last_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def run_once() -> dict:
    if not TWELVE_DATA_API_KEY:
        print("[error] TWELVE_DATA_API_KEY is not set. See README.md for setup.")
        sys.exit(1)

    last_state = load_last_state()
    all_signals = {}

    for pair in PAIRS:
        try:
            df = fetch_candles(pair)
            df = compute_indicators(df)
            result = generate_signal(df)
            all_signals[pair] = result
            print(f"{pair:10s} {result['signal']:5s} conf={result['confidence']:.2f} price={result['price']}")

            prev_signal = last_state.get(pair, {}).get("signal")
            if prev_signal is not None and prev_signal != result["signal"]:
                msg = (
                    f"⚡ {pair} signal changed: {prev_signal} → {result['signal']}\n"
                    f"Price: {result['price']}  |  Confidence: {result['confidence']:.0%}\n"
                    f"Reasons:\n- " + "\n- ".join(result["reasons"])
                )
                send_telegram_alert(msg)

        except Exception as e:
            print(f"[warn] {pair}: {e}")
            if pair in last_state:
                all_signals[pair] = last_state[pair]

    output = {
        "generated_at": datetime.datetime.now().isoformat(),
        "interval": INTERVAL,
        "disclaimer": "ผลลัพธ์จากอินดิเคเตอร์ทางเทคนิคเท่านั้น ไม่ใช่คำแนะนำการลงทุนและไม่รับประกันความแม่นยำ",
        "signals": all_signals,
    }

    with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    save_last_state(all_signals)
    return output


def main():
    """
    Two modes:
    - `python forex_signals.py`          -> loop forever, polling every POLL_SECONDS
                                             (for running on your own always-on machine/server)
    - `python forex_signals.py --once`   -> run a single fetch/compute/save cycle and exit
                                             (used by the GitHub Actions cron workflow, which
                                             provides its own scheduling — see
                                             .github/workflows/forex-signals.yml)
    """
    once = "--once" in sys.argv
    print(f"Forex signal engine starting — pairs={PAIRS} interval={INTERVAL} mode={'once' if once else f'loop every {POLL_SECONDS}s'}")

    if once:
        run_once()
        return

    while True:
        run_once()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

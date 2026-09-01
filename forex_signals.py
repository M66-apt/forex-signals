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

from indicators import compute_indicators, compute_ml_features, FEATURE_COLUMNS

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Currency pairs to watch. Twelve Data format: "EUR/USD"
# 14 pairs = majors + the more liquid crosses. See the quota note below
# before adding a lot more — each pair costs one API credit per run.
PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD",
    "EUR/GBP", "EUR/JPY", "GBP/JPY", "AUD/JPY", "EUR/AUD", "GBP/AUD", "CHF/JPY",
]

# Candle interval: 1min, 5min, 15min, 30min, 1h, 4h, 1day ...
INTERVAL = "15min"

# How many candles of history to pull each time (need enough for EMA21/MACD/ADX)
OUTPUT_SIZE = 100

# How many recent candles to publish in signals.json for charting on the dashboard
CHART_CANDLES = 80

# How often to poll, in seconds — only used when running the loop locally
# (`python forex_signals.py` without --once). When run via GitHub Actions
# the schedule in .github/workflows/forex-signals.yml controls the cadence
# instead, since each cron run is a fresh, short-lived process.
POLL_SECONDS = 300

# --- Free-tier quota math (Twelve Data free plan: 8 req/min, 800 req/day) ---
# requests per run = len(PAIRS)
# runs per day      = (24*60) / cron_minutes
# requests per day   = requests per run * runs per day  -> must stay <= 800
#
# 14 pairs every 15 min = 14 * 96 = 1,344 req/day  -> OVER the 800/day free cap.
# Pick ONE of these before going live:
#   - keep 14 pairs, change the cron to every 30 min  -> 14 * 48 = 672/day  (OK)
#   - keep 15 min cadence, trim PAIRS to ~8            -> 8 * 96 = 768/day  (OK)
#   - upgrade the Twelve Data plan if you want both.
# The cron line lives in .github/workflows/forex-signals.yml.

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
# ML model (optional) — only used if ml/train_model.py has been run and
# committed a trained model. Fails gracefully to "not available" otherwise.
# --------------------------------------------------------------------------

ML_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ml")
MODEL_PATH = os.path.join(ML_DIR, "model.pkl")
META_PATH = os.path.join(ML_DIR, "model_meta.json")

ML_MODEL = None
ML_META = None
try:
    import joblib
    if os.path.exists(MODEL_PATH) and os.path.exists(META_PATH):
        ML_MODEL = joblib.load(MODEL_PATH)
        with open(META_PATH, "r", encoding="utf-8") as f:
            ML_META = json.load(f)
        print(f"[info] Loaded ML model trained {ML_META.get('trained_at')}, "
              f"test accuracy {ML_META.get('test_accuracy')} (baseline {ML_META.get('test_baseline_accuracy')})")
except ImportError:
    pass  # scikit-learn/joblib not installed — ML features simply stay unavailable
except Exception as e:
    print(f"[warn] Could not load ML model: {e}")


def ml_predict(df: pd.DataFrame) -> dict:
    """Returns the model's probability that price will be higher N candles
    from now, based on the same feature set it was trained on. Returns
    {"available": False} if no model has been trained yet."""
    if ML_MODEL is None or ML_META is None:
        return {"available": False}
    try:
        row = df.iloc[-1]
        feat_cols = ML_META["feature_columns"]
        feats = pd.DataFrame([[row[c] for c in feat_cols]], columns=feat_cols)
        proba_up = float(ML_MODEL.predict_proba(feats)[0][1])
        return {
            "available": True,
            "probability_up": round(proba_up, 3),
            "horizon_candles": ML_META.get("horizon_candles"),
            "trained_at": ML_META.get("trained_at"),
            "test_accuracy": ML_META.get("test_accuracy"),
            "test_baseline_accuracy": ML_META.get("test_baseline_accuracy"),
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


# --------------------------------------------------------------------------
# Signal generation (rule-based scoring)
# --------------------------------------------------------------------------
#
# NOTE ON "ACCURACY": adding indicators does not make this predict the market
# better — no rule-based or AI system can reliably do that. What the ADX
# filter and Stochastic confirmation below actually do is reduce *false*
# signals: an EMA crossover in a flat, choppy market (low ADX) is mostly
# noise, so its weight is halved instead of counted at full strength. That
# improves signal *quality/consistency*, not predictive power. Always
# backtest before trusting any of this with real money. The ML block below
# is a genuinely separate, data-driven opinion — see ml/train_model.py and
# README.md for what its accuracy numbers actually mean (and don't mean).

def generate_signal(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0.0
    reasons = []

    trending = pd.notna(last["adx14"]) and last["adx14"] >= 20
    trend_weight = 1.0 if trending else 0.5

    # 1) EMA trend / crossover — weighted down in a non-trending (low ADX) market
    if last["ema9"] > last["ema21"]:
        score += 1 * trend_weight
        if prev["ema9"] <= prev["ema21"]:
            score += 1 * trend_weight
            reasons.append("EMA9 เพิ่งตัดขึ้นเหนือ EMA21ᐧ" + ("" if trending else " (ตลาดไม่ได้เป็นเทรนด์ชัด น้ำหนักลดลง)"))
        else:
            reasons.append("EMA9 อยู่เหนือ EMA21 (เทรนด์ระยะสั้นเป็นขาขึ้น)")
    else:
        score -= 1 * trend_weight
        if prev["ema9"] >= prev["ema21"]:
            score -= 1 * trend_weight
            reasons.append("EMA9 เพิ่งตัดลงใต้ EMA21" + ("" if trending else " (ตลาดไม่ได้เป็นเทรนด์ชัด น้ำหนักลดลง)"))
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

    # 5) Stochastic — second momentum confirmation, independent of RSI's math
    if pd.notna(last["stoch_k"]) and pd.notna(last["stoch_d"]):
        if last["stoch_k"] < 20 and last["stoch_k"] > last["stoch_d"] and prev["stoch_k"] <= prev["stoch_d"]:
            score += 1
            reasons.append("Stochastic ตัดขึ้นในเขต oversold")
        elif last["stoch_k"] > 80 and last["stoch_k"] < last["stoch_d"] and prev["stoch_k"] >= prev["stoch_d"]:
            score -= 1
            reasons.append("Stochastic ตัดลงในเขต overbought")

    if score >= 2.5:
        signal = "BUY"
    elif score <= -2.5:
        signal = "SELL"
    else:
        signal = "HOLD"

    confidence = min(abs(score) / 5.0, 1.0)

    ml = ml_predict(df)

    return {
        "signal": signal,
        "score": round(score, 2),
        "confidence": round(confidence, 2),
        "trending": bool(trending),
        "adx14": round(float(last["adx14"]), 1) if pd.notna(last["adx14"]) else None,
        "reasons": reasons,
        "price": round(float(last["close"]), 5),
        "rsi14": round(float(last["rsi14"]), 2) if pd.notna(last["rsi14"]) else None,
        "stoch_k": round(float(last["stoch_k"]), 1) if pd.notna(last["stoch_k"]) else None,
        "ema9": round(float(last["ema9"]), 5),
        "ema21": round(float(last["ema21"]), 5),
        "macd_hist": round(float(last["macd_hist"]), 6) if pd.notna(last["macd_hist"]) else None,
        "time": last["time"].isoformat(),
        "ml": ml,
        "candles": [
            {
                "time": int(row["time"].timestamp()),
                "open": round(float(row["open"]), 5),
                "high": round(float(row["high"]), 5),
                "low": round(float(row["low"]), 5),
                "close": round(float(row["close"]), 5),
            }
            for _, row in df.tail(CHART_CANDLES).iterrows()
        ],
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

# Twelve Data free plan allows 8 requests/minute. Spacing requests 8 seconds
# apart keeps us safely under that (7.5s would be the exact limit) even
# accounting for network latency, so all 14 pairs succeed instead of the
# 9th-14th getting rate-limited and silently dropped.
REQUEST_SPACING_SECONDS = 8


def run_once() -> dict:
    if not TWELVE_DATA_API_KEY:
        print("[error] TWELVE_DATA_API_KEY is not set. See README.md for setup.")
        sys.exit(1)

    last_state = load_last_state()
    all_signals = {}

    for i, pair in enumerate(PAIRS):
        if i > 0:
            time.sleep(REQUEST_SPACING_SECONDS)
        try:
            df = fetch_candles(pair)
            df = compute_indicators(df)
            df = compute_ml_features(df)
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
                print(f"[info] {pair}: keeping previous signal from last successful run")

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

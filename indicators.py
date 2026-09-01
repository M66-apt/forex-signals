"""
Shared technical-indicator and feature-engineering functions.

Both forex_signals.py (live inference) and ml/train_model.py (training)
import from here. That's deliberate: if the two computed features
differently, the trained model would silently see different numbers at
inference time than it saw during training — a classic, hard-to-notice bug
in ML pipelines. Keeping one shared implementation avoids that.
"""

import pandas as pd

# --------------------------------------------------------------------------
# Base indicators
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


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, 1e-10)
    d = k.rolling(d_period).mean()
    return k, d


def true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    return pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — a volatility measure in price units. Used both
    as a trend-strength ingredient (ADX) and to normalize other features
    (e.g. MACD histogram) so they're comparable across currency pairs that
    have very different pip scales (e.g. USD/JPY vs EUR/USD)."""
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14, atr_series: pd.Series = None):
    """Average Directional Index — measures trend STRENGTH (not direction).
    Used as a filter: EMA-crossover signals are less trustworthy in a flat,
    range-bound market (low ADX) than in a genuinely trending one (high ADX)."""
    high, low = df["high"], df["low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move

    atr_series = atr_series if atr_series is not None else atr(df, period)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_series.replace(0, 1e-10)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_series.replace(0, 1e-10)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-10)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Adds all base indicator columns used by the rule-based signal engine."""
    df = df.copy()
    df["ema9"] = ema(df["close"], 9)
    df["ema21"] = ema(df["close"], 21)
    df["rsi14"] = rsi(df["close"], 14)
    df["macd"], df["macd_signal"], df["macd_hist"] = macd(df["close"])
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = bollinger_bands(df["close"])
    df["stoch_k"], df["stoch_d"] = stochastic(df)
    df["atr14"] = atr(df, 14)
    df["adx14"] = adx(df, 14, atr_series=df["atr14"])
    return df


# --------------------------------------------------------------------------
# ML feature engineering
# --------------------------------------------------------------------------
# These are deliberately built to be comparable ACROSS currency pairs (scale
# -invariant), because the model is trained on pooled data from all pairs
# at once rather than one model per pair — that gives it far more training
# rows to learn from given the limited history a free API key can provide.

FEATURE_COLUMNS = [
    "rsi14", "stoch_k", "stoch_d", "adx14",
    "macd_hist_norm", "ema_spread_norm", "bb_position",
    "roc_1", "roc_4", "roc_8", "atr14_pct",
]


def compute_ml_features(df: pd.DataFrame) -> pd.DataFrame:
    """Requires compute_indicators() to have already been run on df."""
    df = df.copy()
    safe_atr = df["atr14"].replace(0, 1e-10)
    df["macd_hist_norm"] = df["macd_hist"] / safe_atr
    df["ema_spread_norm"] = (df["ema9"] - df["ema21"]) / safe_atr
    band_width = (df["bb_upper"] - df["bb_lower"]).replace(0, 1e-10)
    df["bb_position"] = (df["close"] - df["bb_mid"]) / band_width
    df["roc_1"] = df["close"].pct_change(1) * 100
    df["roc_4"] = df["close"].pct_change(4) * 100
    df["roc_8"] = df["close"].pct_change(8) * 100
    df["atr14_pct"] = df["atr14"] / df["close"] * 100
    return df

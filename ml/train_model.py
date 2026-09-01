"""
Forex ML Model Trainer
=======================
This trains a real, data-driven model — NOT a set of hand-picked rules —
to estimate the probability that a currency pair's price will be higher
N candles from now, based on the technical features in indicators.py.

READ THIS BEFORE TRUSTING ANY NUMBER THIS SCRIPT PRINTS
---------------------------------------------------------
1. Short-term FX direction is close to a coin flip. A model that's right
   53-56% of the time out-of-sample is a genuinely meaningful result in
   this domain — it is NOT the same as "56% accurate trading system" once
   you account for spread, slippage, and execution timing. Do not treat
   the printed accuracy as expected win rate.
2. The free API tier only gives you a limited amount of history per pair.
   This script caches and accumulates data across runs (see DATA_DIR) so
   the dataset grows every time you re-run it — but a model trained on a
   few weeks/months of data has only seen a handful of market "regimes."
   It WILL eventually meet conditions it hasn't seen before.
3. This script reports genuine out-of-sample metrics (it trains on the
   older 80% of each pair's history and tests on the newer, unseen 20% —
   never the other way around). That protects against the most common
   beginner mistake (testing on data the model already memorized), but it
   does NOT protect against regime change, and it does NOT account for
   trading costs.
4. Re-run this periodically (weekly/monthly) via the "Train ML model"
   GitHub Actions workflow to both grow the dataset and refresh the model.
   A model trained once and never updated goes stale.

Usage:  python ml/train_model.py
"""

import os
import sys
import json
import time
import datetime

import pandas as pd
import numpy as np
import joblib
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, roc_auc_score, classification_report

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from forex_signals import PAIRS, INTERVAL, TWELVE_DATA_API_KEY, fetch_candles  # noqa: E402
from indicators import compute_indicators, compute_ml_features, FEATURE_COLUMNS  # noqa: E402

ML_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ML_DIR, "data")
MODEL_PATH = os.path.join(ML_DIR, "model.pkl")
META_PATH = os.path.join(ML_DIR, "model_meta.json")

# Max candles to request per pair per run. Twelve Data's free plan generally
# allows up to 5000 per time_series call. At the live INTERVAL (15min) that's
# roughly 52 calendar days in a single request; older history accumulates
# across repeated runs via the on-disk cache below.
TRAIN_OUTPUT_SIZE = 5000
REQUEST_SPACING_SECONDS = 8  # same free-tier rate limit as forex_signals.py

# How many candles ahead we're predicting the direction of.
# 4 candles at 15min interval = 1 hour ahead.
HORIZON_CANDLES = 4

# Fraction of each pair's history (chronologically) used for training;
# the remaining, more recent fraction is held out for honest evaluation.
TRAIN_FRACTION = 0.8


def pair_filename(pair: str) -> str:
    return os.path.join(DATA_DIR, pair.replace("/", "-") + ".csv")


def load_and_merge_history(pair: str) -> pd.DataFrame:
    """Fetches fresh candles and merges them with whatever's already cached
    on disk from previous runs, so the dataset grows over time instead of
    being limited to a single API call's window."""
    fresh = fetch_candles(pair, interval=INTERVAL, outputsize=TRAIN_OUTPUT_SIZE)

    path = pair_filename(pair)
    if os.path.exists(path):
        cached = pd.read_csv(path, parse_dates=["time"])
        combined = pd.concat([cached, fresh], ignore_index=True)
    else:
        combined = fresh

    combined = combined.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    combined.to_csv(path, index=False)
    return combined


def build_labeled_features(df: pd.DataFrame) -> pd.DataFrame:
    df = compute_indicators(df)
    df = compute_ml_features(df)
    df["future_close"] = df["close"].shift(-HORIZON_CANDLES)
    df["label_up"] = (df["future_close"] > df["close"]).astype(int)
    # Drop rows where we don't have full indicator warmup or a known future outcome
    df = df.dropna(subset=FEATURE_COLUMNS + ["future_close"])
    return df


def main():
    if not TWELVE_DATA_API_KEY:
        print("[error] TWELVE_DATA_API_KEY is not set.")
        sys.exit(1)

    print(f"Fetching/merging history for {len(PAIRS)} pairs (interval={INTERVAL})...")
    train_frames, test_frames = [], []
    data_range = {}

    for i, pair in enumerate(PAIRS):
        if i > 0:
            time.sleep(REQUEST_SPACING_SECONDS)
        try:
            raw = load_and_merge_history(pair)
            labeled = build_labeled_features(raw)
            if len(labeled) < 200:
                print(f"[warn] {pair}: only {len(labeled)} usable rows after warmup — skipping (need more history, re-run later)")
                continue

            split_idx = int(len(labeled) * TRAIN_FRACTION)
            train_frames.append(labeled.iloc[:split_idx])
            test_frames.append(labeled.iloc[split_idx:])
            data_range[pair] = {
                "from": raw["time"].min().isoformat(),
                "to": raw["time"].max().isoformat(),
                "n_candles_cached": len(raw),
                "n_usable_rows": len(labeled),
            }
            print(f"{pair:10s} cached candles={len(raw):5d}  usable rows={len(labeled):5d}")
        except Exception as e:
            print(f"[warn] {pair}: {e}")

    if not train_frames:
        print("[error] No usable data for any pair. Try again after more candles have accumulated.")
        sys.exit(1)

    train_df = pd.concat(train_frames, ignore_index=True)
    test_df = pd.concat(test_frames, ignore_index=True)

    X_train, y_train = train_df[FEATURE_COLUMNS], train_df["label_up"]
    X_test, y_test = test_df[FEATURE_COLUMNS], test_df["label_up"]

    print(f"\nTraining on {len(X_train)} rows, testing on {len(X_test)} rows (chronological split per pair)")
    print(f"Train label balance: {y_train.mean():.3f} up / {1 - y_train.mean():.3f} down")
    print(f"Test  label balance: {y_test.mean():.3f} up / {1 - y_test.mean():.3f} down")

    model = HistGradientBoostingClassifier(max_depth=6, learning_rate=0.05, random_state=42)
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]
    accuracy = accuracy_score(y_test, y_pred)
    baseline = max(y_test.mean(), 1 - y_test.mean())  # accuracy of always guessing the majority class
    try:
        auc = roc_auc_score(y_test, y_proba)
    except ValueError:
        auc = None  # only one class present in test set

    print("\n" + "=" * 60)
    print("OUT-OF-SAMPLE EVALUATION (never seen during training)")
    print("=" * 60)
    print(f"Accuracy:            {accuracy:.4f}")
    print(f"Naive baseline:      {baseline:.4f}  (always guessing the majority class)")
    print(f"ROC-AUC:             {auc:.4f}" if auc is not None else "ROC-AUC:             n/a")
    print("\n" + classification_report(y_test, y_pred, target_names=["down", "up"]))
    print("=" * 60)
    if accuracy <= baseline:
        print("[note] Model did NOT beat the naive baseline on this run's held-out data.")
        print("       That happens, especially early on with limited history — it's still")
        print("       being saved so the app can show it, but weight it accordingly.")

    os.makedirs(ML_DIR, exist_ok=True)
    joblib.dump(model, MODEL_PATH)

    meta = {
        "trained_at": datetime.datetime.now().isoformat(),
        "feature_columns": FEATURE_COLUMNS,
        "horizon_candles": HORIZON_CANDLES,
        "interval": INTERVAL,
        "pairs": list(data_range.keys()),
        "n_train": len(X_train),
        "n_test": len(X_test),
        "test_accuracy": round(float(accuracy), 4),
        "test_baseline_accuracy": round(float(baseline), 4),
        "test_roc_auc": round(float(auc), 4) if auc is not None else None,
        "data_range": data_range,
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"\nSaved model -> {MODEL_PATH}")
    print(f"Saved metadata -> {META_PATH}")


if __name__ == "__main__":
    main()

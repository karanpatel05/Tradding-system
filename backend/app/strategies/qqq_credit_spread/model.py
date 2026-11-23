"""The 15:30 checkpoint model: the external runner's own artifact, loaded as it does.

``<model_dir>/QQQ/model.joblib`` is a scikit-learn Pipeline (median imputer, scaler,
logistic regression) and ``model_metadata.json`` carries its feature list and the
close threshold. The weekly retrain swaps the ``current`` symlink, so the newest
deployed model is read at each checkpoint. Refused (never guessed): a missing or
foreign-ticker artifact, a feature list that differs from ``logic.FEATURES``, or one
older than ``max_age_days``.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from app.strategies.qqq_credit_spread.logic import FEATURES, TICKER


class ModelUnavailable(Exception):
    pass


def model_paths(model_dir: str | Path, ticker: str = TICKER) -> tuple[Path, Path]:
    d = Path(model_dir).expanduser() / ticker
    return d / "model_metadata.json", d / "model.joblib"


def check_metadata(model_dir: str | Path, max_age_days: int, ticker: str = TICKER,
                   now: datetime | None = None) -> dict:
    """The artifact's metadata if it is usable, else ModelUnavailable (no unpickling)."""
    meta_path, model_path = model_paths(model_dir, ticker)
    if not meta_path.exists() or not model_path.exists():
        raise ModelUnavailable(f"no model artifact in {meta_path.parent}")
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError) as exc:
        raise ModelUnavailable(f"unreadable model metadata: {exc}") from exc
    if meta.get("ticker") != ticker:
        raise ModelUnavailable(f"model is for {meta.get('ticker')!r}, not {ticker}")
    if list(meta.get("features") or []) != list(FEATURES):
        raise ModelUnavailable("model feature list does not match the strategy's features")
    try:
        trained = datetime.fromisoformat(str(meta["trained_at_utc"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise ModelUnavailable("model metadata has no valid trained_at_utc") from exc
    if trained.tzinfo is None:
        trained = trained.replace(tzinfo=timezone.utc)
    age = ((now or datetime.now(timezone.utc)) - trained).days
    if age > max_age_days:
        raise ModelUnavailable(f"model is {age} days old, over the {max_age_days}-day limit")
    try:
        threshold = float(meta.get("threshold", 0.99))
    except (TypeError, ValueError) as exc:
        raise ModelUnavailable("model threshold is not a number") from exc
    if not 0.0 < threshold < 1.0:
        raise ModelUnavailable(f"model threshold {threshold} is outside (0, 1)")
    return meta


def load_model(model_dir: str | Path, max_age_days: int, ticker: str = TICKER):
    """(pipeline, meta). Imports scikit-learn lazily so the app boots without it."""
    meta = check_metadata(model_dir, max_age_days, ticker)
    try:
        import joblib
    except ImportError as exc:
        raise ModelUnavailable("scikit-learn/joblib is not installed in the app venv") from exc
    try:
        pipeline = joblib.load(model_paths(model_dir, ticker)[1])
    except Exception as exc:  # noqa: BLE001 - any unpickling failure is "unavailable"
        raise ModelUnavailable(f"model failed to load: {type(exc).__name__}: {exc}") from exc
    return pipeline, meta


def decide_close(pipeline, meta: dict, row: dict) -> tuple[bool, float, float]:
    """(close?, probability, threshold). Close only when p is STRICTLY above it."""
    import numpy as np
    import pandas as pd
    x = pd.DataFrame([row])[FEATURES].replace([np.inf, -np.inf], np.nan)
    p = float(pipeline.predict_proba(x)[0, 1])
    if not math.isfinite(p):
        raise ModelUnavailable("model returned a non-finite probability")
    threshold = float(meta.get("threshold", 0.99))
    return p > threshold, p, threshold

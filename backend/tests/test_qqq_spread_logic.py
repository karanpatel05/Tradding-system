"""Parity of the in-app QQQ spread rules with the external runner.

``fixtures/qqq_spread_golden.json`` was produced by running the runner's OWN functions
(live_strategy.first_hour_signal / checkpoint_features / decide_close /
closing_limit_debit / spread_limit_credit, qqq_options_backtest.ladder_quantity and
iter_sessions) on synthetic sessions, under its own venv, with the deployed model."""
import json
import math
from datetime import date, datetime
from pathlib import Path

import pytest

from app.strategies.qqq_credit_spread import logic as L

GOLD = json.loads((Path(__file__).parent / "fixtures" / "qqq_spread_golden.json").read_text())


def _plan(case):
    return L.session_plan(date.fromisoformat(case["day"]))


@pytest.mark.parametrize("case", GOLD["cases"], ids=[c["day"] for c in GOLD["cases"]])
def test_session_plan_matches_runner_calendar(case):
    plan = _plan(case)
    assert plan.open == datetime.fromisoformat(case["open"])
    assert plan.close == datetime.fromisoformat(case["close"])
    assert plan.early_close == case["early_close"]
    assert plan.entry.hour == 10 and plan.entry.minute == 30


@pytest.mark.parametrize("case", GOLD["cases"], ids=[c["day"] for c in GOLD["cases"]])
def test_first_hour_signal_matches_runner(case):
    sig, reason = L.first_hour_signal(case["bars"], _plan(case))
    if case["signal"] is None:
        assert sig is None and reason == case["reason"]
        return
    assert reason is None
    got = sig.to_dict()
    for k, v in case["signal"].items():
        if isinstance(v, float):
            assert got[k] == pytest.approx(v, rel=1e-12, abs=1e-9), k
        else:
            assert got[k] == v, k


@pytest.mark.parametrize("case", [c for c in GOLD["cases"] if c.get("features")],
                         ids=[c["day"] for c in GOLD["cases"] if c.get("features")])
def test_checkpoint_features_and_model_match_runner(case):
    sig = L.Signal.from_dict(case["signal"])
    feat, reason = L.checkpoint_features(case["bars"], sig, _plan(case).checkpoint, 0.47)
    assert reason is None
    for k in L.FEATURES:
        assert feat[k] == pytest.approx(case["features"][k], rel=1e-9, abs=1e-9), k
    from app.strategies.qqq_credit_spread import model as M
    from app.config import get_settings
    try:
        pipeline, meta = M.load_model(get_settings().qqq_spread_model_dir, 10_000)
    except M.ModelUnavailable as exc:
        pytest.skip(f"deployed model not available here: {exc}")
    if meta["trained_at_utc"] != GOLD["model_trained_at"]:
        pytest.skip("deployed model changed since the fixture was generated")
    close, p, thr = M.decide_close(pipeline, meta, feat)
    assert p == pytest.approx(case["decision"]["p"], rel=1e-9, abs=1e-12)
    assert close == case["decision"]["close"] and thr == case["decision"]["threshold"]


@pytest.mark.parametrize("row", GOLD["debits"])
def test_closing_limit_debit_matches_runner(row):
    assert L.closing_limit_debit(row["quotes"], "S", "L", row["width"], row["progress"]) \
        == pytest.approx(row["debit"])


@pytest.mark.parametrize("row", GOLD["credits"])
def test_spread_limit_credit_matches_runner(row):
    got = list(L.spread_limit_credit(row["quotes"], "S", "L"))
    exp = row["credit"]
    assert got[1] == exp[1]
    assert (got[0] is None and exp[0] is None) or math.isclose(got[0], exp[0])


@pytest.mark.parametrize("row", GOLD["ladder"])
def test_ladder_matches_backtester(row):
    assert L.ladder_quantity(row["equity"], 1000., 500.) == row["q"]


def test_weekend_and_holiday_have_no_session():
    assert L.session_plan(date(2026, 11, 22)) is None     # Sunday
    assert L.session_plan(date(2026, 11, 26)) is None     # Thanksgiving


def test_grace_window():
    plan = L.session_plan(date(2026, 11, 23))
    from datetime import timedelta
    assert L.within_grace(plan.entry, plan.entry, 10)
    assert L.within_grace(plan.entry, plan.entry + timedelta(minutes=10), 10)
    assert not L.within_grace(plan.entry, plan.entry + timedelta(minutes=10, seconds=1), 10)
    assert not L.within_grace(plan.entry, plan.entry - timedelta(seconds=1), 10)


def test_model_metadata_checks(tmp_path):
    from app.strategies.qqq_credit_spread import model as M
    d = tmp_path / "QQQ"
    d.mkdir()
    (d / "model.joblib").write_bytes(b"x")
    meta = {"ticker": "QQQ", "features": L.FEATURES, "threshold": 0.9,
            "trained_at_utc": datetime.now().astimezone().isoformat()}
    (d / "model_metadata.json").write_text(json.dumps(meta))
    assert M.check_metadata(tmp_path, 120)["threshold"] == 0.9
    for bad, msg in ((dict(meta, ticker="SPY"), "not QQQ"),
                     (dict(meta, features=L.FEATURES[:-1]), "feature list"),
                     (dict(meta, trained_at_utc="2020-01-01T00:00:00+00:00"), "days old"),
                     (dict(meta, threshold=1.5), "outside")):
        (d / "model_metadata.json").write_text(json.dumps(bad))
        with pytest.raises(M.ModelUnavailable, match=msg):
            M.check_metadata(tmp_path, 120)

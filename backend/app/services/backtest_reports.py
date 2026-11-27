"""Read completed iron fly replays from the historical data cache."""

from datetime import date
from functools import lru_cache
import logging
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import get_settings

logger = logging.getLogger("trading.backtest_reports")

# Only completed replay artifacts are public. Raw option data and the equity
# backtester's strategy slots share this cache and have different meanings.
_XSP_REPORTS = {
    "alpaca-5m": ("Alpaca · 5-minute replay", "xsp_bt_alpaca/results_5m.json"),
    "tastytrade-5m": ("Tastytrade · 5-minute replay", "xsp_bt/results_5m.json"),
    "tastytrade-1m-oct27": ("Tastytrade · 1-minute comparison", "xsp_bt/results_1m_oct27.json"),
    "tastytrade-5m-oct27": ("Tastytrade · 5-minute comparison", "xsp_bt/results_5m_oct27.json"),
}
SPY_SOURCE = "Alpaca SPY SIP and eligible single-leg SPY 0DTE option trades"
_CATALOG = {
    "xsp_iron_fly": _XSP_REPORTS,
    "spy_iron_fly": {"alpaca-prints": ("Alpaca · SPY option prints", "spy_bt_alpaca/results_5m.json")},
}


class _ReplayModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class _Session(_ReplayModel):
    date: date
    status: str
    pnl: float | None
    contracts: int
    fees: float
    reason: str = ""
    data_gap: bool = False
    stops: list[str] = Field(default_factory=list)
    body: float | None = None
    net_credit: float | None = None
    settlement: float | None = None


class _Aggregate(_ReplayModel):
    sessions: int
    traded: int
    total_pnl: float | None
    win_rate: float | None
    max_drawdown: float | None
    fees: float | None
    unresolved: int = 0
    best_day: float | None = None
    worst_day: float | None = None
    mean_pnl: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    rule_skips: dict[str, str] = Field(default_factory=dict)
    data_gap_skips: dict[str, str] = Field(default_factory=dict)


class _Scenario(_ReplayModel):
    aggregate: _Aggregate
    sessions: list[_Session]


class _Report(_ReplayModel):
    basis: str
    bars_minutes: int = Field(gt=0)
    days: list[date] = Field(min_length=1)
    params: dict[str, str | bool | int | float | None]
    by_width: dict[Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")], _Scenario] = Field(min_length=1)


@lru_cache(maxsize=16)
def _parse(key: str, path: Path, version: tuple[int, int, int]) -> _Report | None:
    # version invalidates this parsed copy when a report is replaced or rewritten.
    report = _Report.model_validate_json(path.read_bytes())
    expected_ticker = "SPY" if key == "spy_iron_fly" else "XSP"
    if report.params.get("ticker") != expected_ticker:
        return None
    if key == "spy_iron_fly" and (report.params.get("replay_source") != SPY_SOURCE
                                  or report.params.get("close_before_settlement") is not True
                                  or report.params.get("flatten_minutes_before_close") != 30):
        return None
    return report


def _read(key: str, relative_path: str) -> _Report | None:
    path = Path(get_settings().history_cache_dir) / relative_path
    try:
        stat = path.stat()
        return _parse(key, path, (stat.st_ino, stat.st_mtime_ns, stat.st_size))
    except FileNotFoundError:
        return None
    except (OSError, ValidationError):
        logger.warning("Unreadable saved backtest report at %s", path)
        return None


def _metadata(report_id: str, label: str, report: _Report) -> dict:
    return {"id": report_id, "label": label, "start": min(report.days).isoformat(),
            "end": max(report.days).isoformat(), "bars_minutes": report.bars_minutes}


def has_reports(key: str) -> bool:
    return any(_read(key, path) is not None for _, path in _CATALOG.get(key, {}).values())


def list_reports(key: str) -> list[dict]:
    reports = []
    for report_id, (label, path) in _CATALOG.get(key, {}).items():
        report = _read(key, path)
        if report is not None:
            reports.append(_metadata(report_id, label, report))
    return reports


def get_report(key: str, report_id: str) -> dict | None:
    catalog = _CATALOG.get(key, {})
    if report_id not in catalog:
        return None
    label, path = catalog[report_id]
    report = _read(key, path)
    if report is None:
        return None
    return {**_metadata(report_id, label, report), **report.model_dump(mode="json", exclude_unset=True)}

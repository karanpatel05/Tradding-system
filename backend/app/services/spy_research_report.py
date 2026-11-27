"""Read the saved SPY iron fly research summary and its published attachments."""

import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import logging
import math
from pathlib import Path
import re

from app.config import get_settings
from app.strategies.xsp_iron_fly import calendar

KEY = "spy_iron_fly"
REPORT_ID = "research-iron-fly"
LABEL = "Research · SPY 0DTE iron fly"
PERIOD_ID = "research-february-one-cent"
PERIOD_START = date(2024, 2, 1)
PERIOD_CSV = "W10_stop150_full_spread_0.01_from_2024-02-01.csv"
DAILY_FIELDS = ("date", "contracts", "wing_width", "stop_multiple", "full_bid_ask_spread",
                "half_spread_per_fill", "fee_per_contract", "net_pnl_usd")
DAILY_SETTINGS = ("10", "1.5", "0.01", "0.005", "0.035")
_METRICS = ("total_pnl", "avg_day", "median_day", "win_rate_pct", "std_day", "sharpe",
            "max_drawdown", "best_day", "worst_day")
_FILES = {
    "summary.txt": ("results/summary.txt", "Full research summary"),
    **{f"daily_pnl_W{w}.csv": (f"results/daily_pnl_W{w}.csv",
                                f"${w} wings · $0.02 half-spread · daily CSV") for w in (10, 5, 3)},
    "SPY_IronFly_Backtest_Report.pdf": ("SPY_IronFly_Backtest_Report.pdf", "Backtest PDF · through Nov 18, 2026"),
    "SPY_IronFly_5K_Report.pdf": ("results/SPY_IronFly_5K_Report.pdf",
                                   "$5,000 funded-account PDF · through Nov 18, 2026"),
}
logger = logging.getLogger("trading.backtest_reports")


def file_path(filename: str) -> Path | None:
    entry = _FILES.get(filename)
    return _copied_file(entry[0]) if entry else None


def _copied_file(relative_path: str) -> Path | None:
    root = Path(get_settings().history_cache_dir) / "spy_iron_fly_research"
    path = (root / relative_path).resolve()
    return path if path.is_relative_to(root.resolve()) and path.is_file() else None


@lru_cache(maxsize=4)
def _parse(path: Path, version: tuple[int, int, int]) -> dict:
    # Source summary contains the saved, rounded metrics for every half-spread.
    # The daily CSVs are a separate export at the researcher's base half-spread.
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith("SPY 0DTE ATM IRON FLY"):
        raise ValueError("Unexpected research title")
    header = re.fullmatch(r"Days traded:\s*(\d+)\s+Period:\s*([\d-]+) -> ([\d-]+)", lines[1])
    if not header:
        raise ValueError("Missing research period")
    count, start, end = header.groups()
    if int(count) <= 0 or date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Invalid research period")
    width = None
    scenarios = []
    for line in lines[2:]:
        if title := re.match(r"=== Wing width \$(\d+)\b", line):
            width = int(title[1])
            continue
        if line.startswith(("===", "Wing entry source")):
            width = None
        if width is None or "|" not in line or line.lstrip().startswith("mode "):
            continue
        setting, *cells = line.split("|")
        mode, spread = setting.split()
        metrics = {k: float(v.replace(",", "")) for k, v in zip(_METRICS, cells, strict=True)}
        if math.isnan(metrics["sharpe"]):
            metrics["sharpe"] = None  # research outputs 'nan' for a flat series
        half_spread = float(spread)
        if not math.isfinite(half_spread) or any(
            value is not None and not math.isfinite(value) for value in metrics.values()
        ):
            raise ValueError("Invalid research metrics")
        scenarios.append({"wing_width": width, "mode": mode, "half_spread": half_spread, **metrics})
    if not scenarios:
        raise ValueError("Missing research scenarios")
    return {"id": REPORT_ID, "kind": "research", "label": LABEL, "start": start, "end": end,
            "days_traded": int(count), "scenarios": scenarios}


def read() -> dict | None:
    path = file_path("summary.txt")
    if path is None:
        return None
    try:
        stat = path.stat()
        return _parse(path, (stat.st_ino, stat.st_mtime_ns, stat.st_size))
    except (IndexError, OSError, ValueError):
        logger.warning("Unreadable SPY research summary at %s", path)
        return None


def metadata(report: dict) -> dict:
    return {k: report[k] for k in ("id", "kind", "label", "start", "end")}


def downloads() -> list[dict]:
    base = f"/api/strategies/{KEY}/backtest/reports/{REPORT_ID}/files"
    return [{"filename": name, "label": label, "url": f"{base}/{name}"}
            for name, (_, label) in _FILES.items() if file_path(name) is not None]


def daily_csv_path() -> Path:
    return Path(get_settings().history_cache_dir) / "spy_iron_fly" / "daily_backtest.csv"


def period_csv() -> Path | None:
    active = daily_csv_path()
    return active if active.is_file() else _copied_file(f"derived/{PERIOD_CSV}")


@lru_cache(maxsize=4)
def _period(path: Path, version: tuple[int, int, int]) -> dict:
    sessions = []
    observed = set()
    gaps = set()
    last_day = PERIOD_START - timedelta(days=1)
    wins = 0
    net = peak = drawdown = Decimal(0)
    expected = tuple(map(Decimal, DAILY_SETTINGS))
    with path.open(newline="", encoding="utf-8") as data:
        for row in csv.DictReader(data):
            day = date.fromisoformat(row["date"])
            # New dates come from the broker's exchange calendar, which can extend
            # beyond the years configured for the paper strategy.
            if (day <= last_day or day.weekday() >= 5
                    or (calendar.calendar_configured(day) and not calendar.is_trading_day(day))
                    or tuple(Decimal(row[k]) for k in DAILY_FIELDS[2:-1]) != expected):
                raise ValueError("Unexpected session or scenario in the SPY research export")
            observed.add(day)
            last_day = day
            if row["contracts"] == "0" and row["net_pnl_usd"] == "":
                gaps.add(day)
                continue
            pnl = Decimal(row["net_pnl_usd"])
            if Decimal(row["contracts"]) != 1 or not pnl.is_finite():
                raise ValueError("Invalid trade in the SPY daily export")
            sessions.append({"date": day.isoformat(), "pnl": float(pnl)})
            wins += int(pnl > 0)
            net += pnl
            peak = max(peak, net)
            drawdown = min(drawdown, net - peak)
    if not observed:
        raise ValueError("Empty research export")
    days = (PERIOD_START + timedelta(days=i) for i in range((last_day - PERIOD_START).days + 1))
    gaps.update(day for day in days if calendar.is_trading_day(day) and day not in observed)
    summary = {
        "trades": len(sessions), "wins": wins, "win_rate_pct": round(100 * wins / len(sessions), 2) if sessions else None,
        "total_pnl": float(round(net, 2)), "max_drawdown": float(round(drawdown, 2)),
    }
    download = {
        "filename": PERIOD_CSV, "label": "Download daily results (CSV)",
        "url": f"/api/strategies/{KEY}/backtest/reports/{PERIOD_ID}/files/{PERIOD_CSV}",
    }
    return {
        "id": PERIOD_ID, "kind": "research-period", "label": "SPY · Daily backtest ($0.01 full spread)",
        "start": PERIOD_START.isoformat(), "end": last_day.isoformat(), "missing_dates": sorted(d.isoformat() for d in gaps),
        "summary": summary, "sessions": sessions, "downloads": [download],
    }


def load_daily_csv(path: Path) -> dict:
    stat = path.stat()
    return _period(path, (stat.st_ino, stat.st_mtime_ns, stat.st_size))


def read_period() -> dict | None:
    path = period_csv()
    if path is None:
        return None
    try:
        return load_daily_csv(path)
    except (OSError, csv.Error, TypeError, KeyError, InvalidOperation, ValueError):
        logger.warning("Unreadable SPY research daily export at %s", path)
        return None

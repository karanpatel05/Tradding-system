"""Serve the trading system's daily QQQ first-hour credit spread backtest."""

import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import logging
from pathlib import Path

from app.config import get_settings
from app.strategies.xsp_iron_fly import calendar

KEY = "qqq_credit_spread"
REPORT_ID = "first-hour-daily"
PERIOD_START = date(2024, 2, 1)
FILENAME = "QQQ_first_hour_daily.csv"
DAILY_FIELDS = ("date", "status", "side", "short_strike", "long_strike", "entry_credit",
                "exit_debit", "exit_method", "net_pnl_usd", "reason")
METHOD = ("One QQQ 0DTE $1 spread per trade. A green first hour sells a put spread and a red first hour "
          "sells a call spread. Entry uses the first paired option minute closes after 10:30 ET through "
          "10:40 ET, with a $0.02 minimum credit. The model charges a $0.01 full spread per leg "
          "($1 per spread at entry and $1 at exit) and zero commission. At 15:50 ET, or 12:50 ET on "
          "half days, it uses the latest paired closes from the preceding two minutes. If those prices "
          "are missing or inconsistent, it estimates intrinsic value using a QQQ close up to 15 minutes old.")

logger = logging.getLogger("trading.backtest_reports")


def daily_csv_path() -> Path:
    return Path(get_settings().history_cache_dir) / KEY / "daily_backtest.csv"


def _amount(row: dict, key: str) -> Decimal:
    value = Decimal(row[key])
    if not value.is_finite():
        raise ValueError(f"Invalid {key} in QQQ export")
    return value


@lru_cache(maxsize=4)
def _parse(path: Path, version: tuple[int, int, int]) -> dict:
    last = None
    observed = set()
    sessions = []
    excluded = {"skipped": [], "missing": [], "unresolved": []}
    net = peak = drawdown = Decimal(0)
    wins = intrinsic_exits = 0
    with path.open(newline="", encoding="utf-8") as data:
        reader = csv.DictReader(data)
        if tuple(reader.fieldnames or ()) != DAILY_FIELDS:
            raise ValueError("Unexpected QQQ daily CSV columns")
        for row in reader:
            day = date.fromisoformat(row["date"])
            if (None in row or any(value is None for value in row.values())
                    or day < PERIOD_START or (last is not None and day <= last) or day.weekday() >= 5
                    or (calendar.calendar_configured(day) and not calendar.is_trading_day(day))):
                raise ValueError("Invalid QQQ session or row")
            observed.add(day)
            last = day
            status = row["status"]
            if status in ("traded", "unresolved"):
                width = _amount(row, "short_strike") - _amount(row, "long_strike")
                credit = _amount(row, "entry_credit")
                if ((row["side"], width) not in {("put", 1), ("call", -1)}
                        or not Decimal("0.02") <= credit < 1):
                    raise ValueError("Invalid QQQ spread entry")
            if status in excluded:
                if row["net_pnl_usd"] or row["exit_method"] or row["exit_debit"] or not row["reason"]:
                    raise ValueError("Unexpected result or missing reason for excluded session")
                excluded[status].append(row["date"])
                continue
            if status != "traded":
                raise ValueError("Unknown QQQ backtest status")
            debit = _amount(row, "exit_debit")
            pnl = _amount(row, "net_pnl_usd")
            if (row["exit_method"] not in {"bar", "intrinsic"} or not Decimal("0.01") <= debit <= Decimal("1.01")
                    or pnl != (credit - debit) * 100):
                raise ValueError("Inconsistent QQQ trade")
            intrinsic_exits += int(row["exit_method"] == "intrinsic")
            sessions.append({"date": row["date"], "pnl": float(pnl)})
            wins += int(pnl > 0)
            net += pnl
            peak = max(peak, net)
            drawdown = min(drawdown, net - peak)
    if last is None:
        raise ValueError("Empty QQQ export")
    days = (PERIOD_START + timedelta(days=i) for i in range((last - PERIOD_START).days + 1))
    excluded["missing"].extend(day.isoformat() for day in days if calendar.is_trading_day(day) and day not in observed)
    excluded["missing"].sort()
    count = len(sessions)
    return {
        "id": REPORT_ID, "kind": "daily-backtest", "label": "QQQ · Daily backtest ($0.01 full spread)",
        "start": PERIOD_START.isoformat(), "end": last.isoformat(), "method": METHOD,
        "sessions": sessions, "missing_dates": excluded["missing"], "skipped_dates": excluded["skipped"],
        "unresolved_dates": excluded["unresolved"], "intrinsic_exits": intrinsic_exits,
        "summary": {"trades": count, "wins": wins, "win_rate_pct": round(100 * wins / count, 2) if count else None,
                    "total_pnl": float(net), "max_drawdown": float(drawdown)},
        "downloads": [{"filename": FILENAME, "label": "Download daily results (CSV)",
                       "url": f"/api/strategies/{KEY}/backtest/reports/{REPORT_ID}/files/{FILENAME}"}],
    }


def load_daily_csv(path: Path) -> dict:
    stat = path.stat()
    return _parse(path, (stat.st_ino, stat.st_mtime_ns, stat.st_size))


def read() -> dict | None:
    path = daily_csv_path()
    try:
        return load_daily_csv(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, InvalidOperation, TypeError, csv.Error):
        logger.warning("Unreadable QQQ daily backtest at %s", path)
        return None

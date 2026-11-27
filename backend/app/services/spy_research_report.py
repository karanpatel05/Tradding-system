"""Read the saved SPY iron fly research summary and its published attachments."""

from datetime import date
from functools import lru_cache
import logging
import math
from pathlib import Path
import re

from app.config import get_settings

KEY = "spy_iron_fly"
REPORT_ID = "research-iron-fly"
LABEL = "Research · SPY 0DTE iron fly"
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
    if not entry:
        return None
    root = Path(get_settings().history_cache_dir) / "spy_iron_fly_research"
    path = (root / entry[0]).resolve()
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

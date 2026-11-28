"""Exchange dates and atomic CSV publication for trading-system backtests."""

import csv
from datetime import date, datetime, time, timedelta
import os
from pathlib import Path
import tempfile

import httpx

from app.strategies.xsp_iron_fly.calendar import ET


def previous_date(now: datetime) -> date:
    return now.astimezone(ET).date() - timedelta(days=1)


def exchange_sessions(client: httpx.Client, start: date, end: date) -> list[dict]:
    if start > end:
        return []
    response = client.get("/v2/calendar", params={"start": start.isoformat(), "end": end.isoformat()})
    response.raise_for_status()
    sessions = response.json()
    if not isinstance(sessions, list):
        raise ValueError("Alpaca returned an invalid trading calendar")
    previous = start - timedelta(days=1)
    for session in sessions:
        day = date.fromisoformat(session["date"])
        if not previous < day <= end or time.fromisoformat(session["open"]) >= time.fromisoformat(session["close"]):
            raise ValueError("Unexpected session in the Alpaca trading calendar")
        previous = day
    return sessions


def load_rows(path: Path, report) -> dict[date, dict]:
    report.load_daily_csv(path)
    with path.open(newline="", encoding="utf-8") as data:
        reader = csv.DictReader(data)
        if tuple(reader.fieldnames or ()) != report.DAILY_FIELDS:
            raise ValueError("Unexpected daily backtest CSV columns")
        return {date.fromisoformat(row["date"]): row for row in reader}


def publish_rows(path: Path, rows: dict, report) -> None:
    # Replace only after the same parser that serves the frontend accepts the export.
    with tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8", dir=path.parent,
                                     delete=False) as data:
        temp_path = Path(data.name)
        try:
            writer = csv.DictWriter(data, fieldnames=report.DAILY_FIELDS)
            writer.writeheader()
            writer.writerows(rows[day] for day in sorted(rows))
            data.flush()
            os.fsync(data.fileno())
            report.load_daily_csv(temp_path)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


def historical_bars(client, path, **params):
    """Alpaca pages across all requested symbols; only a successful final page completes the query."""
    result = {}
    seen = set()
    while True:
        response = client.get(path, params=params)
        response.raise_for_status()
        page = response.json()
        for symbol, bars in (page["bars"] or {}).items():
            result.setdefault(symbol, []).extend(bars)
        token = page.get("next_page_token")
        if not token:
            return result
        if token in seen:
            raise ValueError("Repeated Alpaca bar pagination token")
        seen.add(token)
        params["page_token"] = token

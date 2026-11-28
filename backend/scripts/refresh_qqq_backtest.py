"""Refresh QQQ daily backtest results through yesterday in New York."""

from datetime import date, datetime, timedelta
import fcntl

import httpx

from app.broker import alpaca_client
from app.services import qqq_backtest_report as report
from app.strategies.xsp_iron_fly.calendar import ET
from scripts.daily_backtest import exchange_sessions, load_rows, previous_date, publish_rows
from scripts.qqq_backtest_days import price_session


def refresh(through: date, calendar_client: httpx.Client, data_client: httpx.Client) -> None:
    path = report.daily_csv_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".refresh.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A QQQ daily refresh is already running.")
            return
        rows = load_rows(path, report) if path.exists() else {}
        # One calendar request detects missed sessions anywhere in the history.
        for session in exchange_sessions(calendar_client, report.PERIOD_START, through):
            day = date.fromisoformat(session["date"])
            old = rows.get(day)
            if old and (old["status"] not in ("missing", "unresolved") or day < through - timedelta(days=7)):
                continue
            new = price_session(data_client, session)
            if new["date"] != session["date"]:
                raise ValueError("QQQ replay returned a different session date")
            if old == new or (old and old["status"] == "unresolved" and new["status"] in ("missing", "skipped")):
                continue
            rows[day] = new
            publish_rows(path, rows, report)
            print(f"{day} {new['status']} {new['net_pnl_usd'] or new['reason']}", flush=True)
        if rows:
            print(f"QQQ daily backtest through {max(rows)}; checked calendar through {through}.")


def main() -> None:
    headers = alpaca_client.headers()
    with httpx.Client(base_url=alpaca_client.base_url(), headers=headers, timeout=30) as calendar_client, \
         httpx.Client(base_url="https://data.alpaca.markets", headers=headers, timeout=45) as data_client:
        refresh(previous_date(datetime.now(ET)), calendar_client, data_client)


if __name__ == "__main__":
    main()

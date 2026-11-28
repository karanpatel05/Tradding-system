"""Update the trading system's daily SPY backtest through yesterday in New York."""

from datetime import date, datetime, timedelta
import fcntl

import httpx

from app.broker import alpaca_client
from app.services import spy_research_report as report
from app.strategies.xsp_iron_fly.calendar import ET
from scripts.daily_backtest import exchange_sessions, load_rows, previous_date, publish_rows
from scripts.spy_research_days import price_session


def refresh(through: date, calendar_client: httpx.Client, data_client: httpx.Client) -> None:
    path = report.daily_csv_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".refresh.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A SPY daily refresh is already running.")
            return
        # On upgrade, seed once from the previously published trading-system CSV.
        # A new installation without that baseline can backfill directly from Alpaca.
        existing = report.period_csv()
        rows = load_rows(existing, report) if existing is not None else {}
        if rows and not path.exists():
            publish_rows(path, rows, report)

        last = max(rows, default=report.PERIOD_START - timedelta(days=1))
        # Revisit recent entry gaps in case the provider backfills delayed bars.
        gaps = {day for day, row in rows.items()
                if through - timedelta(days=7) <= day <= through and row["contracts"] == "0"}
        start = min({last + timedelta(days=1)} | gaps)
        for session in exchange_sessions(calendar_client, start, through):
            day = date.fromisoformat(session["date"])
            if day in rows and day not in gaps:
                continue
            pnl, reason = price_session(data_client, session)
            if pnl is None and not reason:
                raise ValueError(f"Missing both a result and an entry-gap reason for {day}")
            row = dict(zip(report.DAILY_FIELDS, (day.isoformat(), "0" if pnl is None else "1",
                                                 *report.DAILY_SETTINGS, "" if pnl is None else pnl), strict=True))
            if rows.get(day) != row:
                rows[day] = row
                publish_rows(path, rows, report)
            print(f"{day}: {reason if pnl is None else f'net P&L {pnl} USD'}", flush=True)

        if rows:
            print(f"SPY daily backtest through {max(rows)}; checked calendar through {through}.")


def main() -> None:
    headers = alpaca_client.headers()
    with httpx.Client(base_url=alpaca_client.base_url(), headers=headers, timeout=30) as calendar_client, \
         httpx.Client(base_url="https://data.alpaca.markets", headers=headers, timeout=45) as data_client:
        refresh(previous_date(datetime.now(ET)), calendar_client, data_client)


if __name__ == "__main__":
    main()

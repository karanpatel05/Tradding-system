#!/usr/bin/env python3
"""Install the SPY report's daily macOS user job with python3 scripts/install_spy_backtest_refresh.py."""

import os
import plistlib
import subprocess
import tempfile
import time
from pathlib import Path


LABEL = "com.tradingsystem.spy-backtest-refresh"


def job_config(library):
    with (library / "LaunchAgents/com.tradingsystem.web.plist").open("rb") as stream:
        web_env = plistlib.load(stream)["EnvironmentVariables"]
    repo = Path(web_env["TS_REPO"])
    backend = Path(web_env["TS_LIVE"]) / "backend"
    python = repo / ".venv/bin/python"
    if not (backend / "scripts/refresh_spy_backtest.py").is_file() or not os.access(python, os.X_OK):
        raise SystemExit(f"Install the release containing scripts.refresh_spy_backtest in {backend} first.")
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-u", "-m", "scripts.refresh_spy_backtest"],
        "WorkingDirectory": str(backend),
        "EnvironmentVariables": {"HISTORY_CACHE_DIR": str(repo / "backend/data_cache")},
        "RunAtLoad": True,
        "StartCalendarInterval": {"Hour": 6, "Minute": 15},  # Mac's local timezone
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 1800,
        "StandardOutPath": str(library / f"Logs/{LABEL}.log"),
        "StandardErrorPath": str(library / f"Logs/{LABEL}.err.log"),
    }


def write_plist(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        try:
            stream.write(data)
            stream.flush()
            os.replace(stream.name, path)
        finally:
            Path(stream.name).unlink(missing_ok=True)


def bootstrap(domain, path):
    # launchd can briefly retain a previous job while a bootout finishes.
    for attempt in range(5):
        result = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(path)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return
        if attempt < 4:
            time.sleep(1)
    raise RuntimeError(result.stderr.strip() or "Could not load the SPY report refresh job")


def main():
    library = Path.home() / "Library"
    config = job_config(library)
    path = library / f"LaunchAgents/{LABEL}.plist"
    (library / "Logs").mkdir(exist_ok=True)
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    data = plistlib.dumps(config)
    previous = path.read_bytes() if path.exists() else None
    loaded = subprocess.run(["/bin/launchctl", "print", service], capture_output=True).returncode == 0
    if not loaded or previous != data:
        if loaded:
            subprocess.run(["/bin/launchctl", "bootout", service], check=True)
        try:
            write_plist(path, data)
            subprocess.run(["/bin/launchctl", "enable", service], check=True)
            bootstrap(domain, path)
        except Exception:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                write_plist(path, previous)
                if loaded:
                    bootstrap(domain, path)
            raise
    print(f"{LABEL} is installed. Refresh runs daily at 06:15 local time and catches up at login or wake.")
    print(f"Refresh output goes to {config['StandardOutPath']}")


if __name__ == "__main__":
    main()

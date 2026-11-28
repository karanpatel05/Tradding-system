#!/usr/bin/env python3
"""Install a strategy's daily backtest refresh as a macOS user job."""

import argparse
import os
import plistlib
import subprocess
import tempfile
import time
from pathlib import Path


def job_config(library, strategy="spy"):
    label = f"com.tradingsystem.{strategy}-backtest-refresh"
    module = f"refresh_{strategy}_backtest"
    with (library / "LaunchAgents/com.tradingsystem.web.plist").open("rb") as stream:
        web_env = plistlib.load(stream)["EnvironmentVariables"]
    repo = Path(web_env["TS_REPO"])
    backend = Path(web_env["TS_LIVE"]) / "backend"
    python = repo / ".venv/bin/python"
    if not (backend / f"scripts/{module}.py").is_file() or not os.access(python, os.X_OK):
        raise SystemExit(f"Install the release containing scripts.{module} in {backend} first.")
    return {
        "Label": label,
        "ProgramArguments": [str(python), "-u", "-m", f"scripts.{module}"],
        "WorkingDirectory": str(backend),
        "EnvironmentVariables": {"HISTORY_CACHE_DIR": str(repo / "backend/data_cache")},
        "RunAtLoad": True,
        "StartCalendarInterval": {"Hour": 6, "Minute": 15},  # Mac's local timezone
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 1800,
        "StandardOutPath": str(library / f"Logs/{label}.log"),
        "StandardErrorPath": str(library / f"Logs/{label}.err.log"),
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
    raise RuntimeError(result.stderr.strip() or "Could not load the daily report refresh job")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", choices=("spy", "qqq"), default="spy")
    args = parser.parse_args()
    library = Path.home() / "Library"
    config = job_config(library, args.strategy)
    label = config["Label"]
    path = library / f"LaunchAgents/{label}.plist"
    (library / "Logs").mkdir(exist_ok=True)
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{label}"
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
    print(f"{label} is installed. Refresh runs daily at 06:15 local time and catches up at login or wake.")
    print(f"Refresh output goes to {config['StandardOutPath']}")


if __name__ == "__main__":
    main()

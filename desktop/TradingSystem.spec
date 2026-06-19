import os
# -*- mode: python ; coding: utf-8 -*-
# Build: cd desktop && ../.venv/bin/pyinstaller --noconfirm --clean TradingSystem.spec
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
ROOT = os.path.abspath(os.path.join(os.getcwd(), ".."))

datas = [
    (os.path.join(ROOT, "frontend", "dist"), "frontend_dist"),
    # Bundle ONLY the app package (not the whole backend/ — that holds data_cache,
    # trading.db and tests we don't want in the .app).
    (os.path.join(ROOT, "backend", "app"), "app"),
]
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("app")
    + ["aiosqlite", "pydantic_settings", "apscheduler", "websockets"]
)
for pkg in ("ib_async", "sqlalchemy", "fastapi", "starlette", "pydantic"):
    d, b, h = collect_all(pkg)
    datas += d
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, "desktop", "app.py")],
    pathex=[os.path.join(ROOT, "backend")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TradingSystem", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="TradingSystem")
app = BUNDLE(coll, name="TradingSystem.app", icon=None,
             bundle_identifier="com.local.tradingsystem")

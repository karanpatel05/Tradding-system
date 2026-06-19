"""Serve the built React app same-origin from the FastAPI process (packaged desktop
build). No-op unless ``dist_dir`` is set and exists, so the dev workflow (Vite) is
untouched. Mount this AFTER all /api and /ws routers so it never shadows them."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_frontend(app: FastAPI, dist_dir: str) -> None:
    if not dist_dir:
        return
    dist = Path(dist_dir)
    index = dist / "index.html"
    if not index.exists():
        return

    assets = dist / "assets"
    if assets.is_dir():
        # Hashed bundles; a miss here returns 404 (never the SPA fallback).
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        # API/WS are matched by earlier routers; guard just in case.
        if full_path.startswith(("api/", "ws")):
            raise HTTPException(status_code=404)
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))   # e.g. favicon, vite.svg
        return FileResponse(str(index))           # SPA fallback

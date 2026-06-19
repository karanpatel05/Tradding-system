from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.static import mount_frontend


def _make_dist(tmp_path: Path) -> str:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>App</title>")
    (tmp_path / "assets" / "main.js").write_text("console.log('hi')")
    return str(tmp_path)


def _app(dist: str) -> FastAPI:
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    mount_frontend(app, dist)
    return app


def test_serves_index_and_spa_fallback(tmp_path):
    client = TestClient(_app(_make_dist(tmp_path)))
    assert client.get("/").status_code == 200
    assert "<title>App</title>" in client.get("/").text
    r = client.get("/strategies")
    assert r.status_code == 200 and "<title>App</title>" in r.text


def test_api_routes_not_shadowed(tmp_path):
    client = TestClient(_app(_make_dist(tmp_path)))
    assert client.get("/api/ping").json() == {"ok": True}


def test_asset_miss_is_404_not_html(tmp_path):
    client = TestClient(_app(_make_dist(tmp_path)))
    assert client.get("/assets/main.js").status_code == 200
    assert client.get("/assets/missing.js").status_code == 404


def test_noop_when_dist_missing():
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    mount_frontend(app, "")  # disabled
    client = TestClient(app)
    assert client.get("/api/ping").json() == {"ok": True}
    assert client.get("/").status_code == 404

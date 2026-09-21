"""Dashboard smoke tests (skipped automatically when FastAPI isn't installed)."""
from __future__ import annotations

import base64

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient      # noqa: E402

from app import config as cfg                  # noqa: E402
from app.services import billing               # noqa: E402
from app.storage import db                     # noqa: E402
from tests.test_flows import ADMIN_ID, USER_ID  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "web.sqlite3"))
    monkeypatch.setattr(cfg, "WEB_USER", "admin")
    monkeypatch.setattr(cfg, "WEB_PASS", "secret")
    import web.dashboard as dashboard
    return TestClient(dashboard.app)


def _auth(user="admin", password="secret") -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_auth_is_enforced(client):
    assert client.get("/").status_code == 401
    assert client.get("/", headers=_auth("admin", "wrong")).status_code == 401
    assert client.get("/", headers=_auth()).status_code == 200
    assert client.get("/health").status_code == 200          # monitoring stays open


def test_dashboard_shows_stats(client):
    store = db.create_store(ADMIN_ID, "Movies")
    db.add_file(store["id"], "Interstellar", "Video", -100, 1)
    db.touch_user(USER_ID, "Buyer", "buyer")

    page = client.get("/", headers=_auth()).text
    assert "Store Bot Dashboard" in page
    assert "Interstellar" in page


def test_order_can_be_approved_from_the_dashboard(client):
    store = db.create_store(ADMIN_ID, "Movies")
    plan = db.plan(db.add_plan(store["id"], "1 Month", 30, 199))
    order = billing.create_order_from_plan(USER_ID, store["id"], plan, method="bkash")

    page = client.get("/orders", headers=_auth()).text
    assert f"#{order['id']}" in page

    response = client.get(f"/orders/approve/{order['id']}", headers=_auth(),
                          follow_redirects=False)
    assert response.status_code == 303
    assert db.order(order["id"])["status"] == "approved"
    assert db.grant_row(store["id"], USER_ID) is not None


def test_file_search_and_delete(client):
    store = db.create_store(ADMIN_ID, "Movies")
    file_id = db.add_file(store["id"], "Dune Part Two", "Video", -100, 5)

    assert "Dune" in client.get("/files?q=dune", headers=_auth()).text
    client.get(f"/files/delete/{file_id}", headers=_auth(), follow_redirects=False)
    assert db.file(file_id) is None


def test_users_page_search(client):
    db.touch_user(USER_ID, "Karim Uddin", "karim")
    page = client.get("/users?q=karim", headers=_auth()).text
    assert "Karim" in page


def test_broadcast_requires_bot_client(client):
    """Without a running bot client the dashboard must report, not crash."""
    response = client.post("/broadcast", data={"audience": "all", "text": "hello"},
                           headers=_auth(), follow_redirects=False)
    assert response.status_code == 303
    assert "not+running" in response.headers["location"] or "sent" in response.headers["location"]

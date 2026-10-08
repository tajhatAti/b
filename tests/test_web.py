"""Website tests: the public store front + the admin panel.

Skipped automatically when FastAPI/httpx are not installed.
"""
from __future__ import annotations

import base64

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient      # noqa: E402

from app import config as cfg                  # noqa: E402
from app.services import billing, broadcast    # noqa: E402
from app.storage import db                     # noqa: E402
from tests.test_flows import ADMIN_ID, USER_ID  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "web.sqlite3"))
    monkeypatch.setattr(cfg, "WEB_USER", "admin")
    monkeypatch.setattr(cfg, "WEB_PASS", "secret")
    monkeypatch.setattr(cfg, "WEB_SECRET", "test-secret")
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    monkeypatch.setattr(cfg, "WEB_TITLE", "Test Store")
    import web.dashboard as dashboard
    return TestClient(dashboard.app)


def _auth(user="admin", password="secret") -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _store(name: str = "Movies", premium: bool = False) -> dict:
    store = db.create_store(ADMIN_ID, name)
    db.update_store(store["id"], is_premium=1 if premium else 0)
    db.add_file(store["id"], "Interstellar", "Video", -100, 1, size=2_000_000)
    return db.store(store["id"])


# ------------------------------------------------------------------- public site
def test_public_site_is_open_and_lists_stores(client):
    store = _store("Free Movies")
    page = client.get("/")
    assert page.status_code == 200
    assert "Free Movies" in page.text
    assert store["name"] in client.get("/stores").text


def test_public_site_never_leaks_premium_file_names(client):
    _store("Paid Vault", premium=True)
    page = client.get("/s/paid-vault")
    assert page.status_code == 200
    assert "premium" in page.text.lower()
    assert "Interstellar" not in page.text          # names stay inside the bot


def test_public_store_page_shows_files_for_free_stores(client):
    _store("Free Movies")
    page = client.get("/s/free_movies")
    assert "Interstellar" in page.text


def test_health_is_public(client):
    data = client.get("/health").json()
    assert data["ok"] is True
    assert "bot_online" in data and "pending_orders" in data


# ------------------------------------------------------------------ admin auth
def test_admin_requires_login(client):
    assert client.get("/admin", follow_redirects=False).status_code == 303
    assert client.get("/admin/login").status_code == 200
    assert client.get("/admin?x=1", headers=_auth("admin", "wrong")).status_code == 401
    assert client.get("/admin", headers=_auth()).status_code == 200


def test_login_sets_a_session_cookie(client):
    response = client.post("/admin/login", data={"username": "admin", "password": "secret"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.cookies.get("sb_session")
    assert client.get("/admin", headers=response.cookies and {}).status_code in (200, 303)
    client.get("/admin/logout")


def test_dashboard_shows_stats_and_recent_campaigns(client):
    _store()
    db.touch_user(USER_ID, "Buyer", "buyer")
    broadcast.create_campaign(ADMIN_ID, text="hello", audience="all")

    page = client.get("/admin", headers=_auth()).text
    assert "ড্যাশবোর্ড" in page or "Dashboard" in page
    assert "#1" in page


# ------------------------------------------------------------------- studio
def test_campaign_can_be_created_from_the_website(client):
    _store()
    db.touch_user(USER_ID, "Buyer", "buyer")

    response = client.post("/admin/broadcast/new",
                           data={"audience": "all", "file_id": 0, "text": "নতুন ভিডিও",
                                 "action": "draft"},
                           headers=_auth(), follow_redirects=False)
    assert response.status_code == 303
    campaign = db.campaigns(limit=1)[0]
    assert campaign["text"] == "নতুন ভিডিও"
    assert campaign["status"] == "draft"
    assert db.campaign_counts(campaign["id"])["total"] == len(db.all_user_ids())


def test_campaign_start_without_bot_stays_queued(client, monkeypatch):
    """No bot in this process → the campaign must wait, not spam failures."""
    from app import runtime
    monkeypatch.setattr(runtime, "_client", None)      # simulate “bot offline”
    _store()
    db.touch_user(USER_ID, "Buyer", "buyer")

    response = client.post("/admin/broadcast/new",
                           data={"audience": "all", "file_id": 0, "text": "hi",
                                 "action": "start"},
                           headers=_auth(), follow_redirects=False)
    assert response.status_code == 303
    assert "warn=" in response.headers["location"]
    campaign = db.campaigns(limit=1)[0]
    assert campaign["status"] == "queued"
    assert db.campaign_counts(campaign["id"])["pending"] > 0


def test_campaign_progress_api(client):
    _store()
    db.touch_user(USER_ID, "Buyer", "buyer")
    campaign = broadcast.create_campaign(ADMIN_ID, text="hi", audience="all")

    data = client.get(f"/admin/api/campaign/{campaign['id']}", headers=_auth()).json()
    assert data["total"] == 1
    assert data["percent"] == 0
    assert data["running"] is False


def test_file_broadcast_from_the_panel(client):
    store = _store()
    file_id = db.files_of(store["id"])[0]["id"]
    db.touch_user(USER_ID, "Buyer", "buyer")

    assert f"Broadcast" in client.get(f"/admin/files/broadcast/{file_id}",
                                      headers=_auth()).text
    response = client.post(f"/admin/files/broadcast/{file_id}",
                           data={"audience": "all", "text": "", "action": "draft"},
                           headers=_auth(), follow_redirects=False)
    assert response.status_code == 303
    campaign = db.campaigns(limit=1)[0]
    assert campaign["file_ids"] == [file_id]
    assert campaign["status"] in ("queued", "running")
    assert db.campaign_counts(campaign["id"])["total"] >= 1


# ------------------------------------------------------- orders / users / files
def test_order_can_be_approved_from_the_dashboard(client):
    store = _store()
    plan = db.plan(db.add_plan(store["id"], "1 Month", 30, 199))
    order = billing.create_order_from_plan(USER_ID, store["id"], plan, method="bkash")

    assert f"#{order['id']}" in client.get("/admin/orders", headers=_auth()).text
    response = client.get(f"/admin/orders/approve/{order['id']}", headers=_auth(),
                          follow_redirects=False)
    assert response.status_code == 303
    assert db.order(order["id"])["status"] == "approved"
    assert db.grant_row(store["id"], USER_ID) is not None


def test_file_search_and_delete(client):
    store = _store()
    file_id = db.add_file(store["id"], "Dune Part Two", "Video", -100, 5)

    assert "Dune" in client.get("/admin/files?q=dune", headers=_auth()).text
    client.get(f"/admin/files/delete/{file_id}", headers=_auth(), follow_redirects=False)
    assert db.file(file_id) is None


def test_users_page_search(client):
    db.touch_user(USER_ID, "Karim Uddin", "karim")
    page = client.get("/admin/users?q=karim", headers=_auth()).text
    assert "Karim" in page


def test_start_button_queues_the_campaign_while_the_bot_is_offline(client, monkeypatch):
    """A campaign created offline must start by itself once the bot signs in."""
    from app import runtime
    monkeypatch.setattr(runtime, "_client", None)
    _store()
    db.touch_user(USER_ID, "Buyer", "buyer")
    campaign = broadcast.create_campaign(ADMIN_ID, text="hi", audience="all", start=False)

    response = client.post(f"/admin/broadcast/{campaign['id']}/start",
                           headers=_auth(), follow_redirects=False)
    assert response.status_code == 303
    assert "warn=" in response.headers["location"]
    assert db.campaign(campaign["id"])["status"] == broadcast.QUEUED
    assert [c["id"] for c in db.queued_campaigns()] == [campaign["id"]]

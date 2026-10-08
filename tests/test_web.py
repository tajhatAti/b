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


# ---------------------------------------------------------------- settings page
def test_settings_page_shows_every_group(client):
    page = client.get("/admin/settings", headers=_auth())
    assert page.status_code == 200
    for label in ("চ্যানেল জয়েন", "পেমেন্ট", "সাপোর্ট", "ব্রডকাস্ট গতি"):
        assert label in page.text, f"{label} group missing"
    assert "FORCE_CHANNEL" in page.text


def test_settings_can_be_saved_from_the_website(client, monkeypatch):
    from app import config as cfg
    from app.services import forcejoin, settings
    monkeypatch.setattr(cfg, "FORCE_CHANNEL", "")
    settings._cache.clear()

    response = client.post("/admin/settings/save", headers=_auth(),
                           data={"__group": "payments", "CURRENCY": "৳",
                                 "PAY_BKASH": "01712-345678", "PAY_NAGAD": "",
                                 "PAY_ROCKET": "", "PAY_UPI": "", "PAY_CRYPTO": "",
                                 "PAY_NOTE": "", "TRIAL_HOURS": "48",
                                 "AUTO_APPROVE_ZERO": "1"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert settings.get_str("PAY_BKASH") == "01712-345678"
    assert settings.get_int("TRIAL_HOURS") == 48
    assert settings.get_bool("AUTO_APPROVE_ZERO", False) is True


def test_force_join_accepts_a_private_invite_from_the_website(client, monkeypatch):
    from app import config as cfg
    from app.services import forcejoin, settings
    monkeypatch.setattr(cfg, "FORCE_CHANNEL", "")
    settings._cache.clear()

    response = client.post("/admin/settings/save", headers=_auth(),
                           data={"__group": "force_join",
                                 "FORCE_JOIN_ENABLED": "1",
                                 "FORCE_CHANNEL": "https://t.me/+PrivateInvite99",
                                 "FORCE_JOIN_NOTE": ""},
                           follow_redirects=False)
    assert response.status_code == 303
    assert forcejoin.switch() == "https://t.me/+PrivateInvite99"
    assert "PrivateInvite99" in settings.get_str("FORCE_CHANNEL")


def test_website_refuses_a_bad_value(client, monkeypatch):
    from app import config as cfg
    from app.services import settings
    monkeypatch.setattr(cfg, "FORCE_CHANNEL", "")
    settings._cache.clear()

    response = client.post("/admin/settings/save", headers=_auth(),
                           data={"__group": "force_join", "FORCE_JOIN_ENABLED": "1",
                                 "FORCE_CHANNEL": "?? not a channel", "FORCE_JOIN_NOTE": ""},
                           follow_redirects=False)
    assert response.status_code == 303
    assert "warn=" in response.headers["location"]
    assert settings.get_str("FORCE_CHANNEL") == ""     # nothing stored


def test_config_env_export(client):
    response = client.get("/admin/settings/export", headers=_auth())
    assert response.status_code == 200
    assert "FORCE_CHANNEL=" in response.text
    assert "config.env" in response.headers.get("content-disposition", "")


def test_public_site_uses_saved_title(client):
    from app.services import settings
    settings.set("SITE_TAGLINE", "এখানেই সব ভিডিও")
    assert "এখানেই সব ভিডিও" in client.get("/").text


# ===================================================== v3 panel pages (analytics)
def test_admin_nav_exposes_the_new_sections(client):
    page = client.get("/admin", headers=_auth())
    assert page.status_code == 200
    for label in ("📡 Channels", "📊 Analytics", "🔗 Links"):
        assert label in page.text


def test_channels_page_lists_and_adds(client):
    from app.services import channels
    db.add_join_channel(None, "@my_channel", title="My channel")
    page = client.get("/admin/channels", headers=_auth())
    assert page.status_code == 200
    assert "My channel" in page.text

    from app.services import forcejoin
    async def fake_resolve(ref, refresh=False, use_cache=True):
        return {"ok": True, "chat_id": -1001234, "title": "New channel",
                "kind": "username", "ref": ref, "error": "", "via": "bot"}
    original = forcejoin.resolve
    forcejoin.resolve = fake_resolve
    try:
        response = client.post("/admin/channels/add", headers=_auth(),
                               data={"ref": "@new_channel", "store_id": ""},
                               follow_redirects=False)
    finally:
        forcejoin.resolve = original
    assert response.status_code == 303
    assert any(row["ref"] == "@new_channel" for row in db.join_channels())


def test_composer_page_and_post(client, monkeypatch):
    from app.services import channels
    store = _store("Composer store")
    db.add_join_channel(None, "@post_here", title="Post here", chat_id=-100999)
    page = client.get(f"/admin/channels/compose?store={store['id']}", headers=_auth())
    assert page.status_code == 200
    assert "ইনলাইন বাটন" in page.text

    async def fake_publish(target, **kwargs):
        return {"ok": True, "message_id": 55, "sent": 1, "failed": 0, "error": ""}
    monkeypatch.setattr(channels, "publish", fake_publish)
    response = client.post("/admin/channels/compose", headers=_auth(),
                           data={"target": "-100999", "text": "Hello", "buttons": "",
                                 "store_id": str(store["id"]), "link_limit": "-1"},
                           follow_redirects=False)
    assert response.status_code == 200
    assert "পাঠানো হয়েছে" in response.text


def test_analytics_page_shows_funnel_and_top_files(client):
    store = _store("Analytics store")
    file_id = db.files_of(store["id"])[0]["id"]
    db.touch_user(USER_ID, "Buyer", "buyer")
    db.log_event("deliver", USER_ID, store["id"], file_id, "button")
    db.log_event("paid", USER_ID, store["id"], None, "plan:1")
    page = client.get("/admin/analytics", headers=_auth())
    assert page.status_code == 200
    assert "অ্যানালিটিক্স" in page.text
    assert "Analytics store" in page.text
    assert "ফানেল" in page.text


def test_store_analytics_page_lists_watchers(client):
    store = _store("Watch store")
    file_id = db.files_of(store["id"])[0]["id"]
    db.touch_user(USER_ID, "Watcher", "watcher")
    db.log_event("deliver", USER_ID, store["id"], file_id, "button")
    page = client.get(f"/admin/analytics/store/{store['id']}", headers=_auth())
    assert page.status_code == 200
    assert "Watcher" in page.text
    assert "Interstellar" in page.text


def test_file_watchers_page(client):
    store = _store("File store")
    file_id = db.files_of(store["id"])[0]["id"]
    db.touch_user(USER_ID, "Watcher", "watcher")
    db.log_event("deliver", USER_ID, store["id"], file_id, "button")
    page = client.get(f"/admin/analytics/file/{file_id}", headers=_auth())
    assert page.status_code == 200
    assert "Watcher" in page.text


def test_user_detail_page_shows_clicks_and_state(client):
    store = _store("User store")
    file_id = db.files_of(store["id"])[0]["id"]
    db.touch_user(USER_ID, "Detail user", "detail")
    db.log_event("start", USER_ID, None, None, "start")
    db.log_event("open_store", USER_ID, store["id"], None, "x")
    db.log_event("deliver", USER_ID, store["id"], file_id, "button")
    page = client.get(f"/admin/users/{USER_ID}", headers=_auth())
    assert page.status_code == 200
    assert "Detail user" in page.text
    assert "ভিডিও দেখেছে" in page.text
    assert "User store" in page.text


def test_limited_link_created_from_the_site(client):
    store = _store("Link store")
    file_id = db.files_of(store["id"])[0]["id"]
    response = client.post("/admin/links/new", headers=_auth(),
                           data={"file_ids": [str(file_id)], "store_id": "",
                                 "max_clicks": "100", "note": "customer A"},
                           follow_redirects=False)
    assert response.status_code == 303
    links = db.links(limit=5)
    assert links and links[0]["max_clicks"] == 100
    assert db.link_file_ids(links[0]["token"]) == [file_id]

    page = client.get("/admin/links", headers=_auth())
    assert page.status_code == 200
    assert "customer A" in page.text


def test_store_page_has_its_own_force_channel_editor(client):
    store = _store("Channelled store")
    page = client.get(f"/admin/stores/{store['id']}", headers=_auth())
    assert page.status_code == 200
    assert "আলাদা চ্যানেল" in page.text


def test_store_force_channel_saved_from_the_site(client, monkeypatch):
    from app.services import channels, forcejoin
    store = _store("Scoped store")

    async def fake_add(ref, store_id=None, title=""):
        return {"ok": True, "id": 1, "chat_id": -100555, "title": "Store channel"}
    monkeypatch.setattr(channels, "add_channel", fake_add)
    response = client.post(f"/admin/stores/{store['id']}/forcejoin", headers=_auth(),
                           data={"ref": "@store_channel"}, follow_redirects=False)
    assert response.status_code == 303
    assert db.store_forcejoin(store["id"]) == "@store_channel"
    assert "@store_channel" in {t["ref"] for t in forcejoin.targets(store["id"])}


# ---------------------------------------------------- new public pages (v3.1)
def test_empty_site_never_shows_a_broken_page(client):
    """A fresh deploy has no stores at all — the site must still look sane."""
    page = client.get("/")
    assert page.status_code == 200
    assert "এখনো কোনো স্টোর নেই" in page.text
    assert client.get("/stores").status_code == 200
    assert client.get("/search?q=anything").status_code == 200


def test_help_page_answers_the_obvious_questions(client):
    page = client.get("/help")
    assert page.status_code == 200
    for phrase in ("কীভাবে ব্যবহার করবেন", "কেন?", "লিমিট শেষ"):
        assert phrase in page.text


def test_help_page_offers_a_way_to_reach_the_admin(client):
    from app.services import settings
    settings.set("SUPPORT_CONTACT", "@my_support")
    page = client.get("/help")
    assert "https://t.me/my_support" in page.text
    settings.reset("SUPPORT_CONTACT")


def test_home_teaches_the_three_steps(client):
    _store("Free Movies")
    page = client.get("/")
    assert "কীভাবে ফাইল পাবেন" in page.text
    assert "সোজা ভিডিও" in page.text


# ------------------------------------------- per-video broadcast from the panel
def test_per_video_broadcast_from_the_website(client):
    store = _store("Free Movies")
    file_id = db.files_of(store["id"])[0]["id"]
    db.touch_user(USER_ID, "Rahim", "rahim")
    resp = client.post(
        f"/admin/files/broadcast/{file_id}",
        data={"audience": "all", "text": "এই ভিডিওটি আপনার জন্য 🎬", "action": "start",
              "online": "1", "buttons": "🔗 সাইট | https://example.com"},
        headers=_auth(), follow_redirects=False)
    assert resp.status_code == 303
    campaign_id = int(str(resp.headers["location"]).split("?")[0].rsplit("/", 1)[-1])
    campaign = db.campaign(campaign_id)
    assert campaign is not None
    assert campaign["file_ids"] == [file_id]
    assert "ভিডিও" in (campaign["text"] or "")
    assert campaign["buttons"] == "🔗 সাইট | https://example.com"
    assert campaign["online_button"] == 1
    assert USER_ID in db.campaign_user_ids(campaign_id)


def test_campaign_buttons_can_be_turned_off_per_campaign(client):
    """The “online” switch is per campaign, so one broadcast can skip it."""
    store = _store("Free Movies")
    file_id = db.files_of(store["id"])[0]["id"]
    db.touch_user(USER_ID, "Rahim", "rahim")
    resp = client.post(f"/admin/files/broadcast/{file_id}",
                       data={"audience": "all", "text": "x", "action": "start"},
                       headers=_auth(), follow_redirects=False)
    campaign_id = int(str(resp.headers["location"]).split("?")[0].rsplit("/", 1)[-1])
    campaign = db.campaign(campaign_id)
    assert campaign["online_button"] == 0
    from app.services import broadcast as broadcast_service
    labels = [label for row in broadcast_service.campaign_buttons(campaign) for label, _ in row]
    assert not any("অনলাইন" in label for label in labels)


# ------------------------------------------------- 🔐 security / 📝 bot texts / 🗑 purge
def test_security_page_shows_the_leaked_file_and_the_fix(client):
    """The owner pasted a crash log that leaked API_HASH — the panel must say so."""
    page = client.get("/admin/security", headers=_auth())
    assert page.status_code == 200
    assert "নিরাপত্তা" in page.text
    assert "website.example.py" in page.text          # the real finding, on line 71
    assert "Revoke" in page.text                      # step one of the fix
    assert "API_HASH" in page.text


def test_bot_text_overrides_reach_the_bot(client):
    from app import i18n
    from app.services import bot_texts

    assert "স্টোরে স্বাগতম" in i18n.t(USER_ID, "home_banner")
    save = client.post("/admin/texts/save", headers=_auth(),
                       data={"key": "home_banner", "lang": "bn",
                             "value": "আসসালামু আলাইকুম — আমাদের স্টোরে স্বাগতম!"},
                       follow_redirects=False)
    assert save.status_code == 303
    assert "আসসালামু আলাইকুম" in i18n.t(USER_ID, "home_banner")
    assert bot_texts.is_overridden("home_banner")
    assert "Welcome to the store" in bot_texts.render("home_banner", "en")   # en untouched
    # ♻️ reset brings the built-in wording back
    client.post("/admin/texts/save", headers=_auth(),
                data={"key": "home_banner", "lang": "bn", "value": "-"})
    assert "স্টোরে স্বাগতম" in i18n.t(USER_ID, "home_banner")


def test_bot_texts_page_lists_every_group(client):
    page = client.get("/admin/texts", headers=_auth())
    assert page.status_code == 200
    assert "বটের মেসেজ" in page.text
    assert "home_banner" in page.text
    assert "media_only" in page.text                  # not in i18n, still editable


def test_mass_delete_files_and_users(client):
    store = _store("Movies")
    extra = [db.add_file(store["id"], f"Film {i}", "Video", -100, 100 + i)
             for i in range(3)]
    page = client.post("/admin/files/bulk-delete", headers=_auth(),
                       data={"ids": f"{extra[0]}, {extra[1]}"}, follow_redirects=False)
    assert page.status_code == 303
    assert db.file(extra[0]) is None and db.file(extra[1]) is None
    assert db.file(extra[2]) is not None              # untouched

    db.touch_user(777, "Old User", "old")
    db.touch_user(778, "Keep Me", "keep")
    gone = client.post("/admin/users/bulk-delete", headers=_auth(),
                       data={"ids": "777"}, follow_redirects=False)
    assert gone.status_code == 303
    assert db.user(777) is None
    assert db.user(778) is not None
    assert client.get("/admin/users", headers=_auth()).status_code == 200


def test_old_users_can_be_purged_safely(client):
    import time as _time

    db.touch_user(ADMIN_ID, "Owner", "owner")
    db.touch_user(901, "Sleepy", "sleepy")
    db.touch_user(902, "Fresh", "fresh")
    db._run("UPDATE users SET last_seen = ? WHERE user_id = ?",
            (_time.time() - 400 * 86400, 901))         # 400 days ago
    preview = client.get("/admin/users/purge?days=90", headers=_auth())
    assert preview.status_code == 200
    assert "Sleepy" in preview.text and "Fresh" not in preview.text
    client.post("/admin/users/purge", headers=_auth(), data={"days": "90"},
                follow_redirects=False)
    assert db.user(901) is None
    assert db.user(902) is not None
    assert db.user(ADMIN_ID) is not None               # admins are never purged


# ---------------------------------------- hosting panel: the job must open $PORT
def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("entry", ["main.py", "bot.py"])
def test_a_hosted_job_opens_its_web_port(tmp_path, entry):
    """CodeNest/RunSpace runs an entry file and waits for it to listen on $PORT.

    “The job is running, but no web listener yet” is what the panel shows when
    nothing binds that port — so every entry point must serve the website, even
    while the bot itself is unconfigured.
    """
    import os
    import subprocess
    import sys
    import time
    import urllib.request
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    port = _free_port()
    env = dict(os.environ)
    env.pop("BOT_ENV_FILE", None)
    for leaked in ("API_ID", "API_HASH", "BOT_TOKEN", "STRING_SESSION"):  # from config.env
        env.pop(leaked, None)
    env.update({
        "PORT": str(port),
        "WEB_USER": "admin", "WEB_PASS": "demo", "WEB_SECRET": "demo-secret",
        "WEB_TITLE": "Host Test", "DB_FILE": str(tmp_path / "hosted.sqlite3"),
        "BOT_ENV_FILE": str(tmp_path / "empty.env"),        # no real credentials
        "ADMIN_IDS": "999", "WEB_ENABLED": "false",          # a panel only sets PORT
    })
    (tmp_path / "empty.env").write_text("", encoding="utf-8")

    proc = subprocess.Popen([sys.executable, entry], cwd=str(root), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 60
        last_error = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as response:
                    assert response.status == 200
                    break
            except Exception as exc:               # not listening yet
                last_error = str(exc)
                time.sleep(0.4)
        else:
            pytest.fail(f"{entry} never opened port {port} ({last_error})")

        assert proc.poll() is None, f"{entry} exited instead of serving the site"
        request = urllib.request.Request(base + "/", headers={
            "X-Forwarded-Prefix": "/live/u16-b-2576c1"})
        with urllib.request.urlopen(request, timeout=5) as response:
            html = response.read().decode("utf-8", "ignore")
        assert '<base href="/live/u16-b-2576c1/">' in html
        assert 'href="admin"' in html                  # links stay under the prefix
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_the_panel_host_variable_is_followed(tmp_path):
    """RunSpace sets HOST=0.0.0.0 for web jobs — we must not ignore it."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env.pop("BOT_ENV_FILE", None)
    env.pop("WEB_HOST", None)                     # the panel only gives HOST+PORT
    env.update({"HOST": "0.0.0.0", "PORT": "11000"})
    out = subprocess.run([sys.executable, "-c",
                          "from app import config as c; print(c.WEB_HOST, c.WEB_PORT)"],
                         cwd=str(root), env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["0.0.0.0", "11000"]

    env.pop("HOST")
    env["WEB_HOST"] = "127.0.0.1"
    out = subprocess.run([sys.executable, "-c",
                          "from app import config as c; print(c.WEB_HOST)"],
                         cwd=str(root), env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "127.0.0.1"        # an explicit value still wins

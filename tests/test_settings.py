"""Settings service + force-join tests.

Everything the owner can change from the panel/website lives in the `meta`
table; config.env only acts as a fallback. These tests pin down that behaviour,
plus every channel-reference format the force-join gate must accept.
"""
from __future__ import annotations

import pytest

from app import config as cfg, runtime
from app.services import billing, forcejoin, settings, support
from app.handlers import state
from app.storage import db
from tests.test_flows import ADMIN_ID, USER_ID, FakeEvent


@pytest.fixture(autouse=True)
def panel_admin(monkeypatch):
    """Handlers only answer the configured admins — make the test user one."""
    monkeypatch.setattr(cfg, "ADMIN_IDS", [ADMIN_ID])
    monkeypatch.setattr(runtime, "force_join_cache", {})
    monkeypatch.setattr(runtime, "force_join_ok", {})
    state.pending_input.clear()
    state.flow_ctx.clear()
    yield
    settings._cache.clear()

# ------------------------------------------------------------------ precedence
def test_db_value_beats_env_and_default(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "settings.sqlite3"))
    monkeypatch.setattr(cfg, "PAY_BKASH", "01711-000000")     # config.env value
    settings._cache.clear()

    assert settings.get_str("PAY_BKASH") == "01711-000000"
    assert settings.source("PAY_BKASH") == "env"

    assert settings.set("PAY_BKASH", "01999-111111")[0]
    assert settings.get_str("PAY_BKASH") == "01999-111111"    # panel wins
    assert settings.source("PAY_BKASH") == "db"

    settings.reset("PAY_BKASH")                                # back to config.env
    assert settings.get_str("PAY_BKASH") == "01711-000000"
    assert settings.source("PAY_BKASH") == "env"


def test_default_applies_when_nothing_is_set(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "settings.sqlite3"))
    monkeypatch.setattr(cfg, "CURRENCY", "")
    settings._cache.clear()
    assert settings.get_str("CURRENCY", "৳") == "৳"
    assert settings.source("CURRENCY") == "default"


def test_environment_variable_still_works(tmp_path, monkeypatch):
    """Old installs that only have config.env must keep working untouched."""
    db.bind(str(tmp_path / "settings.sqlite3"))
    monkeypatch.setattr(cfg, "TRIAL_HOURS", 48)
    settings._cache.clear()
    assert settings.get_int("TRIAL_HOURS") == 48
    assert billing.trial_hours() == 48


def test_bool_and_number_handling(tmp_path):
    db.bind(str(tmp_path / "settings.sqlite3"))
    settings._cache.clear()
    settings.set("FORCE_JOIN_ENABLED", "0")
    assert settings.get_bool("FORCE_JOIN_ENABLED", True) is False
    settings.set("FORCE_JOIN_ENABLED", "1")
    assert settings.get_bool("FORCE_JOIN_ENABLED", False) is True

    settings.set("BROADCAST_DELAY", "1.5")
    assert settings.get_float("BROADCAST_DELAY", 0.35) == 1.5
    assert settings.set("BROADCAST_DELAY", "-3")[0] is False     # negative refused
    assert settings.set("BROADCAST_BATCH", "ten")[0] is False    # not a number


# ------------------------------------------------------------------- force join
@pytest.mark.parametrize("raw,expected", [
    ("@mychannel", "@mychannel"),
    ("mychannel", "@mychannel"),
    ("https://t.me/mychannel", "@mychannel"),
    ("t.me/mychannel", "@mychannel"),
    ("https://telegram.me/mychannel", "@mychannel"),
    ("https://t.me/mychannel/123", "@mychannel"),
    ("https://t.me/+AbCdEfGhIjKl", "https://t.me/+AbCdEfGhIjKl"),
    ("https://t.me/joinchat/AAAAAEabc123", "https://t.me/+AAAAAEabc123"),
    ("-1001234567890", "-1001234567890"),
])
def test_any_channel_reference_is_accepted(raw, expected):
    assert forcejoin.normalize(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "https://example.com/x"])
def test_garbage_channel_references_are_rejected(raw):
    assert forcejoin.normalize(raw) == ""


def test_setting_force_channel_validates_and_stores(tmp_path):
    db.bind(str(tmp_path / "settings.sqlite3"))
    settings._cache.clear()
    ok, error = settings.set("FORCE_CHANNEL", "https://t.me/+PrivateInvite99")
    assert ok and not error
    assert settings.get_str("FORCE_CHANNEL") == "https://t.me/+PrivateInvite99"
    assert forcejoin.switch() == "https://t.me/+PrivateInvite99"
    assert forcejoin.join_url().startswith("https://t.me/+")

    assert settings.set("FORCE_CHANNEL", "###")[0] is False      # invalid → refused


def test_force_join_can_be_switched_off(tmp_path):
    db.bind(str(tmp_path / "settings.sqlite3"))
    settings._cache.clear()
    settings.set("FORCE_CHANNEL", "@mychannel")
    settings.set("FORCE_JOIN_ENABLED", "0")
    assert forcejoin.switch() == ""                              # gate is open


@pytest.mark.asyncio
async def test_membership_check_fails_open(tmp_path, monkeypatch):
    """No client / unresolvable channel must never lock a paying user out."""
    db.bind(str(tmp_path / "settings.sqlite3"))
    settings._cache.clear()
    settings.set("FORCE_CHANNEL", "@private_channel")

    monkeypatch.setattr(runtime, "_client", None)
    assert await forcejoin.is_member(USER_ID) is None            # could not check

    from app import ui
    event = FakeEvent(USER_ID)
    assert await ui.require_membership(event, USER_ID) is True   # fails open


@pytest.mark.asyncio
async def test_membership_blocks_a_stranger_and_onboards_them(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "settings.sqlite3"))
    settings._cache.clear()
    settings.set("FORCE_CHANNEL", "https://t.me/+PrivateInvite99")
    settings.set("FORCE_JOIN_NOTE", "জয়েন করে আবার /start দিন")
    runtime.force_join_cache.clear()
    runtime.force_join_ok.clear()

    async def not_a_member(_user_id):
        return False

    monkeypatch.setattr(forcejoin, "is_member", not_a_member)
    from app import ui
    event = FakeEvent(USER_ID)
    assert await ui.require_membership(event, USER_ID) is False
    assert event.sent, "the user must be told to join"
    text, buttons = event.sent[0]
    assert "জয়েন" in text
    url = getattr(getattr(buttons[0][0], "type", None), "url", None)
    assert url == "https://t.me/+PrivateInvite99"      # private invite link works


# --------------------------------------------------------- used by the features
def test_payment_screen_follows_the_panel_numbers(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "settings.sqlite3"))
    settings._cache.clear()
    for key in ("PAY_BKASH", "PAY_NAGAD", "PAY_ROCKET"):
        monkeypatch.setattr(cfg, key, "")
    settings.set("PAY_BKASH", "01712-345678")
    settings.set("CURRENCY", "₹")
    assert billing.payment_methods() == [("bKash", "01712-345678")]
    assert billing.price_text(199) == "₹199"
    assert "01712-345678" in billing.methods_text()


def test_support_contact_and_cooldown_come_from_settings(tmp_path):
    db.bind(str(tmp_path / "settings.sqlite3"))
    settings._cache.clear()
    settings.set("SUPPORT_CONTACT", "@myhelp")
    settings.set("SUPPORT_NOTE", "10:00-22:00")
    settings.set("TICKET_COOLDOWN", 120)
    assert support.support_line() == "@myhelp · 10:00-22:00"
    support._last_ticket.clear()
    support._last_ticket[USER_ID] = __import__("time").time()
    assert 0 < support.cooldown_left(USER_ID) <= 120


def test_export_produces_a_config_env(tmp_path):
    db.bind(str(tmp_path / "settings.sqlite3"))
    settings._cache.clear()
    settings.set("FORCE_CHANNEL", "@mychannel")
    text = settings.env_preview()
    assert "FORCE_CHANNEL=@mychannel" in text
    assert "PAY_BKASH=" in text


def test_registry_is_complete():
    """Every featured setting is editable and grouped."""
    for key in ("FORCE_CHANNEL", "PAY_BKASH", "SUPPORT_CONTACT", "CUSTOM_CAPTION",
                "BROADCAST_DELAY", "WEB_TITLE", "TZ_OFFSET", "DEFAULT_LANG"):
        item = settings.setting(key)
        assert item is not None, f"{key} is missing from the registry"
        assert item.label and settings.group_of(key) is not None


# ------------------------------------------------------------------- bot panel
@pytest.mark.asyncio
async def test_settings_screen_lists_all_groups(tmp_path):
    db.bind(str(tmp_path / "settings.sqlite3"))
    from app.handlers.router import dispatch

    event = FakeEvent(ADMIN_ID, data=b"adm:setmenu")
    await dispatch(event)
    text, buttons = event.edits[-1]
    labels = [button.text for row in buttons for button in row]
    assert any("চ্যানেল" in label for label in labels)
    assert any("পেমেন্ট" in label for label in labels)


@pytest.mark.asyncio
async def test_admin_can_edit_a_setting_from_the_bot(tmp_path):
    db.bind(str(tmp_path / "settings.sqlite3"))
    settings._cache.clear()
    from app.handlers.messages import pending_input
    from app.handlers.router import dispatch

    event = FakeEvent(ADMIN_ID, data=b"adm:setk:payments:PAY_BKASH")
    await dispatch(event)
    assert state.peek(ADMIN_ID)["action"] == "setting_edit"

    event = FakeEvent(ADMIN_ID, text="01812-999888")
    await pending_input(event)
    assert settings.get_str("PAY_BKASH") == "01812-999888"
    assert event.sent, "the admin should get a confirmation"


@pytest.mark.asyncio
async def test_invalid_channel_input_is_refused_in_the_bot(tmp_path, monkeypatch):
    db.bind(str(tmp_path / "settings.sqlite3"))
    monkeypatch.setattr(cfg, "FORCE_CHANNEL", "")            # ignore config.env
    settings._cache.clear()
    from app.handlers.messages import pending_input

    state.ask(ADMIN_ID, "setting_edit", group="force_join", key="FORCE_CHANNEL")
    event = FakeEvent(ADMIN_ID, text="### not a channel ###")
    await pending_input(event)
    assert settings.get_str("FORCE_CHANNEL") == ""           # nothing saved
    assert state.peek(ADMIN_ID) is not None                  # still waiting
    assert any("চেনা গেল না" in (sent[0] or "") for sent in event.sent)


@pytest.mark.asyncio
async def test_toggle_button_flips_a_boolean(tmp_path):
    db.bind(str(tmp_path / "settings.sqlite3"))
    settings._cache.clear()
    settings.set("FORCE_JOIN_ENABLED", "1")
    from app.handlers.router import dispatch

    event = FakeEvent(ADMIN_ID, data=b"adm:setk:force_join:FORCE_JOIN_ENABLED")
    await dispatch(event)
    assert settings.get_bool("FORCE_JOIN_ENABLED", True) is False
    assert any("বন্ধ" in (answer[0] or "") for answer in event.answers)

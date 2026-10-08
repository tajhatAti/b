"""One place for every setting the owner can change — from the bot *or* the website.

Why this exists
    Values like the payment numbers, the force-join channel or the support
    contact used to live only in `config.env`, so changing them meant editing a
    file and restarting the hosting. Now they live in the database:

        website /admin/settings  ─┐
        bot  ⚙️ Settings screen  ─┼──►  settings.set("PAY_BKASH", "017…")
        (first boot / export)    ─┘     stored in the `meta` table

Precedence (highest wins)
    1. a value saved from the panel / website   → `meta` key `set:<NAME>`
    2. the environment variable / `config.env`  → the old behaviour, still works
    3. the built-in default below

So an existing `config.env` keeps working exactly as before, and anything the
owner saves in the UI simply overrides it — no restart, no file editing.
`settings.source("PAY_BKASH")` tells the UI which of the three is in play.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app import config as cfg
from app.logger import log
from app.storage import db

META_PREFIX = "set:"
CACHE_TTL = 10.0                      # seconds — keeps hot reads (caption, tz) cheap


@dataclass(frozen=True)
class Setting:
    key: str
    label: str                        # বাংলা নাম (প্যানেলে যা দেখাবে)
    group: str
    kind: str = "text"                # text | long | int | float | bool
    hint: str = ""
    placeholder: str = ""
    default: Any = ""
    env: str = ""                     # equivalent environment variable name


@dataclass
class Group:
    key: str
    label: str
    icon: str = "⚙️"
    blurb: str = ""
    settings: list[Setting] = field(default_factory=list)


def _s(key: str, label: str, group: str, **kwargs) -> Setting:
    return Setting(key=key, label=label, group=group, env=key, **kwargs)


# ------------------------------------------------------------------- registry
REGISTRY: list[Group] = [
    Group(
        key="force_join", label="চ্যানেল জয়েন (Force Join)", icon="📢",
        blurb="ইউজার ফাইল পেতে আগে চ্যানেলে জয়েন করতে হবে। প্রাইভেট ইনভাইট লিংক, "
              "@username বা চ্যানেল আইডি — যেকোনোটা চলবে।",
        settings=[
            _s("FORCE_JOIN_ENABLED", "জয়েন বাধ্যতামূলক", "force_join", kind="bool",
               default=True, hint="বন্ধ করলে কেউ আটকাবে না"),
            _s("FORCE_CHANNEL", "চ্যানেল (লিংক / @username / আইডি)", "force_join",
               placeholder="@mychannel অথবা https://t.me/+AbCdEfGh",
               hint="প্রাইভেট চ্যানেল হলে ইনভাইট লিংক দিন — বট সেই লিংক থেকে চ্যানেল খুঁজে নেবে"),
            _s("FORCE_JOIN_NOTE", "জয়েন করার সময় ইউজারকে যা দেখাবে", "force_join",
               kind="long", hint="যেমন: জয়েন করে আবার /start দিন"),
        ],
    ),
    Group(
        key="payments", label="পেমেন্ট", icon="💳",
        blurb="এই নাম্বারগুলো ইউজার প্রিমিয়াম কেনার সময় দেখবে। যেটা খালি রাখবেন সেটা "
              "লুকানো থাকবে।",
        settings=[
            _s("CURRENCY", "মুদ্রা প্রতীক", "payments", default="৳", placeholder="৳ / ₹ / $"),
            _s("PAY_BKASH", "bKash", "payments", placeholder="01712-345678 (Personal)"),
            _s("PAY_NAGAD", "Nagad", "payments", placeholder="01812-345678 (Personal)"),
            _s("PAY_ROCKET", "Rocket", "payments", placeholder="017123456789-1"),
            _s("PAY_UPI", "UPI / অন্যান্য", "payments", placeholder="yourname@upi"),
            _s("PAY_CRYPTO", "Crypto", "payments", placeholder="USDT TRC20: T…"),
            _s("PAY_NOTE", "পেমেন্ট নির্দেশনা (ঐচ্ছিক)", "payments", kind="long",
               hint="যেমন: Send Money করে ট্রানজেকশন আইডি পাঠান — স্ক্রিনশটও চলবে"),
            _s("TRIAL_HOURS", "ফ্রি ট্রায়াল (ঘণ্টা)", "payments", kind="int", default=24,
               hint="0 দিলে ট্রায়াল বন্ধ"),
            _s("AUTO_APPROVE_ZERO", "ফ্রি প্ল্যান অটো-অ্যাপ্রুভ", "payments", kind="bool",
               default=True),
        ],
    ),
    Group(
        key="support", label="সাপোর্ট / যোগাযোগ", icon="📞",
        blurb="ইউজার “📞 অ্যাডমিনের সাথে কথা বলুন” চাপলে যে তথ্য দেখবে (মেসেজ সবসময় "
              "বটেই আসে, তাই কিছু সেট না করলেও কাজ করে)।",
        settings=[
            _s("SUPPORT_CONTACT", "কন্টাক্ট (যেকোনো কিছু)", "support",
               placeholder="@myusername / 01712-345678 / https://t.me/mychat"),
            _s("SUPPORT_NOTE", "সাপোর্ট সময়/নির্দেশনা", "support",
               hint="যেমন: সকাল ১০টা – রাত ১০টা"),
            _s("TICKET_COOLDOWN", "একই ইউজার কত সেকেন্ড পর আবার মেসেজ দিতে পারবে",
               "support", kind="int", default=60),
        ],
    ),
    Group(
        key="content", label="কনটেন্ট ও ভাষা", icon="📝",
        blurb="ফাইল পাঠানোর সময়ের ক্যাপশন, স্বাগতম বার্তা ও ডিফল্ট ভাষা।",
        settings=[
            _s("CUSTOM_CAPTION", "প্রতি ফাইলের ক্যাপশন", "content", kind="long",
               hint="{name} দিলে ফাইলের নাম বসবে"),
            _s("DEFAULT_LANG", "ডিফল্ট ভাষা", "content", default="bn",
               placeholder="bn অথবা en"),
        ],
    ),
    Group(
        key="broadcast", label="ব্রডকাস্ট গতি", icon="📣",
        blurb="ধীর গতি = অ্যাকাউন্ট নিরাপদ; দ্রুত গতি = সময় কম।",
        settings=[
            _s("BROADCAST_DELAY", "মেসেজের মাঝে বিরতি (সেকেন্ড)", "broadcast",
               kind="float", default=0.35),
            _s("BROADCAST_BATCH", "কত মেসেজ পরপর লম্বা বিরতি", "broadcast",
               kind="int", default=25),
            _s("BROADCAST_BATCH_PAUSE", "লম্বা বিরতি (সেকেন্ড)", "broadcast",
               kind="float", default=3.0),
            _s("BROADCAST_MAX_FILES", "এক মেসেজে সর্বোচ্চ ফাইল", "broadcast",
               kind="int", default=5),
            _s("BROADCAST_AUTO_RESUME", "রিস্টার্টের পর অসমাপ্ত ব্রডকাস্ট চালু করবে",
               "broadcast", kind="bool", default=True),
        ],
    ),
    Group(
        key="site", label="ওয়েবসাইট", icon="🌐",
        blurb="পাবলিক সাইটের নাম, ট্যাগলাইন ও টাইমজোন।",
        settings=[
            _s("WEB_TITLE", "সাইটের নাম", "site", default="Store"),
            _s("SITE_TAGLINE", "সাইটের ট্যাগলাইন", "site", kind="long"),
            _s("SITE_CONTACT", "সাইটে দেখানোর যোগাযোগ", "site"),
            _s("TZ_OFFSET", "টাইমজোন (UTC থেকে ঘণ্টা)", "site", kind="float",
               default=6.0, hint="বাংলাদেশ = 6"),
        ],
    ),
]

_BY_KEY: dict[str, Setting] = {s.key: s for g in REGISTRY for s in g.settings}
_GROUPS: dict[str, Group] = {g.key: g for g in REGISTRY}
_cache: dict[str, tuple[float, Any, Any]] = {}     # key -> (expiry, value, env snapshot)


def setting(key: str) -> Setting | None:
    return _BY_KEY.get(key)


def keys() -> list[str]:
    return list(_BY_KEY)


def group_of(key: str) -> Group | None:
    item = _BY_KEY.get(key)
    return _GROUPS.get(item.group) if item else None


def all_groups() -> list[Group]:
    return REGISTRY


# ------------------------------------------------------------------ coercion
def _env_value(key: str, default: Any) -> Any:
    raw = getattr(cfg, key, None)
    return default if raw in (None, "") else raw


def _coerce(item: Setting, raw: Any) -> Any:
    if isinstance(raw, str):
        raw = raw.strip()
    if item.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on", "ha", "hae", "চালু"}
    if item.kind == "int":
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return int(item.default or 0)
    if item.kind == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float(item.default or 0)
    return "" if raw is None else str(raw)


# ---------------------------------------------------------------------- read
def get(key: str, default: Any = None) -> Any:
    """DB override → environment → built-in default (caller default wins last)."""
    item = _BY_KEY.get(key)
    if item is None:
        return default
    fallback = item.default if default is None else default
    env_now = _env_value(key, fallback)
    cached = _cache.get(key)
    # The cache also remembers the environment value it was built from, so a
    # changed env (tests, or a real env edit) is picked up immediately.
    if cached and cached[0] > time.time() and cached[2] == env_now:
        return cached[1]

    stored = db.get_meta(META_PREFIX + key, "")
    if stored != "":
        value = _coerce(item, stored)
    else:
        value = _coerce(item, env_now)
    _cache[key] = (time.time() + CACHE_TTL, value, env_now)
    return value


def get_str(key: str, default: str = "") -> str:
    value = get(key, default)
    return "" if value is None else str(value)


def get_int(key: str, default: int = 0) -> int:
    return _coerce(_BY_KEY[key], get(key, default)) if key in _BY_KEY else int(default)


def get_float(key: str, default: float = 0.0) -> float:
    return _coerce(_BY_KEY[key], get(key, default)) if key in _BY_KEY else float(default)


def get_bool(key: str, default: bool = False) -> bool:
    return bool(_coerce(_BY_KEY[key], get(key, default))) if key in _BY_KEY else default


def source(key: str) -> str:
    """`db` (panel), `env` (config.env) or `default` — shown in the UI."""
    item = _BY_KEY.get(key)
    if item is None:
        return "unknown"
    if db.get_meta(META_PREFIX + key, "") != "":
        return "db"
    if getattr(cfg, key, "") not in (None, ""):
        return "env"
    return "default"


def as_dict(group: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in _BY_KEY.items():
        if group and item.group != group:
            continue
        out[key] = get(key)
    return out


# ---------------------------------------------------------------------- write
def validate(key: str, raw: Any) -> tuple[bool, Any, str]:
    """Return (ok, cleaned_value, error_message)."""
    item = _BY_KEY.get(key)
    if item is None:
        return False, raw, "অজানা সেটিং"
    value = str(raw or "").strip()
    if item.kind == "int":
        if value and not value.lstrip("-").isdigit():
            return False, raw, "সংখ্যা দিন"
        value = int(value) if value else 0
        if item.key in ("TRIAL_HOURS", "TICKET_COOLDOWN", "BROADCAST_BATCH",
                        "BROADCAST_MAX_FILES", "BROADCAST_BATCH_PAUSE") and value < 0:
            return False, raw, "ঋণাত্মক সংখ্যা চলবে না"
        return True, value, ""
    if item.kind == "float":
        try:
            number = float(value) if value else float(item.default or 0)
        except ValueError:
            return False, raw, "সংখ্যা দিন"
        if number < 0:
            return False, raw, "ঋণাত্মক সংখ্যা চলবে না"
        return True, number, ""
    if item.kind == "bool":
        return True, _coerce(item, value), ""
    if item.key == "FORCE_CHANNEL" and value:
        from app.services import forcejoin
        cleaned = forcejoin.normalize(value)
        if not cleaned:
            return False, raw, "চ্যানেল চেনা গেল না — @username বা t.me লিংক দিন"
        return True, cleaned, ""
    if item.key == "DEFAULT_LANG" and value and value not in ("bn", "en"):
        return False, raw, "bn অথবা en দিন"
    return True, value, ""


def set(key: str, value: Any, *, validate_first: bool = True) -> tuple[bool, str]:
    """Save a setting (empty value clears the override → back to env/default)."""
    item = _BY_KEY.get(key)
    if item is None:
        return False, "অজানা সেটিং"
    if validate_first:
        ok, cleaned, error = validate(key, value)
        if not ok:
            return False, error
        value = cleaned
    text = "" if value is None else str(value)
    if item.kind == "bool":
        text = "true" if _coerce(item, value) else "false"
    if text.strip() == "" and item.kind not in ("bool",):
        db.set_meta(META_PREFIX + key, "")
    else:
        db.set_meta(META_PREFIX + key, text)
    _cache.pop(key, None)
    log.info("Setting %s updated from the panel", key)
    return True, ""


def set_many(values: dict[str, Any]) -> dict[str, str]:
    """Save a whole form; returns {key: error} for the fields that failed."""
    errors: dict[str, str] = {}
    for key, raw in values.items():
        ok, message = set(key, raw)
        if not ok:
            errors[key] = message
    return errors


def reset(key: str) -> None:
    """Forget the panel value — the environment value (if any) applies again."""
    db.set_meta(META_PREFIX + key, "")
    _cache.pop(key, None)


def env_preview() -> str:
    """A ready-to-paste `config.env` snippet of the current values."""
    lines = ["# নিজের মানগুলো (প্যানেল থেকে এক্সপোর্ট করা)", ""]
    last_group = ""
    for group in REGISTRY:
        lines.append(f"# ---------- {group.label} ----------")
        for item in group.settings:
            value = get(item.key)
            if item.kind == "bool":
                value = "true" if value else "false"
            if value == "" and item.default == "":
                lines.append(f"# {item.key}=")
            else:
                lines.append(f"{item.key}={value}")
        lines.append("")
        last_group = group.key
    return "\n".join(lines).rstrip() + "\n"

"""Force-join: accept *any* channel reference — @username, t.me link or private invite.

Admins kept asking for this: “প্রাইভেট লিংক বা এনি ইউজারনেম যে কোন কিছুই দেওয়া যাবে”.
So one module handles every shape:

    @mychannel
    mychannel
    https://t.me/mychannel
    https://t.me/+AbCdEfGhIj          ← private invite link
    https://t.me/joinchat/AAAAAE…     ← old style invite
    -1001234567890                    ← numeric channel id

Resolution order: the bot itself, then any connected userbot session (needed
because *bots* cannot always open a private invite link — a user account can).
Everything is cached in `meta` so the membership check stays fast.

Safety rule that never changes: if we cannot verify membership (wrong link, bot
not in the channel, Telegram hiccup) we let the user through. Locking paying
customers out of a whole store is far worse than skipping one gate.
"""
from __future__ import annotations

import re
import time

from app import runtime
from app.logger import log
from app.services import settings
from app.services.telegram import safe_call
from app.storage import db

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
INVITE_RE = re.compile(r"^(?:\+|joinchat/)([A-Za-z0-9_-]{8,})$")
META_ID = "force_channel_id"
META_TITLE = "force_channel_title"
META_KIND = "force_channel_kind"

_cache: dict[str, tuple[float, dict]] = {}


# --------------------------------------------------------------------- parsing
def normalize(raw: str) -> str:
    """Return the canonical form of whatever the admin typed ("" if unusable)."""
    parsed = parse(raw)
    if parsed["kind"] == "username":
        return "@" + parsed["value"]
    if parsed["kind"] == "invite":
        return "https://t.me/+" + parsed["value"]
    if parsed["kind"] == "id":
        return parsed["value"]
    return ""


def parse(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        return {"kind": "none", "value": ""}

    # t.me/<something> or telegram.me links
    text = re.sub(r"^https?://", "", text, flags=re.I)
    text = re.sub(r"^telegram\.(me|dog)/", "", text, flags=re.I)
    text = re.sub(r"^t\.me/", "", text, flags=re.I)

    if text.startswith("-") and text.lstrip("-").isdigit():
        return {"kind": "id", "value": text}

    text = text.split("?")[0].strip("/")
    if text.startswith("@"):
        text = text[1:]
    if not text:
        return {"kind": "none", "value": ""}

    invite = INVITE_RE.match(text)
    if invite:
        return {"kind": "invite", "value": invite.group(1)}
    if text.lower() in ("off", "none", "disable", "no", "বন্ধ"):
        return {"kind": "off", "value": ""}
    if "/" in text:                       # t.me/name/123 → take the first part
        text = text.split("/")[0]
    if USERNAME_RE.match(text):
        return {"kind": "username", "value": text}
    return {"kind": "none", "value": text}


def switch() -> str:
    """`@name`, invite URL or empty — used by the membership check."""
    if not settings.get_bool("FORCE_JOIN_ENABLED", True):
        return ""
    return normalize(settings.get_str("FORCE_CHANNEL"))


def is_configured() -> bool:
    return bool(switch())


def join_url(ref: str | None = None) -> str:
    """The link shown to users (private channels keep their invite link)."""
    parsed = parse(ref if ref is not None else settings.get_str("FORCE_CHANNEL"))
    if parsed["kind"] == "username":
        return f"https://t.me/{parsed['value']}"
    if parsed["kind"] == "invite":
        return f"https://t.me/+{parsed['value']}"
    if parsed["kind"] == "id":
        stored = db.get_meta("force_channel_invite", "")
        return stored
    return ""


def note() -> str:
    return settings.get_str("FORCE_JOIN_NOTE") or ""


def _cached() -> str:
    return db.get_meta(META_ID, "")


# ------------------------------------------------------------------ resolution
async def resolve(ref: str | None = None, *, refresh: bool = False,
                  use_cache: bool = True) -> dict:
    """Work out the chat id + title for the configured channel.

    Returns: {ok, chat_id, title, kind, ref, error, via}
    """
    target = switch() if ref is None else normalize(ref)
    if not target:
        return {"ok": False, "chat_id": 0, "title": "", "kind": "off",
                "ref": "", "error": "কোনো চ্যানেল সেট করা নেই", "via": ""}

    cache_key = f"resolve:{target}"
    hit = _cache.get(cache_key)
    if hit and use_cache and not refresh and hit[0] > time.time() and hit[1].get("ok"):
        return hit[1]

    parsed = parse(target)
    bot_client = runtime.get_client()
    attempts: list[tuple[str, object]] = []
    if bot_client is not None:
        attempts.append(("bot", bot_client))
    for admin_id, client in list(runtime.user_clients.items()):
        attempts.append((f"session:{admin_id}", client))

    error = ""
    for name, client in attempts:
        try:
            entity = await safe_call(client.get_entity, target, what="force_join_entity",
                                     retries=1, raise_after_retries=False)
            if entity is None:
                continue
            chat_id = getattr(entity, "id", 0)
            if getattr(entity, "megagroup", False) or hasattr(entity, "broadcast"):
                title = getattr(entity, "title", "") or ""
            else:
                title = getattr(entity, "title", "") or getattr(entity, "username", "")
            result = {"ok": True, "chat_id": chat_id, "title": title,
                      "kind": parsed["kind"], "ref": target, "error": "", "via": name}
            db.set_meta(META_ID, str(chat_id))
            db.set_meta(META_TITLE, title)
            db.set_meta(META_KIND, parsed["kind"])
            if parsed["kind"] == "invite":
                db.set_meta("force_channel_invite", join_url(target))
            _cache[cache_key] = (time.time() + 600, result)
            log.info("Force-join channel resolved as %s (%s) via %s", title, chat_id, name)
            return result
        except Exception as exc:
            error = str(exc)[:200]
            log.debug("force-join resolve via %s failed: %s", name, exc)

    return {"ok": False, "chat_id": 0, "title": "", "kind": parsed["kind"],
            "ref": target, "via": "",
            "error": error or "চ্যানেল পাওয়া যায়নি — লিংক ঠিক আছে কিনা বা বট/সেশন "
                              "চ্যানেলে আছে কিনা দেখুন"}


async def channel_id() -> object | None:
    """Cached chat id (or the raw username when we could not resolve it yet)."""
    cached = _cached()
    if cached:
        try:
            return int(cached)
        except ValueError:
            pass
    result = await resolve(use_cache=False)
    if result.get("ok"):
        return result["chat_id"]
    target = switch()
    if parse(target)["kind"] == "username":
        return target                       # GetParticipant accepts @username too
    return None


async def ensure_invite_link() -> str:
    """Try to create/read an invite link for a private channel (for the panel)."""
    ref = switch()
    url = join_url(ref)
    if url:
        return url
    peer = await channel_id()
    if peer is None:
        return ""
    for client in [runtime.get_client(), *list(runtime.user_clients.values())]:
        if client is None:
            continue
        try:
            from telethon.tl import functions
            invite = await safe_call(client(functions.messages.ExportChatInviteRequest(peer)),
                                     what="export_invite", retries=1,
                                     raise_after_retries=False)
            link = getattr(invite, "link", "") if invite else ""
            if link:
                db.set_meta("force_channel_invite", link)
                return link
        except Exception as exc:
            log.debug("invite link export failed: %s", exc)
    return ""


async def is_member(user_id: int) -> bool | None:
    """True = joined, False = not joined, None = could not check (fail open)."""
    from telethon.errors import UserNotParticipantError
    from telethon.tl.functions.channels import GetParticipantRequest

    ref = switch()
    if not ref:
        return True
    client = runtime.get_client()
    if client is None:
        return None
    peer = await channel_id()
    if peer is None:
        log.debug("force-join: channel not resolved — skipping the gate")
        return None
    try:
        await safe_call(client(GetParticipantRequest(channel=peer, user_id=user_id)),
                        what="force_join", retries=1, raise_after_retries=False)
        return True
    except UserNotParticipantError:
        return False
    except Exception as exc:
        text = str(exc).lower()
        if "not a participant" in text or "participant" in text and "not" in text:
            return False
        log.debug("force-join check skipped: %s", exc)
        return None


def clear_cache() -> None:
    _cache.clear()
    for key in ("force_join_cache",):
        getattr(runtime, key, {}).clear()
    runtime.force_join_ok.clear()

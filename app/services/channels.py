"""Channel management — browse the channels the bot can reach and post to them.

The panel (website *and* bot) can:

* list every channel where the bot has posting rights (`list_channels()`), which
  mixes three sources: channels the bot scanned before, force-join channels and
  channels the admin registered by hand (`channels` table);
* add a channel with any reference — `@username`, `https://t.me/+invite`,
  `-100…` id, or by **forwarding a post from it**;
* publish a message: text and/or one or more stored files, with inline URL
  buttons (“🎯 delivery”) — the same syntax as a broadcast;
* preview, and read back the message id so the panel can link to the post.
"""
from __future__ import annotations

import time

from app import runtime
from app.logger import log
from app.services import forcejoin, settings
from app.services.sender import FLOOD, deliver, send_text
from app.services.telegram import safe_call
from app.storage import db
from app.utils import button_rows, parse_button_spec


def _admin_ids() -> list[int]:
    from app import config as cfg
    return list(cfg.ADMIN_IDS)


async def list_channels(refresh: bool = False) -> list[dict]:
    """Every channel the bot may post to, with its source and title."""
    rows: list[dict] = []
    seen: set[str] = set()

    for row in db.join_channels():
        key = str(row.get("chat_id") or row.get("ref"))
        if key in seen:
            continue
        seen.add(key)
        rows.append({"id": row["id"], "chat_id": row.get("chat_id"),
                     "ref": row.get("ref"), "title": row.get("title") or row.get("ref"),
                     "source": "force-join", "error": row.get("error") or "",
                     "store_id": row.get("store_id")})

    for chat_id in db.bot_chats():
        row = db.get_meta(f"chat_title:{chat_id}", "")
        if str(chat_id) in seen:
            continue
        seen.add(str(chat_id))
        rows.append({"id": 0, "chat_id": chat_id, "ref": str(chat_id),
                     "title": row or str(chat_id), "source": "bot", "error": "",
                     "store_id": None})

    for row in db.all_stores():
        raw = (row.get("forcejoin") or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        rows.append({"id": 0, "chat_id": None, "ref": raw,
                     "title": f"{row['name']} (স্টোর চ্যানেল)", "source": "store",
                     "error": "", "store_id": row["id"]})

    if refresh:
        for row in rows:
            await _resolve_row(row)
    return sorted(rows, key=lambda r: (r["source"] != "force-join", str(r["title"])))


async def _resolve_row(row: dict) -> dict:
    if row.get("chat_id"):
        return row
    target = await forcejoin.resolve(row.get("ref") or "", refresh=True, use_cache=False)
    if target.get("ok"):
        row["chat_id"] = target["chat_id"]
        row["title"] = target.get("title") or row["title"]
        row["error"] = ""
        db.set_meta(f"chat_title:{target['chat_id']}", row["title"])
    else:
        row["error"] = str(target.get("error") or "")[:180]
    return row


async def add_channel(ref: str, store_id: int | None = None, title: str = "") -> dict:
    """Register a channel + work out its id (accepts any link shape)."""
    cleaned = forcejoin.normalize(ref)
    if not cleaned:
        return {"ok": False, "error": "লিংক/ইউজারনেম চেনা গেল না"}
    existing = [row for row in db.join_channels() if forcejoin.normalize(row["ref"]) == cleaned
                and row.get("store_id") == store_id]
    if existing:
        return {"ok": True, "id": existing[0]["id"], "chat_id": existing[0].get("chat_id"),
                "title": existing[0].get("title") or cleaned, "already": True}
    resolved = await forcejoin.resolve(cleaned, refresh=True, use_cache=False)
    chat_id = resolved.get("chat_id") if resolved.get("ok") else None
    name = resolved.get("title") if resolved.get("ok") else (title or cleaned)
    channel_id = db.add_join_channel(store_id, cleaned, title=name or cleaned,
                                     chat_id=chat_id, kind=forcejoin.parse(cleaned)["kind"],
                                     invite=forcejoin.join_url(cleaned))
    if chat_id:
        db.set_meta(f"chat_title:{chat_id}", name or cleaned)
    return {"ok": bool(chat_id), "id": channel_id, "chat_id": chat_id,
            "title": name or cleaned, "error": "" if chat_id else
            (resolved.get("error") or "চ্যানেল পাওয়া যায়নি")}


async def publish(target: str | int, *, text: str = "", file_ids: list[int] | None = None,
                  buttons: str = "", store_id: int | None = None,
                  parse_buttons: bool = True, link_limit: int = -1) -> dict:
    """Post text and/or files to a channel with inline buttons.

    Returns {ok, message_id, sent, failed, error}. Used by the channel composer,
    the per-store “share to channel” button and the website panel.
    """
    peer = target
    if isinstance(target, str) and target.strip().lstrip("-").isdigit():
        peer = int(target)
    file_ids = [int(f) for f in (file_ids or []) if f]
    spec = (buttons or "").strip()
    if not spec and parse_buttons:
        spec = settings.get_str("BROADCAST_DEFAULT_BUTTONS")
    rows = parse_button_spec(spec)
    # “একবার খোলার লিংক”: post to the channel with a limited deep link instead of
    # the media itself, so the click limit can actually be enforced.
    link_mode = bool(file_ids) and link_limit >= 0
    if link_mode:
        limit = link_limit or settings.get_int("LINK_DEFAULT_LIMIT", 100)
        token = db.create_link(0, file_ids, None, kind="limited", max_clicks=limit,
                               note="channel post")
        if runtime.bot_username:
            url = f"https://t.me/{runtime.bot_username}?start=t{token}"
            label = (f"🔓 ভিডিও নিন ({limit} বার)" if limit else "🔓 ভিডিও নিন")
            rows.insert(0, [(label, url)])
    footer = settings.get_str("CHANNEL_POST_FOOTER")
    body = text or ""
    if footer:
        body = f"{body}\n\n{footer}" if body else footer
    markup = button_rows(rows) or None
    if not body and not file_ids:
        return {"ok": False, "error": "খালি পোস্ট পাঠানো যাবে না", "message_id": 0}

    # In link mode the channel gets the button only; the files stay behind the bot.
    if link_mode:
        file_ids = []

    first_id = 0
    sent = failed = 0
    try:
        if file_ids:
            for index, file_id in enumerate(file_ids):
                row = db.file(file_id)
                if row is None:
                    failed += 1
                    continue
                caption = body if index == 0 else (runtime.caption() or "")
                result = await deliver(peer, row, caption, buttons=rows or None)
                if result.ok:
                    sent += 1
                else:
                    failed += 1
                    log.warning("channel post of file %s failed: %s", file_id, result.reason)
            if sent:
                db.log_event("channel_post", None, store_id,
                             file_ids[0] if file_ids else None, str(peer),
                             f"sent={sent} failed={failed}")
            return {"ok": sent > 0, "sent": sent, "failed": failed, "message_id": first_id,
                    "error": "" if sent else "কোনো ফাইল পাঠানো যায়নি"}
        message = await send_text(peer, body, buttons=rows or None)
        if message is None:
            return {"ok": False, "error": "পোস্ট পাঠানো যায়নি (বট কি চ্যানেলে অ্যাডমিন?)",
                    "message_id": 0}
        first_id = int(getattr(message, "id", 0) or 0)
        db.log_event("channel_post", None, store_id, None, str(peer), "text")
        return {"ok": True, "sent": 1, "failed": 0, "message_id": first_id, "error": ""}
    except Exception as exc:
        log.warning("channel publish failed for %s: %s", peer, exc)
        return {"ok": False, "error": str(exc)[:200], "message_id": 0}


async def channel_info(target: str | int) -> dict:
    """Title + member count, for the browse screen."""
    from app.runtime import bot
    try:
        entity = await safe_call(bot.get_entity, target, what="channel_info", retries=1,
                                 raise_after_retries=False)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:150]}
    if entity is None:
        return {"ok": False, "error": "চ্যানেল পাওয়া যায়নি"}
    count = getattr(entity, "participants_count", None)
    if count:
        count = int(count)
    else:
        try:
            from telethon.tl.functions.channels import GetFullChannelRequest
            full = await safe_call(bot(GetFullChannelRequest(entity)), what="channel_full",
                                   retries=1, raise_after_retries=False)
            count = int(getattr(getattr(full, "full_chat", None), "participants_count", 0) or 0)
        except Exception:
            count = 0
    kind = "channel" if getattr(entity, "broadcast", False) else (
        "group" if getattr(entity, "megagroup", False) else "chat")
    return {"ok": True, "chat_id": getattr(entity, "id", 0),
            "title": getattr(entity, "title", "") or getattr(entity, "username", ""),
            "username": getattr(entity, "username", "") or "",
            "members": count, "kind": kind}


async def postable_channels() -> list[dict]:
    """Channels where the bot is an admin (it can actually publish there)."""
    from app.runtime import bot
    out = []
    for row in await list_channels():
        peer = row.get("chat_id") or row.get("ref")
        if not peer:
            continue
        try:
            from telethon.tl.functions.channels import GetParticipantRequest
            me = await safe_call(bot.get_me, what="bot_me", retries=1, raise_after_retries=False)
            if me is None:
                break
            await safe_call(bot(GetParticipantRequest(channel=peer, user_id=me.id)),
                            what="adminship", retries=1, raise_after_retries=False)
            out.append({**row, "can_post": True})
        except Exception as exc:
            out.append({**row, "can_post": False, "why": str(exc)[:120]})
    return out


async def auto_post_new_file(file_id: int, store_id: int | None = None) -> dict:
    """Publish a newly added file to the configured channel automatically.

    This is the “ভিডিও যোগ করলেই চ্যানেলে পোস্ট” flow: the admin sends a video to
    the bot, it gets mirrored, and the channel gets a message with the online
    button — optionally as a **limited link** instead of the media itself.
    """
    from app import runtime
    from app.services import settings

    if not settings.get_bool("AUTO_POST_ENABLED", False):
        return {"ok": False, "error": "off"}
    file_row = db.file(file_id)
    if file_row is None:
        return {"ok": False, "error": "file missing"}
    store_id = store_id or file_row["store_id"]
    store = db.store(store_id)
    target = (settings.get_str("AUTO_POST_CHANNEL") or "").strip()
    if not target:
        rows = db.join_channels()
        if not rows:
            return {"ok": False, "error": "কোনো চ্যানেল যোগ করা নেই"}
        target = rows[0].get("chat_id") or rows[0]["ref"]
    template = settings.get_str("AUTO_POST_TEXT") or "{name}"
    text = template.replace("{name}", file_row["name"])
    buttons = []
    username = runtime.bot_username or ""
    if store and username:
        buttons.append(f"🟢 {store['name'][:30]} খুলুন | "
                       f"https://t.me/{username}?start={store['slug']}")
    default = settings.get_str("BROADCAST_DEFAULT_BUTTONS")
    if default:
        buttons.append(default)
    limit = settings.get_int("AUTO_POST_LINK_LIMIT", 0)
    result = await publish(target, text=text, file_ids=[file_id],
                           buttons="\n".join(buttons), store_id=store_id,
                           link_limit=limit if limit > 0 else -1)
    db.log_event("auto_post", None, store_id, file_id, str(target),
                 "ok" if result.get("ok") else str(result.get("error"))[:120])
    return result


def composer_defaults(store_id: int | None = None) -> dict:
    """Text + buttons a fresh channel post starts with (editable in the panel)."""
    store = db.store(store_id) if store_id else None
    username = runtime.bot_username or ""
    text_lines = ["🎬 <b>নতুন ভিডিও যোগ হয়েছে!</b>"]
    if store:
        text_lines.append(f"🏪 <b>{store['name']}</b>")
        if store.get("description"):
            text_lines.append(store["description"][:200])
    text_lines.append("👇 নিচের বাটনে চাপ দিয়ে সরাসরি দেখুন।")
    buttons = []
    if store and username:
        buttons.append(f"🟢 {store['name'][:30]} খুলুন | https://t.me/{username}?start={store['slug']}")
    default = settings.get_str("BROADCAST_DEFAULT_BUTTONS")
    if default:
        buttons.append(default)
    return {"text": "\n".join(text_lines), "buttons": "\n".join(buttons),
            "store_id": store_id, "at": time.time()}


def composer_preview(text: str, buttons: str, store_id: int | None = None) -> dict:
    rows = parse_button_spec(buttons)
    return {"text": text, "buttons": rows, "has_buttons": bool(rows)}

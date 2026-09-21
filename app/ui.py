"""Shared UI helpers used by both the user and the admin handlers."""
from __future__ import annotations

from telethon import events
from telethon.errors import UserNotParticipantError
from telethon.tl.custom import Button
from telethon.tl.functions.channels import GetParticipantRequest

from app import config as cfg, keyboards, runtime, texts
from app.logger import log
from app.runtime import bot
from app.services import access
from app.services.sender import FLOOD, NO_SESSION, deliver
from app.services.telegram import safe_call
from app.storage import db
from app.utils import button_data, esc

Event = events.common.EventCommon


async def render(event, text: str, buttons=None, edit: bool = False):
    """Edit the current message when possible, otherwise send a new one."""
    if edit:
        try:
            return await event.edit(text, buttons=buttons, link_preview=False)
        except Exception:
            pass
    try:
        return await event.respond(text, buttons=buttons, link_preview=False)
    except Exception as exc:
        log.debug("render failed: %s", exc)
        return None


async def notify_join(event, channel: str) -> None:
    username = channel.replace("https://t.me/", "").replace("@", "").strip("/")
    buttons = [[Button.url("📢 Join channel", f"https://t.me/{username}")]] if username else None
    await event.respond(texts.FORCE_JOIN, buttons=buttons)


async def require_membership(event, user_id: int) -> bool:
    """Force-join gate with a 10 minute cache, so heavy browsing does not
    hammer GetParticipantRequest (and trip a FloodWait)."""
    channel = runtime.force_channel()
    if not channel or access.is_admin(user_id):
        return True
    cached = runtime.cached_join(user_id)
    if cached is not None:
        if not cached:
            await notify_join(event, channel)
        return cached
    try:
        await safe_call(bot, GetParticipantRequest(channel=channel, user_id=user_id),
                        what="force_join", retries=1)
        runtime.mark_join(user_id, True)
        return True
    except UserNotParticipantError:
        runtime.mark_join(user_id, False)
        await notify_join(event, channel)
        return False
    except Exception as exc:
        # Wrong channel name, bot not in the channel, … never lock users out.
        log.debug("membership check skipped: %s", exc)
        return True


async def show_store(event, store: dict, page: int = 0, edit: bool = True):
    from app.handlers.extras import store_header, sorted_files
    user_id = event.sender_id
    files = sorted_files(store["id"], user_id)
    total = len(files)
    page = max(0, min(page, max(0, (total - 1) // cfg.CONTENT_PAGE_SIZE)))
    start = page * cfg.CONTENT_PAGE_SIZE
    page_files = files[start:start + cfg.CONTENT_PAGE_SIZE]
    stats = db.store_stats(store["id"])
    subscribed = db.is_sub(store["id"], user_id)
    buttons = keyboards.store_content(store, page_files, page, total, subscribed)
    buttons.extend(view_extra_rows(store, user_id, page_files))

    header = store_header(store, user_id, total, stats["views"], page)
    if not page_files:
        header += "\n\n<i>এই স্টোরে এখন কিছু নেই — পরে আবার দেখুন।</i>"
    return await render(event, header, buttons, edit=edit)


def view_extra_rows(store: dict, user_id: int, page_files: list[dict]) -> list[list]:
    """Sort controls, share card, favorites and request shortcuts."""
    from telethon.tl.custom import Button
    from app.handlers.extras import SORT_MODES
    rows = [
        [Button.inline(SORT_MODES.get(db.user_sort(user_id), SORT_MODES["default"]),
                       f"st:{store['id']}"),
         Button.inline("🔗 শেয়ার", f"sh:{store['id']}")],
        [Button.inline("🙋 কনটেন্ট চাই", f"rq:{store['id']}")],
    ]
    for file_row in page_files[:2]:
        rows.append([Button.inline(f"⭐ {file_row['name'][:20]}", f"fv:{file_row['id']}")])
    return rows


async def deliver_to_user(event, user_id: int, file_row: dict,
                          store: dict | None = None) -> bool:
    if file_row is None:
        await event.respond(texts.NOT_FOUND)
        return False
    store = store or db.store(file_row["store_id"])
    if store is None:
        await event.respond(texts.NOT_FOUND)
        return False
    if not access.has_access(store, user_id):
        await event.respond(texts.access_denied(store["name"]))
        return False
    if not await require_membership(event, user_id):
        return False

    result = await deliver(event.chat_id, file_row)
    if result.ok:
        return True

    if result.reason == NO_SESSION:
        message = texts.SESSION_NEEDED
    elif result.reason == FLOOD:
        message = texts.FLOOD_WAIT
    else:
        message = texts.NOT_FOUND
    if access.is_admin(user_id) and result.detail:
        message += f"\n<code>{esc(result.detail)}</code>"
    await event.respond(message)
    return False


async def strip_button(event, data: bytes) -> None:
    """Remove an already delivered file's button from the list message."""
    try:
        message = await event.get_message()
    except Exception:
        return
    if message is None or not message.buttons:
        return
    rows = []
    for row in message.buttons:
        keep = [button for button in row if button_data(button) != data]
        if keep:
            rows.append(keep)
    try:
        if rows:
            await event.edit(buttons=rows)
        else:
            await event.edit("✅ <b>You've received everything in this list!</b>")
    except Exception as exc:
        log.debug("could not update buttons: %s", exc)

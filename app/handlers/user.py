"""User facing handlers: browsing stores, deep links, search, referrals."""
from __future__ import annotations

import asyncio
import time

from telethon import events
from telethon.tl.custom import Button

from app import i18n, keyboards, runtime, texts, ui
from app.handlers.router import route
from app.handlers.state import search_pending
from app.logger import log
from app.runtime import bot
from app.services import access
from app.services.sender import deliver
from app.storage import db
from app.utils import esc, safe_int

from app.payloads import (KIND_CODE, KIND_FILE, KIND_LEGACY_BATCH, KIND_LINK,
                          KIND_REFERRAL, KIND_SLUG, KIND_STORE, resolve_payload)


async def apply_referral(user_id: int, referrer_id: int, is_new: bool) -> None:
    """Reward the inviter once, and only for genuinely new users."""
    if not referrer_id or referrer_id == user_id or not is_new:
        return
    if not db.set_referrer(user_id, referrer_id):
        return
    for cfg_row in db.all_referral_cfgs():
        store = db.store(cfg_row["store_id"])
        if store is None or not cfg_row["days"]:
            continue
        access.grant_days(store["id"], referrer_id, cfg_row["days"], source="referral")
        try:
            await bot.send_message(
                referrer_id,
                f"🎁 <b>Referral reward!</b>\n"
                f"A friend joined with your link — you got <b>{cfg_row['days']} days</b> "
                f"of <b>{esc(store['name'])}</b>.",
            )
        except Exception as exc:
            log.debug("could not notify referrer %s: %s", referrer_id, exc)
    db.mark_rewarded(user_id)


async def deliver_link_token(event, user_id: int, token: str) -> None:
    row = db.link(token)
    if row is None:
        await event.respond("⚠️ This link is invalid or was removed.")
        return
    if row["expires_at"] and time.time() > row["expires_at"]:
        db.delete_link(token)
        await event.respond("⌛ <b>This link has expired.</b>")
        return
    if not await ui.require_membership(event, user_id):
        return
    file_ids = db.link_file_ids(token)
    if not file_ids:
        await event.respond("⚠️ This link has no files any more.")
        return
    await event.respond(f"📦 Sending {len(file_ids)} file(s)…")
    sent = 0
    for file_id in file_ids:
        file_row = db.file(file_id)
        if file_row is None:
            continue
        result = await deliver(event.chat_id, file_row)
        sent += 1 if result.ok else 0
        await asyncio.sleep(0.4)
    if not sent:
        await event.respond(texts.NOT_FOUND)


async def handle_payload(event, user_id: int, payload: str, is_new: bool) -> bool:
    """Returns True when the payload fully handled the message."""
    resolved = resolve_payload(payload)
    if resolved is None:
        return False
    kind, value = resolved

    if kind == KIND_REFERRAL:
        await apply_referral(user_id, int(value), is_new)
        return False                      # fall through to the normal home screen

    if kind == KIND_FILE:
        await ui.deliver_to_user(event, user_id, db.file(int(value)))
        return True

    if kind == KIND_STORE:
        store = db.store(int(value))
        if store is None:
            await event.respond("⚠️ This store no longer exists.")
            return True
        await ui.show_store(event, store, 0, edit=False)
        return True

    if kind == KIND_SLUG:
        store = db.store_by_slug(str(value))
        if store is None:
            await event.respond("⚠️ This store link is invalid or the store was deleted.")
            return True
        await ui.show_store(event, store, 0, edit=False)
        return True

    if kind == KIND_LINK:
        await deliver_link_token(event, user_id, str(value))
        return True

    if kind == KIND_LEGACY_BATCH:
        file_rows = [db.file_by_code(part) for part in str(value).split("_AND_")]
        file_rows = [row for row in file_rows if row]
        if not file_rows:
            await event.respond(texts.NOT_FOUND)
            return True
        if not await ui.require_membership(event, user_id):
            return True
        await event.respond(f"📦 Sending {len(file_rows)} file(s)…")
        for row in file_rows:
            await deliver(event.chat_id, row)
            await asyncio.sleep(0.4)
        return True

    if kind == KIND_CODE:
        file_row = db.file_by_code(str(value))
        if file_row:
            await ui.deliver_to_user(event, user_id, file_row)
            return True
        store = db.store_by_slug(str(value))
        if store:
            await ui.show_store(event, store, 0, edit=False)
            return True
        link = db.link(str(value))
        if link:
            await deliver_link_token(event, user_id, str(value))
            return True
        await event.respond("⚠️ This link is invalid or the content was removed.")
        return True

    return False


async def show_home(event, user_id: int, edit: bool = False):
    from app import i18n
    # Banned check
    try:
        if db.is_banned(user_id) and not access.is_admin(user_id):
            await ui.render(event, "🚫 <b>আপনি ব্যান হয়েছেন</b>\nঅ্যাডমিনের সাথে যোগাযোগ করুন।",
                            [[Button.inline(i18n.t(user_id, "contact_button"), "ct:0")]], edit=edit)
            return
    except Exception:
        pass

    stores = db.all_stores()
    if not stores:
        await ui.render(event, "👋 <b>Welcome!</b>\nNo content stores are available right now.",
                        None, edit=edit)
        return

    # Custom welcome if admin set one
    try:
        # Use primary admin's welcome if exists
        welcome = ""
        if cfg.ADMIN_IDS:
            welcome = db.welcome_text(cfg.ADMIN_IDS[0])
        banner = welcome if welcome else i18n.t(user_id, "home_banner")
    except Exception:
        banner = i18n.t(user_id, "home_banner")

    buttons = keyboards.user_store_list(stores)
    buttons.extend([
        [Button.inline(i18n.t(user_id, "invite_button"), "rf"),
         Button.inline(i18n.t(user_id, "favorites_button"), "fvs")],
        [Button.inline(i18n.t(user_id, "search_all"), "gs"),
         Button.inline(i18n.t(user_id, "my_access_button"), "mya")],
        [Button.inline(i18n.t(user_id, "request_button"), "rq:0")],
        [Button.inline(i18n.t(user_id, "contact_button"), "ct:0"),
         Button.inline(i18n.t(user_id, "help_button"), "hp")],
        [Button.inline(i18n.t(user_id, "language_button"), "lang")],
    ])
    await ui.render(event, banner, buttons, edit=edit)


# ------------------------------------------------------------------- commands
@bot.on(events.NewMessage(func=lambda e: e.is_private,
                          pattern=r"^/(start|admin|admin@\w+)(\s|$)"))
async def start_handler(event: events.NewMessage.Event) -> None:
    user_id = event.sender_id
    sender = await event.get_sender()
    is_new = db.touch_user(user_id,
                           getattr(sender, "first_name", None),
                           getattr(sender, "username", None))

    parts = (event.raw_text or "").strip().split(maxsplit=1)
    command = parts[0].split("@")[0].lower()
    payload = parts[1].strip() if len(parts) > 1 else ""

    if payload and await handle_payload(event, user_id, payload, is_new):
        return

    if access.is_admin(user_id):
        from app.handlers.admin import send_panel     # local import avoids a cycle
        await send_panel(event)
        return

    if command == "/admin":
        return                                        # never leak the panel
    await show_home(event, user_id)


@bot.on(events.NewMessage(func=lambda e: e.is_private, pattern=r"^/help"))
async def help_handler(event: events.NewMessage.Event) -> None:
    user_id = event.sender_id
    text = texts.HELP_TEXT
    if access.is_admin(user_id):
        text += "\n\n" + texts.ADMIN_HELP
    await event.respond(text)


@bot.on(events.NewMessage(func=lambda e: e.is_private, pattern=r"^/cancel"))
async def cancel_handler(event: events.NewMessage.Event) -> None:
    from app.handlers.state import clear
    clear(event.sender_id)
    await event.respond(texts.CANCELLED)


# ------------------------------------------------------------------- callbacks
@route("hp")
async def help_button(event, _rest: str) -> None:
    text = texts.HELP_TEXT
    if access.is_admin(event.sender_id):
        text += "\n\n" + texts.ADMIN_HELP
    await ui.render(event, text, [[Button.inline("🔙 Back", "bs:0")]], edit=True)
    await event.answer()


@route("rf")
async def referral_button(event, _rest: str) -> None:
    user_id = event.sender_id
    if not runtime.bot_username:
        await event.answer("Bot is still starting up — try again in a moment.", alert=True)
        return
    lines = [
        "🎁 <b>Invite &amp; Earn</b>",
        "",
        f"Your personal link:\n<code>https://t.me/{runtime.bot_username}?start=r{user_id}</code>",
        f"Friends joined with it: <b>{db.invite_count(user_id)}</b>",
        "",
    ]
    rewards = []
    for cfg_row in db.all_referral_cfgs():
        store = db.store(cfg_row["store_id"])
        if store and cfg_row["days"]:
            rewards.append(f"• A friend joins → <b>{cfg_row['days']} days</b> of <b>{esc(store['name'])}</b>")
    lines.append("<b>Rewards:</b>")
    lines.extend(rewards or ["• Ask the admin about current referral rewards."])
    await ui.render(event, "\n".join(lines),
                    [[Button.inline("🔙 Back", "bs:0")]], edit=True)
    await event.answer()


@route("bs:")
async def back_to_stores(event, rest: str) -> None:
    if not await ui.require_membership(event, event.sender_id):
        await event.answer()
        return
    await show_home(event, event.sender_id, edit=True)
    await event.answer()


@route("s:")
async def open_store(event, rest: str) -> None:
    user_id = event.sender_id
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("This store no longer exists.", alert=True)
        return
    if not await ui.require_membership(event, user_id):
        await event.answer()
        return
    if not access.has_access(store, user_id):
        from app.handlers.billing import paywall_buttons
        from app.services import billing
        stats = db.store_stats(store["id"])
        text = (
            i18n.t(user_id, "premium_locked", store=esc(store["name"]))
            + f"\n\n{i18n.t(user_id, 'items', n=stats['files'])}"
            + billing.upsell_text(store, user_id)
        )
        await ui.render(event, text, paywall_buttons(event, store, user_id), edit=True)
        await event.answer()
        return
    await ui.show_store(event, store, 0, edit=True)
    await event.answer()


@route("sp:")
async def store_page(event, rest: str) -> None:
    store_id, _, page = rest.partition(":")
    store = db.store(safe_int(store_id))
    if store is None:
        await event.answer("This store no longer exists.", alert=True)
        return
    if not access.has_access(store, event.sender_id):
        await event.answer(texts.NO_ACCESS_ALERT, alert=True)
        return
    await ui.show_store(event, store, safe_int(page), edit=True)
    await event.answer()


@route("ss:")
async def search_prompt(event, rest: str) -> None:
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("This store no longer exists.", alert=True)
        return
    if not access.has_access(store, event.sender_id):
        await event.answer(texts.NO_ACCESS_ALERT, alert=True)
        return
    search_pending[event.sender_id] = store["id"]
    await event.respond(f"🔍 Send a keyword to search inside <b>{esc(store['name'])}</b>\n"
                        f"<i>(send /cancel to stop)</i>")
    await event.answer()


@route("sb:")
async def toggle_subscription(event, rest: str) -> None:
    store = db.store(safe_int(rest))
    user_id = event.sender_id
    if store is None:
        await event.answer("This store no longer exists.", alert=True)
        return
    if not access.has_access(store, user_id):
        # Bug fix: previously anyone could subscribe to a premium store's daily
        # drip and receive the paid files for free.
        await event.answer(texts.NO_ACCESS_ALERT, alert=True)
        return
    subscribed = db.toggle_sub(store["id"], user_id)
    await event.answer("🔔 You'll get daily updates from this store!"
                       if subscribed else "🔕 Unsubscribed from daily updates.", alert=True)


@route("f:")
async def receive_file(event, rest: str) -> None:
    user_id = event.sender_id
    file_row = db.file(safe_int(rest))
    if file_row is None:
        await event.answer("This file was removed.", alert=True)
        return
    sent = await ui.deliver_to_user(event, user_id, file_row)
    await event.answer("✅ Sent!" if sent else "")
    if sent:
        await ui.strip_button(event, event.data)

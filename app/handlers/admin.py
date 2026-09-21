"""Admin control panel: stores, sessions, scanning, grants, drip, broadcast.

v2.2: panel is now category-based (clean). All old routes still work, but
main panel shows only essential buttons; details behind sub-menus.
User can grant/revoke access per store, download DB, and manage everything
without getting lost.
"""
from __future__ import annotations

import time

from telethon import events
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.custom import Button

from app import config as cfg, keyboards, runtime, texts, ui
from app.handlers.router import route
from app.handlers.state import (ask, batch_mode, ctx as flow_ctx,
                                dialog_cache, flow)
from app.logger import log
from app.runtime import bot, spawn
from app.services import access, scanner
from app.services.telegram import flood_status
from app.storage import db
from app.utils import esc, fmt_ts, human_delta, local_time_str, safe_int

ADMIN_HELP = texts.ADMIN_HELP


# --------------------------------------------------------------------- panel
def panel_text(admin_id: int) -> str:
    stores = db.stores_admin(admin_id)
    stats = db.stats()
    active_id = db.active_store_id(admin_id)
    active = db.store(active_id) if active_id else None
    session_state = "✅ connected" if runtime.is_online(admin_id) else "❌ not connected"
    lines = [
        "⚙️ <b>অ্যাডমিন প্যানেল</b> — সবকিছু এক জায়গায়",
        "",
        f"🏪 Active: <b>{esc(active['name']) if active else 'not set'}</b> · 📂 Stores: <b>{len(stores)}</b> · 📄 Files: <b>{stats['files']}</b>",
        f"👥 Users: <b>{stats['users']}</b> · 💎 Granted: <b>{stats['grants']}</b> · 👁 Views: <b>{stats['views']}</b>",
        f"🤖 Session: {session_state} · 📢 Force-join: <b>{esc(runtime.force_channel() or 'off')}</b>",
    ]
    revenue = db.revenue()
    if revenue["count"] or revenue["pending"]:
        lines.append(f"💰 Revenue: <b>{revenue['total']:.0f}</b> · ⏳ Pending: <b>{revenue['pending']}</b> · ✅ Orders: {revenue['count']}")
    open_tickets = db.open_ticket_count()
    if open_tickets:
        lines.append(f"📩 Support: <b>{open_tickets} open</b>")
    open_requests = db.open_request_count()
    if open_requests:
        lines.append(f"🙋 Requests: <b>{open_requests} open</b>")
    flood = flood_status()
    if flood:
        lines.append(f"⏳ Rate limited: {esc(flood)}")
    lines.append("")
    lines.append("<i>নিচ থেকে একটি ক্যাটাগরি বেছে নিন — সবকিছু ভিতরে সাজানো আছে।</i>")
    return "\n".join(lines)


def panel_buttons(admin_id: int) -> list[list[Button]]:
    active_id = db.active_store_id(admin_id)
    active = db.store(active_id) if active_id else None
    return keyboards.admin_panel(
        admin_id,
        active["name"] if active else None,
        runtime.is_online(admin_id),
        bool(active["is_premium"]) if active else None,
        runtime.force_channel(),
        bool(runtime.caption()),
        pending_orders=db.pending_order_count(),
        open_tickets=db.open_ticket_count(),
    )


async def send_panel(event, edit: bool = False) -> None:
    await ui.render(event, panel_text(event.sender_id), panel_buttons(event.sender_id), edit=edit)


def _active_store(admin_id: int) -> dict | None:
    store_id = db.active_store_id(admin_id)
    return db.store(store_id) if store_id else None


# ------------------------------------------------------------- panel actions
@route("adm:")
async def admin_action(event, action: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        await event.answer("Admins only.", alert=True)
        return

    # ---------------- main panel
    if action == "back":
        await send_panel(event, edit=True)

    # ---------------- category menus (new clean design)
    elif action == "stores":
        stores = db.stores_admin(admin_id)
        active_id = db.active_store_id(admin_id)
        text = (
            "🏪 <b>Stores — আপনার সব স্টোর</b>\n\n"
            f"মোট: <b>{len(stores)}</b> টি স্টোর · Active: <b>{esc(_active_store(admin_id)['name']) if _active_store(admin_id) else 'not set'}</b>\n"
            "একটি স্টোরে ট্যাপ করলে তার সেটিংস খুলবে (rename, description, cover, plans, files, access)।\n"
        )
        await ui.render(event, text, keyboards.admin_stores_menu(stores, active_id), edit=True)

    elif action == "users":
        await ui.render(event,
                        "👥 <b>Users & Access</b>\n\n"
                        "• সব ইউজার দেখুন, সার্চ করুন\n"
                        "• যেকোনো স্টোরে অ্যাক্সেস দিন বা কেড়ে নিন\n"
                        "• ব্রডকাস্ট পাঠান",
                        keyboards.admin_users_menu(), edit=True)

    elif action == "users_list":
        from app.handlers.manage import show_users_page
        await show_users_page(event, 0)

    elif action == "users_search":
        ask(admin_id, "user_search")
        await event.respond("🔍 ইউজার খুঁজুন — ID, নাম বা username লিখে পাঠান।\n<i>/cancel দিয়ে বাতিল</i>")

    elif action == "revoke_menu":
        stores = db.stores_admin(admin_id)
        if not stores:
            await event.answer("Create a store first.", alert=True)
            return
        await ui.render(event, "🚫 <b>Revoke access</b>\nকোন স্টোর থেকে অ্যাক্সেস কেড়ে নেবেন?",
                        keyboards.store_picker(stores, "sga:", "adm:users"), edit=True)

    elif action == "sales":
        pending = db.pending_order_count()
        await ui.render(event,
                        f"💰 <b>Sales & Monetization</b>\n\n"
                        f"⏳ Pending orders: <b>{pending}</b>\n"
                        f"💰 Total revenue: <b>{db.revenue()['total']:.0f}</b>\n"
                        "এখান থেকে প্ল্যান, অর্ডার, কুপন, পেমেন্ট সেটিংস সব ম্যানেজ করুন।",
                        keyboards.admin_sales_menu(pending), edit=True)

    elif action == "content":
        await ui.render(event,
                        "📦 <b>Content & Files</b>\n\n"
                        "• চ্যানেল স্ক্যান (session / bot-admin)\n"
                        "• ফাইল ম্যানেজ (rename, move, delete, link)\n"
                        "• মাল্টি-লিংক ও ব্যাচ লিংক",
                        keyboards.admin_content_menu(), edit=True)

    elif action == "growth":
        await ui.render(event,
                        "📢 <b>Growth & Engagement</b>\n\n"
                        "• ব্রডকাস্ট, রেফারেল, ডেইলি ড্রিপ\n"
                        "• ফোর্স-জয়েন চ্যানেল সেট করুন\n"
                        "• ক্যাপশন ও শেয়ার লিংক",
                        keyboards.admin_growth_menu(runtime.force_channel(), bool(runtime.caption())),
                        edit=True)

    elif action == "settings":
        await ui.render(event,
                        "🛠 <b>Settings & Tools</b>\n\n"
                        "• সেশন কানেক্ট / ডিসকানেক্ট\n"
                        "• ব্যাকআপ ও ডাটাবেস ডাউনলোড\n"
                        "• ফোর্স-জয়েন, ক্যাপশন",
                        keyboards.admin_settings_menu(runtime.is_online(admin_id)), edit=True)

    elif action == "lang_info":
        await ui.render(event,
                        "🌐 <b>Language</b>\n\n"
                        "ইউজাররা নিজেরা 🌐 বাটনে ভাষা বদলাতে পারে (বাংলা ↔ English)।\n"
                        "ডিফল্ট ভাষা <code>config.env</code> এ <code>DEFAULT_LANG=bn</code> দিয়ে সেট করা যায়।",
                        [[Button.inline("🔙 Back to settings", "adm:settings")]], edit=True)

    elif action == "ref_info":
        current = db.referral_cfg(admin_id)
        note = ""
        if current:
            store = db.store(current["store_id"])
            if store:
                note = f"\nবর্তমানে: <b>{current['days']} দিন</b> এর <b>{esc(store['name'])}</b>"
        await ui.render(event,
                        f"🎁 <b>Referral reward</b>{note}\n\n"
                        "ইউজাররা বন্ধুকে ইনভাইট করলে কতদিনের অ্যাক্সেস পাবে তা এখানে সেট করুন।",
                        [[Button.inline("⚙️ Setup referral", "adm:ref")],
                         [Button.inline("🔙 Back", "adm:users")]], edit=True)

    elif action == "dbdl":
        # Try to send DB file via bot if small, otherwise give instructions
        try:
            import os
            db_path = cfg.DB_FILE
            size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
            if size and size < 45 * 1024 * 1024:  # Telegram bot limit ~50MB
                await event.respond(f"📥 Database file ({size // 1024} KB) পাঠাচ্ছি...")
                await bot.send_file(admin_id, db_path, caption=f"💾 DB backup — {fmt_ts(time.time())}")
            else:
                await event.respond(
                    "📥 <b>Database download</b>\n\n"
                    f"DB path: <code>{esc(db_path)}</code>\n"
                    f"Size: {size // (1024*1024)} MB — Telegram এ সরাসরি পাঠানো যাচ্ছে না (৫০MB সীমা)।\n\n"
                    "উপায়:\n"
                    "1️⃣ ওয়েব ড্যাশবোর্ড থেকে ডাউনলোড করুন: <code>/download/db</code>\n"
                    "2️⃣ হোস্টিং প্যানেলের File Manager থেকে <code>bot_data.sqlite3</code> ডাউনলোড করুন\n"
                    "3️⃣ বা <code>/backup</code> কমান্ডে JSON ব্যাকআপ নিন (ছোট ফাইল)"
                )
        except Exception as exc:
            log.error("db download failed: %s", exc)
            await event.answer("❌ Download failed — check logs.", alert=True)

    elif action == "file_search":
        ask(admin_id, "admin_file_search")
        await event.respond("🔍 ফাইল খুঁজুন — নামের অংশ লিখে পাঠান (যেমন <code>interstellar</code>)।\n"
                            "সব স্টোরে খোঁজা হবে।")

    elif action == "links_manage":
        stores = db.stores_admin(admin_id)
        if not stores:
            await event.answer("No stores.", alert=True)
            return
        # Show active links count
        link_count = db.link_count()
        await ui.render(event,
                        f"🔗 <b>Links — {link_count} active</b>\n\n"
                        "• Store links are permanent (never expire unless deleted)\n"
                        "• Multi-links can expire — set expiry when creating\n"
                        "• Use 🗂 Manage files → Get link for single files",
                        [[Button.inline("🔗 All store links", "adm:links")],
                         [Button.inline("🎬 Multi-link generator", "adm:ml")],
                         [Button.inline("🔙 Back to content", "adm:content")]], edit=True)

    elif action == "ban_menu":
        ask(admin_id, "ban_user")
        await event.respond("🚫 ইউজার ব্যান করুন — user ID পাঠান।\n"
                            "ব্যান করলে সে আর বট ব্যবহার করতে পারবে না, ফাইল পাবে না।")

    elif action == "unban_menu":
        ask(admin_id, "unban_user")
        await event.respond("✅ ইউজার আনব্যান — user ID পাঠান।")

    elif action == "ref_leaderboard":
        top = db.top_referrers(10)
        if not top:
            await ui.render(event, "🎁 <b>Referral leaderboard</b>\n\n<i>No referrals yet.</i>",
                            [[Button.inline("🔙 Back", "adm:growth")]], edit=True)
            return
        lines = ["🎁 <b>Top referrers</b>", ""]
        for i, row in enumerate(top, 1):
            user = db.user(row["referrer_id"]) or {}
            name = esc(user.get("name") or str(row["referrer_id"]))
            lines.append(f"{i}. <code>{row['referrer_id']}</code> · {name} — <b>{row['invites']} invites</b>")
        await ui.render(event, "\n".join(lines),
                        [[Button.inline("🔙 Back", "adm:growth")]], edit=True)

    elif action == "welcome_set":
        current = db.welcome_text(admin_id) or "(not set)"
        ask(admin_id, "set_welcome")
        await event.respond(f"👋 বর্তমান ওয়েলকাম মেসেজ:\n<code>{esc(current[:200])}</code>\n\n"
                            "নতুন ওয়েলকাম মেসেজ লিখে পাঠান — ইউজার /start দিলে এটি দেখবে।\n"
                            "<i>clear লিখলে ডিফল্টে ফিরে যাবে।</i>")

    # ---------------- old actions (kept for compatibility)
    elif action == "help":
        await ui.render(event, ADMIN_HELP, [[Button.inline("🔙 Back", "adm:back")]], edit=True)
    elif action == "new":
        ask(admin_id, "create_store")
        await event.respond("💬 Send the <b>name</b> for the new store (e.g. <code>Movies 🍿</code>).")
    elif action == "del":
        stores = db.stores_admin(admin_id)
        if not stores:
            await event.answer("No stores to delete.", alert=True)
            return
        await ui.render(event, "🗑 <b>Which store should be deleted?</b>\n"
                               "<i>Its files, grants, drip settings and links go with it.</i>",
                        keyboards.store_picker(stores, "dst:", "adm:stores"), edit=True)
    elif action == "active":
        stores = db.stores_admin(admin_id)
        if not stores:
            await event.answer("Create a store first.", alert=True)
            return
        await ui.render(event, "📌 <b>Pick the active store</b>\n"
                               "<i>New uploads and scans go into the active store.</i>",
                        keyboards.store_picker(stores, "ast:", "adm:stores",
                                               mark_active=db.active_store_id(admin_id)),
                        edit=True)
    elif action == "lock":
        store = _active_store(admin_id)
        if store is None:
            await event.answer("Set an active store first.", alert=True)
            return
        new_state = 0 if store["is_premium"] else 1
        db.update_store(store["id"], is_premium=new_state)
        await event.answer(
            f"{store['name']} is now {'🔒 premium (needs a grant)' if new_state else '🌐 free for everyone'}.",
            alert=True,
        )
        await send_panel(event, edit=True)
    elif action == "fj":
        current = runtime.force_channel() or "off"
        ask(admin_id, "set_channel")
        await event.respond(f"📢 Current force-join: <b>{esc(current)}</b>\n\n"
                            "Send the channel username (e.g. <code>@mychannel</code>), "
                            "or send <code>off</code> to disable.\n\n"
                            "<i>Tip: @username, t.me/username, or https://t.me/username — সব ফরম্যাট চলবে।</i>")
    elif action == "cap":
        current = runtime.caption() or "(empty)"
        ask(admin_id, "set_caption")
        await event.respond(f"📝 Current caption:\n<code>{esc(current)}</code>\n\n"
                            "Send the new caption text, or <code>clear</code> to remove it.")
    elif action == "conn":
        if runtime.is_online(admin_id):
            owner = runtime.session_owners.get(admin_id, "?")
            buttons = [
                [Button.inline("🔄 Replace session", "adm:repl")],
                [Button.inline("🔌 Disconnect", "adm:disc")],
                [Button.inline("🔙 Back", "adm:settings")],
            ]
            await ui.render(event, f"📲 <b>Session active</b> (account id <code>{owner}</code>).\n"
                                   "It is used to serve files from private channels.",
                            buttons, edit=True)
        else:
            ask(admin_id, "session_input")
            await event.respond("💬 Send your <b>String Session</b> in the next message.\n"
                                "<i>(/cancel to abort — the message will be deleted after use.)</i>")
    elif action == "repl":
        ask(admin_id, "session_input")
        await event.respond("💬 Send the <b>new String Session</b> — it replaces the current one.")
    elif action == "disc":
        client = runtime.user_clients.pop(admin_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        runtime.unregister_client(admin_id)
        db.delete_session(admin_id)
        await event.respond("🔌 Session disconnected.")
        await send_panel(event, edit=False)
    elif action == "scan":
        if not runtime.is_online(admin_id):
            await event.answer("Connect a session first (📲 Session), or use 🤖 Bot-admin scan which needs no session.", alert=True)
            return
        status = await event.respond("🔍 Loading your channels…")
        spawn(_load_and_show_dialogs(event, admin_id, status))
    elif action == "bscan":
        # Improved: show which store will receive files
        store = _active_store(admin_id)
        if store is None:
            stores = db.stores_admin(admin_id)
            if not stores:
                await event.answer("Create a store first.", alert=True)
                return
            await ui.render(event,
                            "🤖 <b>Bot-admin scan — pick destination store first</b>\n"
                            "বট যে চ্যানেলে অ্যাডমিন, সেই চ্যানেল থেকে ফাইল আনতে পারবে (কোনো session লাগবে না)।",
                            keyboards.store_picker(stores, "bscan_store:", "adm:content"), edit=True)
            return
        ask(admin_id, "botscan_channel")
        await event.respond(
            f"🤖 <b>Bot-admin scan → {esc(store['name'])}</b>\n"
            "Make sure this bot is an <b>admin</b> in that channel, then either:\n"
            "• forward any one message from the channel here, or\n"
            "• send its <code>@username</code>.\n\n"
            "<i>এটি সবচেয়ে সহজ উপায় — কোনো String Session লাগে না।</i>"
        )
    elif action == "plans":
        stores = db.stores_admin(admin_id)
        if not stores:
            await event.answer("Create a store first.", alert=True)
            return
        await ui.render(event, "💎 <b>Plans & pricing</b>\nPick a store to edit its plans:",
                        keyboards.store_picker(stores, "apl:", "adm:sales"), edit=True)
    elif action == "sset":
        stores = db.stores_admin(admin_id)
        if not stores:
            await event.answer("Create a store first.", alert=True)
            return
        await ui.render(event, "🛠 <b>Store settings</b>\nPick a store:",
                        keyboards.store_picker(stores, "sss:", "adm:stores"), edit=True)
    elif action == "files":
        stores = db.stores_admin(admin_id)
        if not stores:
            await event.answer("Create a store first.", alert=True)
            return
        active = _active_store(admin_id)
        if active:
            from app.handlers.manage import show_file_manager
            await show_file_manager(event, active, 0, edit=True)
        else:
            await ui.render(event, "🗂 <b>Manage files</b> — pick a store:",
                            keyboards.store_picker(stores, "fms:", "adm:content"), edit=True)
    elif action == "ml":
        stores = db.stores_admin(admin_id)
        if not stores:
            await event.answer("Create a store first.", alert=True)
            return
        await ui.render(event, "🎬 <b>Multi-link generator</b>\n"
                               "Pick a store, select 2–8 files and generate one shareable link.",
                        keyboards.store_picker(stores, "lg:", "adm:content"), edit=True)
    elif action == "links":
        stores = db.stores_admin(admin_id)
        if not stores:
            await event.answer("Create a store first.", alert=True)
            return
        lines = ["🔗 <b>Shareable store links</b>", ""]
        for store in stores:
            stats = db.store_stats(store["id"])
            lock = "🔒" if store["is_premium"] else "🌐"
            lines.append(f"{lock} <b>{esc(store['name'])}</b> — {stats['files']} files")
            lines.append(f"<code>https://t.me/{runtime.bot_username}?start=s{store['id']}</code>")
            lines.append(f"<i>short link:</i> <code>https://t.me/{runtime.bot_username}?start={store['slug']}</code>")
            lines.append("")
        await ui.render(event, "\n".join(lines), [[Button.inline("🔙 Back", "adm:content")]], edit=True)
    elif action == "batch":
        batch_mode[admin_id] = []
        await event.respond("📦 <b>Batch mode on.</b> Forward photos/videos here and send /done "
                            "when you are finished — you'll get one link for all of them.")
    elif action == "bc":
        stores = db.stores_admin(admin_id)
        await ui.render(event, "📢 <b>Who should receive this broadcast?</b>",
                        keyboards.broadcast_filters(stores), edit=True)
    elif action == "gp":
        # Improved grant flow: pick store first if not already in flow
        flow_ctx_store = flow_ctx(admin_id).get("store_id")
        if flow_ctx_store:
            ask(admin_id, "grant_target")
            await event.respond(f"👑 Send the <b>Telegram user ID</b> for store <code>{flow_ctx_store}</code> (or forward a message from them).")
        else:
            stores = db.stores_admin(admin_id)
            if not stores:
                await event.answer("Create a store first.", alert=True)
                return
            await ui.render(event, "👑 <b>Grant premium — pick a store first</b>",
                            keyboards.store_picker(stores, "gst:", "adm:users"), edit=True)
    elif action == "ref":
        stores = db.stores_admin(admin_id)
        if not stores:
            await event.answer("Create a store first.", alert=True)
            return
        current = db.referral_cfg(admin_id)
        note = ""
        if current:
            store = db.store(current["store_id"])
            if store:
                note = f"\nCurrently: <b>{current['days']} days</b> of <b>{esc(store['name'])}</b>"
        await ui.render(event, f"🎁 <b>Referral reward setup</b>{note}\n\n"
                              "Which store should new invites unlock?",
                        keyboards.store_picker(stores, "rst:", "adm:growth"), edit=True)
    elif action == "drip":
        stores = db.stores_admin(admin_id)
        if not stores:
            await event.answer("Create a store first.", alert=True)
            return
        lines = ["📅 <b>Daily drip</b>", "", "Push a few never-before-sent files to subscribers every day.", ""]
        for store in stores:
            row = db.drip(store["id"])
            if row and row["enabled"]:
                lines.append(f"🏪 <b>{esc(store['name'])}</b> — {row['count']}/day at "
                             f"{row['send_time']} · {len(db.subscribers(store['id']))} subscribers")
            else:
                lines.append(f"🏪 <b>{esc(store['name'])}</b> — off")
        await ui.render(event, "\n".join(lines),
                        keyboards.store_picker(stores, "dr:", "adm:growth"), edit=True)
    elif action == "stats":
        await ui.render(event, stats_text(admin_id), [[Button.inline("🔙 Back", "adm:back")]], edit=True)
    elif action == "users":
        from app.handlers.manage import show_users_page
        await show_users_page(event, 0)
    elif action == "backup":
        try:
            path = db.backup_json(cfg.BACKUP_DIR, cfg.KEEP_BACKUPS)
            await event.answer("✅ Backup written", alert=True)
            await event.respond(f"💾 Backup saved:\n<code>{esc(path)}</code>")
        except Exception as exc:
            log.error("manual backup failed: %s", exc)
            await event.answer("❌ Backup failed — check the logs.", alert=True)
    elif action == "prev":
        from app.handlers.user import show_home
        await show_home(event, admin_id, edit=True)
    else:
        await event.answer()
        return
    try:
        await event.answer()
    except Exception:
        pass


def stats_text(admin_id: int) -> str:
    stats = db.stats()
    stores = db.stores_admin(admin_id)
    lines = [
        "📊 <b>Statistics</b>",
        "",
        f"👥 Users: <b>{stats['users']}</b>",
        f"📂 Files: <b>{stats['files']}</b> · 👁 Views: <b>{stats['views']}</b>",
        f"🏪 Stores: <b>{len(stores)}</b> · 🔗 Links: <b>{stats['links']}</b>",
        f"💎 Granted users: <b>{stats['grants']}</b> · 🎁 Referrals: <b>{stats['referrals']}</b>",
        f"💰 Revenue: <b>{stats['revenue']:.0f}</b> · ⏳ Pending: {stats['orders_pending']}",
        "",
        "<b>Your stores</b>",
    ]
    for store in stores:
        s = db.store_stats(store["id"])
        lock = "🔒" if store["is_premium"] else "🌐"
        lines.append(f"{lock} <b>{esc(store['name'])}</b> — {s['files']} files · 👁 {s['views']} · "
                     f"💎 {s['grants']} · 🔔 {s['subs']}")
    top = db.top_files(5)
    if top:
        lines.append("")
        lines.append("<b>Top files</b>")
        for row in top:
            lines.append(f"👁 {row['views']} — {esc(row['name'])} <i>({esc(row['store_name'] or '?')})</i>")
    return "\n".join(lines)


async def _load_and_show_dialogs(event, admin_id: int, status_message) -> None:
    dialogs = await scanner.list_dialogs(admin_id)
    dialog_cache[admin_id] = dialogs
    if not dialogs:
        await status_message.edit("⚠️ No channels or groups found for this session.\n"
                                  "Try 🤖 Bot-admin scan instead — it needs no session.")
        return
    await status_message.edit("🔍 <b>Pick a channel to scan:</b>\n"
                              "<i>Session scan — works for private channels your account can see.</i>",
                              buttons=keyboards.scan_chats(dialogs, 0))


# ------------------------------------------------------- store / drip / grant
@route("ast:")
async def set_active_store(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    db.set_active_store(event.sender_id, store["id"])
    await event.answer(f"✅ Active store: {store['name']}", alert=True)
    await send_panel(event, edit=True)


@route("bscan_store:")
async def bscan_pick_store(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    db.set_active_store(event.sender_id, store["id"])
    flow(event.sender_id, store_id=store["id"])
    ask(event.sender_id, "botscan_channel")
    await ui.render(event,
                    f"🤖 <b>Bot-admin scan → {esc(store['name'])}</b>\n\n"
                    "বটকে ওই চ্যানেলে অ্যাডমিন বানিয়েছেন তো? তারপর:\n"
                    "• চ্যানেল থেকে যেকোনো একটি মেসেজ এখানে ফরওয়ার্ড করুন, অথবা\n"
                    "• চ্যানেলের @username পাঠান (যেমন <code>@mychannel</code>)",
                    [[Button.inline("🔙 Back to content", "adm:content")]], edit=True)
    await event.answer()


@route("dst:")
async def confirm_delete_store(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    store = db.store(safe_int(rest))
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    stats = db.store_stats(store["id"])
    await ui.render(
        event,
        f"🗑 <b>Delete “{esc(store['name'])}”?</b>\n\n"
        f"This removes {stats['files']} files, {stats['grants']} grants, "
        f"{stats['subs']} drip subscribers and its links.\n"
        "<b>This cannot be undone.</b>",
        keyboards.confirm(f"dc:{store['id']}", "adm:stores"),
        edit=True,
    )


@route("dc:")
async def delete_store(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    store_id = safe_int(rest)
    store = db.store(store_id)
    if store is None:
        await event.answer("Already deleted.", alert=True)
        return
    db.delete_store(store_id)
    await event.answer(f"🗑 Deleted {store['name']}.", alert=True)
    await send_panel(event, edit=True)


@route("dr:")
async def drip_setup(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store_id = safe_int(rest)
    store = db.store(store_id)
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    flow(admin_id, store_id=store_id)
    row = db.drip(store_id)
    buttons = [
        [Button.inline("2 files/day", "drc:2"), Button.inline("3 files/day", "drc:3")],
        [Button.inline("5 files/day", "drc:5")],
        [Button.inline("✏️ Custom count", "drc:custom")],
    ]
    if row and row["enabled"]:
        buttons.append([Button.inline("⏹ Turn off", f"dro:{store_id}")])
    buttons.append([Button.inline("🔙 Back", "adm:growth")])
    current = ""
    if row and row["enabled"]:
        current = f"\nCurrently: {row['count']}/day at {row['send_time']}"
    await ui.render(event, f"📅 <b>{esc(store['name'])}</b> — how many new files per day?{current}",
                    buttons, edit=True)


@route("drc:")
async def drip_count(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store_id = flow_ctx(admin_id).get("store_id")
    if not store_id:
        await event.answer("Start over from 📅 Daily drip.", alert=True)
        return
    if rest == "custom":
        ask(admin_id, "drip_count", store_id=store_id)
        await event.respond("💬 Send how many files to send per day.")
        return
    ask(admin_id, "drip_time", store_id=store_id, count=safe_int(rest, 3))
    await event.respond(f"🕒 Send the daily send time as <code>HH:MM</code> "
                        f"(24h, your timezone, current time {local_time_str()}).")


@route("dro:")
async def drip_off(event, rest: str) -> None:
    if not access.is_admin(event.sender_id):
        return
    store_id = safe_int(rest)
    db.set_drip_enabled(store_id, False)
    await event.answer("⏹ Daily drip turned off.", alert=True)
    await ui.render(event, f"⏹ Drip disabled for store #{store_id}",
                    [[Button.inline("🔙 Back", "adm:growth")]], edit=True)


@route("rst:")
async def referral_store(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store_id = safe_int(rest)
    store = db.store(store_id)
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    flow(admin_id, store_id=store_id)
    buttons = [
        [Button.inline("7 days", "rd:7"), Button.inline("15 days", "rd:15")],
        [Button.inline("30 days", "rd:30"), Button.inline("✏️ Custom", "rd:custom")],
        [Button.inline("🔙 Back", "adm:growth")],
    ]
    await ui.render(event, f"🎁 Days of <b>{esc(store['name'])}</b> per successful invite?",
                    buttons, edit=True)


@route("rd:")
async def referral_days(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store_id = flow_ctx(admin_id).get("store_id")
    if not store_id:
        await event.answer("Start over from 🎁 Referral reward.", alert=True)
        return
    if rest == "custom":
        ask(admin_id, "referral_days_value", store_id=store_id)
        await event.respond("💬 Send the number of days to reward per invite.")
        return
    days = safe_int(rest)
    db.set_referral_cfg(admin_id, store_id, days)
    await event.answer(f"✅ {days} days per invite.", alert=True)
    await send_panel(event, edit=True)


@route("gst:")
async def grant_store(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    store_id = safe_int(rest)
    store = db.store(store_id)
    if store is None:
        await event.answer("Store not found.", alert=True)
        return
    flow(admin_id, store_id=store_id)
    # If target already in flow, go directly to duration picker
    target = flow_ctx(admin_id).get("target")
    if target:
        await ui.render(event, f"⏳ Duration of access to <b>{esc(store['name'])}</b> for <code>{target}</code>?",
                        keyboards.duration_choices("gd:"), edit=True)
    else:
        ask(admin_id, "grant_target")
        await ui.render(event,
                        f"👑 <b>Grant access → {esc(store['name'])}</b>\n\n"
                        "Send the Telegram user ID (or forward a message from them).",
                        [[Button.inline("🔙 Back", "adm:users")]], edit=True)


@route("gd:")
async def grant_duration(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    pending = flow_ctx(admin_id)
    store_id, target = pending.get("store_id"), pending.get("target")
    if not store_id or not target:
        await event.answer("Start over from 👑 Grant premium.", alert=True)
        return
    if rest == "custom":
        ask(admin_id, "grant_days_value", store_id=store_id, target=target)
        await event.respond("💬 Send the number of days to grant (e.g. 30).")
        return
    seconds = safe_int(rest)
    expires_at = time.time() + seconds if seconds > 0 else None
    store = db.store(store_id)
    access.grant_access(store_id, target, expires_at, source="admin")
    await event.answer("✅ Access granted.", alert=True)
    label = "lifetime" if expires_at is None else f"{seconds // 86400} days"
    await event.respond(f"👑 <code>{target}</code> now has {label} access to "
                        f"<b>{esc(store['name'])}</b>.")
    if expires_at is not None:
        await event.respond(f"<i>Expires on {fmt_ts(expires_at)} ({human_delta(seconds)} from now).</i>")
    try:
        await bot.send_message(
            target,
            f"🎉 You now have access to <b>{esc(store['name'])}</b>!\nUse /start to browse it.",
        )
    except Exception:
        pass
    # Show access list again
    try:
        from app.handlers.manage import show_store_access
        await show_store_access(event, store_id)
    except Exception:
        await send_panel(event, edit=False)


@route("bc:")
async def broadcast_filter(event, rest: str) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    # rest can be like "all", "premium", "free", "store:12"
    filter_value = rest
    if rest.isdigit():
        filter_value = f"store:{rest}"
    targets = access.broadcast_targets(admin_id, filter_value)
    if not targets:
        await event.answer("No users match this filter.", alert=True)
        return
    ask(admin_id, "broadcast", filter=rest, targets=targets)
    await event.respond(f"📢 Send the message to broadcast to <b>{len(targets)}</b> user(s).\n"
                        "<i>/cancel to abort. You can send text, photo, etc. — for media, use dashboard.</i>")


# up: route is handled in manage.py to avoid duplicate
# @route("up:") removed — see manage.py users_page_nav


# ------------------------------------------------------------------ commands
@bot.on(events.NewMessage(func=lambda e: e.is_private, pattern=r"^/session(\s+.+)?$"))
async def session_command(event) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    session_str = (event.pattern_match.group(1) or "").strip()
    if not session_str:
        ask(admin_id, "session_input")
        await event.respond("💬 Send your <b>String Session</b> in the next message.")
        return
    await connect_session(event, session_str)


async def connect_session(event, session_str: str) -> None:
    admin_id = event.sender_id
    existing = runtime.user_clients.get(admin_id)
    if existing is not None:
        try:
            await existing.disconnect()
        except Exception:
            pass
    try:
        from telethon import TelegramClient
        client = TelegramClient(StringSession(session_str), cfg.API_ID, cfg.API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await event.respond("❌ <b>Invalid session string</b> — it is not authorised.")
            return
        me = await client.get_me()
        runtime.register_client(admin_id, client, me.id, me.first_name or "")
        db.save_session(admin_id, session_str, me.id, me.first_name or "")
        await event.respond(f"✅ <b>Session connected</b> — logged in as {esc(me.first_name or '')} "
                            f"(<code>{me.id}</code>).\nPrivate channel files can be served now.")
        log.info("Admin %s connected a session (owner %s)", admin_id, me.id)
        await send_panel(event, edit=False)
    except SessionPasswordNeededError:
        await event.respond("⚠️ This account has 2FA enabled — connect it from a device first, "
                            "then generate the string session again.")
    except Exception as exc:
        log.warning("session connect failed: %s", exc)
        await event.respond(f"⚠️ <b>Session error:</b>\n<code>{esc(str(exc)[:200])}</code>")


@bot.on(events.NewMessage(func=lambda e: e.is_private, pattern=r"^/content$"))
async def content_overview(event) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    stores = db.stores_admin(admin_id)
    if not stores:
        await event.respond("📂 No stores yet — create one from the panel first.")
        return
    lines = ["📁 <b>Your database</b>", ""]
    for store in stores:
        files = db.files_of(store["id"])
        lines.append(f"🏪 <b>{esc(store['name'])}</b> — {len(files)} files")
        for row in files[-5:]:
            lines.append(f"  ├ <a href='https://t.me/{runtime.bot_username}?start=f{row['id']}'>"
                         f"{esc(row['name'])}</a> · 👁 {row['views']}")
        if len(files) > 5:
            lines.append(f"  └ <i>…and {len(files) - 5} more</i>")
        lines.append("")
    await event.respond("\n".join(lines), link_preview=False)


@bot.on(events.NewMessage(func=lambda e: e.is_private, pattern=r"^/done$"))
async def finish_batch(event) -> None:
    admin_id = event.sender_id
    if not access.is_admin(admin_id):
        return
    batch = batch_mode.pop(admin_id, None)
    if batch is None:
        await event.respond("No batch in progress. Tap 📦 Batch link to start one.")
        return
    if not batch:
        await event.respond("Batch was empty — nothing to link.")
        return
    token = db.create_link(admin_id, batch, None, kind="batch")
    await event.respond(
        f"✅ <b>Batch link ready</b> ({len(batch)} files)\n"
        f"<code>https://t.me/{runtime.bot_username}?start=t{token}</code>\n\n"
        "<i>⚠️ Anyone with this link can receive these files, so share it carefully. "
        "It never expires — delete the link any time.</i>"
    )

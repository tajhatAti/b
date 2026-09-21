"""Background workers: daily drip, expired-link cleanup, backups, renewal
reminders and a session watchdog."""
from __future__ import annotations

import asyncio

from app import config as cfg
from app.logger import log
from app.runtime import (blocked_users, bot, register_client,
                         unregister_client, user_clients)
from app.services.access import has_access
from app.services.sender import deliver
from app.services.telegram import safe_call
from app.storage import db
from app.utils import esc, fmt_ts, local_date_str, local_time_str


async def drip_loop() -> None:
    """Every day at the configured time, push N never-sent-before files to the
    store's subscribers. Access is checked at send time, so a user who lost
    premium is dropped instead of getting the files for free."""
    while True:
        try:
            today = local_date_str()
            now_hm = local_time_str()
            for row in db.all_drips():
                store_id = row["store_id"]
                if row["last_sent_date"] == today:
                    continue
                if now_hm < (row["send_time"] or "00:00"):
                    continue
                store = db.store(store_id)
                if store is None:
                    db.set_drip_enabled(store_id, False)
                    continue

                subscribers = db.subscribers(store_id)
                if not subscribers:
                    db.set_drip_last_sent(store_id, today)
                    continue

                already = db.sent_file_ids(store_id)
                fresh = [f for f in db.files_of(store_id) if f["id"] not in already][: max(1, row["count"])]
                if not fresh:
                    db.set_drip_last_sent(store_id, today)
                    log.info("Drip: no fresh files left in store %s", store_id)
                    continue

                delivered = 0
                for user_id in list(subscribers):
                    if user_id in blocked_users:
                        continue
                    if not has_access(store, user_id):
                        db.drop_sub(store_id, user_id)
                        continue
                    try:
                        await safe_call(
                            bot.send_message, user_id,
                            f"📅 <b>Today's drop — {esc(store['name'])}</b>",
                            what="drip_notice", retries=1,
                        )
                        for file_row in fresh:
                            await deliver(user_id, file_row)
                            await asyncio.sleep(cfg.DRIP_SEND_DELAY)
                        delivered += 1
                    except Exception as exc:
                        log.debug("drip to %s failed: %s", user_id, exc)

                db.mark_sent(store_id, [f["id"] for f in fresh])
                db.set_drip_last_sent(store_id, today)
                log.info("Drip sent: store=%s files=%s users=%s", store_id, len(fresh), delivered)
        except Exception as exc:  # never let a worker die
            log.error("drip loop error: %s", exc)
        await asyncio.sleep(30)


async def gc_loop() -> None:
    """Hourly housekeeping: drop expired deep links and stale caches."""
    while True:
        try:
            removed = db.purge_expired_links()
            if removed:
                log.info("Removed %s expired link(s)", removed)
        except Exception as exc:
            log.error("gc loop error: %s", exc)
        await asyncio.sleep(3600)


async def backup_loop() -> None:
    """One JSON + SQLite snapshot per day (keeps the last KEEP_BACKUPS)."""
    while True:
        try:
            today = local_date_str()
            if int(local_time_str()[:2]) >= cfg.BACKUP_HOUR and db.get_meta("last_backup") != today:
                path = db.backup_json(cfg.BACKUP_DIR, cfg.KEEP_BACKUPS)
                db.set_meta("last_backup", today)
                log.info("Daily backup written to %s", path)
        except Exception as exc:
            log.error("backup loop error: %s", exc)
        await asyncio.sleep(600)


async def reminder_loop() -> None:
    """Once a day, nudge users whose premium access is about to end."""
    while True:
        try:
            today = local_date_str()
            if int(local_time_str()[:2]) >= 10 and db.get_meta("last_reminder") != today:
                db.set_meta("last_reminder", today)
                for grant in db.expired_grants(within_seconds=3 * 86400):
                    user_id = grant["user_id"]
                    if user_id in blocked_users:
                        continue
                    try:
                        await safe_call(
                            bot.send_message, user_id,
                            f"⏳ Your access to <b>{esc(grant['store_name'])}</b> ends on "
                            f"{fmt_ts(grant['expires_at'])}.\n"
                            "Message the admin to renew and keep your access.",
                            what="renewal_notice", retries=1,
                        )
                    except Exception:
                        pass
        except Exception as exc:
            log.error("reminder loop error: %s", exc)
        await asyncio.sleep(1800)


async def session_watchdog() -> None:
    """Make sure the userbot sessions stay online; alert the admins if not."""
    while True:
        try:
            for row in db.all_sessions():
                admin_id = row["admin_id"]
                client = user_clients.get(admin_id)
                if client is not None and client.is_connected():
                    continue
                try:
                    from telethon import TelegramClient
                    from telethon.sessions import StringSession
                    client = TelegramClient(StringSession(row["session_str"]),
                                            cfg.API_ID, cfg.API_HASH)
                    await client.connect()
                    if await client.is_user_authorized():
                        me = await client.get_me()
                        register_client(admin_id, client, me.id, me.first_name or "")
                        db.save_session(admin_id, row["session_str"], me.id, me.first_name or "")
                        log.info("Watchdog reconnected session for admin %s", admin_id)
                        await _notify_admin(admin_id, "♻️ <b>Session reconnected</b> — files can be served again.")
                    else:
                        unregister_client(admin_id)
                        log.warning("Session for admin %s is no longer authorized", admin_id)
                        await _notify_admin(
                            admin_id,
                            "⚠️ <b>Session logged out.</b> Send /session to connect a new one.",
                        )
                except Exception as exc:
                    log.warning("watchdog could not restore session for %s: %s", admin_id, exc)
        except Exception as exc:
            log.error("watchdog error: %s", exc)
        await asyncio.sleep(120)


async def _notify_admin(admin_id: int, text: str) -> None:
    try:
        await safe_call(bot.send_message, admin_id, text, what="admin_notice",
                        retries=1, raise_after_retries=False)
    except Exception:
        pass


def start_all() -> list[asyncio.Task]:
    from app.runtime import spawn
    tasks = [
        spawn(drip_loop()),
        spawn(gc_loop()),
        spawn(backup_loop()),
        spawn(reminder_loop()),
        spawn(session_watchdog()),
    ]
    log.info("Started %s background workers", len(tasks))
    return tasks

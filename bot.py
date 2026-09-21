"""Entry point for the Telegram store bot (v2).

Run it with:  python bot.py
Configuration lives in `config.env` (see config.env.example) — never in the code.
"""
from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

from app import config as cfg, runtime
from app.logger import log, setup_logging
from app.services import scheduler
from app.storage import db


async def on_startup(client: TelegramClient) -> None:
    me = await client.get_me()
    runtime.bot_username = me.username or ""
    runtime.bot_id = me.id
    log.info("Bot online as @%s (id=%s)", runtime.bot_username, me.id)

    stats = db.stats()
    log.info("Database: %s users · %s files · %s stores · %s grants",
             stats["users"], stats["files"], stats["stores"], stats["grants"])

    await seed_and_reconnect_sessions()
    scheduler.start_all()


async def seed_and_reconnect_sessions() -> None:
    """Bring every stored userbot session online (and seed the pre-filled one)."""
    from app.runtime import register_client

    if cfg.STRING_SESSION and cfg.ADMIN_IDS and db.session(cfg.ADMIN_IDS[0]) is None:
        db.save_session(cfg.ADMIN_IDS[0], cfg.STRING_SESSION)
        log.info("Seeded the pre-filled STRING_SESSION for admin %s", cfg.ADMIN_IDS[0])

    for row in db.all_sessions():
        admin_id = row["admin_id"]
        try:
            client = TelegramClient(StringSession(row["session_str"]), cfg.API_ID, cfg.API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                log.warning("Stored session for admin %s is no longer authorised", admin_id)
                continue
            me = await client.get_me()
            register_client(admin_id, client, me.id, me.first_name or "")
            db.save_session(admin_id, row["session_str"], me.id, me.first_name or "")
            log.info("Session online for admin %s (as %s, id=%s)", admin_id, me.first_name, me.id)
        except Exception as exc:
            log.error("Could not restore session for admin %s: %s", admin_id, exc)

    runtime.log_session_summary()


async def admin_error_notifier(text: str) -> None:
    client = runtime.get_client()
    if client is None:
        return
    for admin_id in cfg.ADMIN_IDS[:2]:
        try:
            await client.send_message(admin_id, f"⚠️ <b>Bot error</b>\n<code>{text}</code>")
        except Exception:
            pass


def main() -> int:
    setup_logging()
    problems = cfg.validate()
    if problems:
        log.error("Configuration problem(s): %s", "; ".join(problems))
        log.error("Copy config.env.example to config.env and fill in your values.")
        return 2

    db.bind(cfg.DB_FILE)
    imported = db.migrate_legacy(cfg.LEGACY_DB_FILE)
    if imported:
        log.info("Imported the legacy bot_db.json: %s", imported)

    def handle_loop_exception(loop, context):
        log.error("Unhandled loop error: %s (%s)", context.get("message"), context.get("exception"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(handle_loop_exception)

    client = TelegramClient(cfg.SESSION_STATE_FILE, cfg.API_ID, cfg.API_HASH,
                            device_model="Store Bot v2",
                            loop=loop)
    # Every message in this project uses HTML tags — with Telethon's markdown
    # default the users would see literal <b> and ** asterisks everywhere.
    client.parse_mode = "html"
    runtime.set_client(client)

    # Handlers register themselves on import (they need runtime.bot to exist),
    # so we import every handler module for its side effects.
    from importlib import import_module
    for module_name in ("app.handlers.admin", "app.handlers.billing",
                        "app.handlers.extras", "app.handlers.inline",
                        "app.handlers.manage", "app.handlers.messages",
                        "app.handlers.user"):
        import_module(module_name)

    import app.logger as logger_module
    logger_module.error_notifier = admin_error_notifier

    log.info("Connecting to Telegram…")
    try:
        client.start(bot_token=cfg.BOT_TOKEN)
    except Exception as exc:
        log.error("Could not connect to Telegram: %s", exc)
        log.error("Check the server network/firewall, then verify BOT_TOKEN and API_ID/API_HASH "
                  "in config.env.")
        return 3

    log.info("Starting background workers and sessions…")
    loop.run_until_complete(on_startup(client))

    try:
        client.run_until_disconnected()
    except KeyboardInterrupt:
        log.info("Shutting down on user request")
    finally:
        try:
            loop.run_until_complete(disconnect_all(client))
        except Exception:
            pass
    return 0


async def disconnect_all(client: TelegramClient) -> None:
    for admin_id, userbot in list(runtime.user_clients.items()):
        try:
            await userbot.disconnect()
        except Exception:
            pass
    try:
        await client.disconnect()
    except Exception:
        pass
    log.info("Bye 👋")


if __name__ == "__main__":
    sys.exit(main())

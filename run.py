"""Run the Telegram bot **and** the website in one process.

This is the entry point hosting panels expect (CodeNest, Render, Railway, a VPS…):

    python run.py

What it does
    1. binds the SQLite database,
    2. starts the website (public store front + admin panel) on `$PORT`
       — so the hosting panel gets its HTTP port and the site is live even while
       the bot is reconnecting,
    3. starts the Telegram bot in the same event loop, with automatic retry: if
       Telegram is unreachable the site keeps working and the bot connects by
       itself as soon as the network is back.

Everything the panel does (broadcasts, orders, files) talks to the same database
as the bot, so the two halves never disagree.
"""
from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient

from app import config as cfg, runtime
from app.logger import log, setup_logging
from app.services import secrets_guard
from app.storage import db

HANDLER_MODULES = ("app.handlers.admin", "app.handlers.billing",
                   "app.handlers.extras", "app.handlers.inline",
                   "app.handlers.manage", "app.handlers.messages",
                   "app.handlers.user")


async def secrets_watchdog() -> None:
    """Warn (log + Telegram) when secrets sit inside a committed config.env.

    Purely informative — it can never take the bot or the website down.
    """
    try:
        report = secrets_guard.log_report()
        if report.get("secrets"):
            await secrets_guard.notify_admins_once_per_day()
    except Exception as exc:
        log.debug("secrets watchdog skipped: %s", exc)


async def bot_worker() -> None:
    """Connect the bot and keep it connected. Never takes the website down."""
    from importlib import import_module

    from bot import admin_error_notifier, on_startup

    client = TelegramClient(cfg.SESSION_STATE_FILE, cfg.API_ID, cfg.API_HASH,
                            device_model="Store Bot v2")
    client.parse_mode = "html"
    runtime.set_client(client)

    # Handlers register themselves on import — they must be imported *after* the
    # client exists, otherwise the buttons would answer to nobody.
    for module_name in HANDLER_MODULES:
        import_module(module_name)

    import app.logger as logger_module
    logger_module.error_notifier = admin_error_notifier

    started = False
    while True:
        try:
            if not client.is_connected():
                await client.connect()
            if not await client.is_user_authorized():
                log.info("Signing in to Telegram…")
                await client.start(bot_token=cfg.BOT_TOKEN)
            if not started:
                await on_startup(client)
                started = True
                log.info("Bot is online — website + bot are both running ✅")
            await client.run_until_disconnected()
        except Exception as exc:
            log.error("Telegram connection problem: %s", exc)
            log.error("The website stays online; retrying the bot in 15 seconds…")
        await asyncio.sleep(15)


async def web_worker() -> None:
    """Serve the store front + admin panel on WEB_HOST:WEB_PORT ($PORT)."""
    import uvicorn

    from web.dashboard import app as web_app

    config = uvicorn.Config(web_app, host=cfg.WEB_HOST, port=cfg.WEB_PORT,
                            log_level="info", access_log=False)
    server = uvicorn.Server(config)
    log.info("Website listening on http://%s:%s", cfg.WEB_HOST, cfg.WEB_PORT)
    await server.serve()


async def run() -> int:
    db.bind(cfg.DB_FILE)
    imported = db.migrate_legacy(cfg.LEGACY_DB_FILE)
    if imported:
        log.info("Imported the legacy bot_db.json: %s", imported)

    tasks = [asyncio.create_task(web_worker()),
             asyncio.create_task(secrets_watchdog())]
    problems = cfg.validate()
    if problems:
        log.error("Bot not started — configuration problem(s): %s", "; ".join(problems))
        log.error("The website is running; fix config.env and restart to bring the bot online.")
    else:
        tasks.append(asyncio.create_task(bot_worker()))

    await asyncio.gather(*tasks)
    return 0


def main() -> int:
    setup_logging()
    log.info("Starting store bot + website (run.py)…")
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Shutting down on user request")
        return 0


if __name__ == "__main__":
    sys.exit(main())

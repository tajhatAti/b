"""File delivery service.

Two strategies, tried in order:

1. **Direct** — the bot itself can read the source message (it is an admin in the
   channel, or the file was uploaded to the bot). Zero session usage, fastest.
2. **JIT forwarding** — a userbot session forwards the message into the bot's DM
   and we catch it as a live update. Bots cannot fetch history, so we subscribe
   *before* forwarding. The temporary copy is deleted right after sending, so the
   bot DM no longer fills up with duplicates.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from telethon.errors import FloodWaitError

from app import config as cfg
from app.logger import log
from app import runtime
from app.runtime import (bot, bot_entity_cache, jit_locks, session_owners,
                         user_clients, wait_for_forward)
from app.storage import db
from app.services.telegram import safe_call

OK, NOT_FOUND, NO_SESSION, FLOOD, ERROR = "ok", "not_found", "no_session", "flood", "error"


@dataclass
class Delivery:
    ok: bool
    reason: str = OK
    detail: str = ""


async def _send_media(chat_id: int, media, caption_text: str) -> None:
    """Send media with forward-protection.

    Note: `send_file(..., noforwards=True)` is silently ignored by Telethon (it
    accepts **kwargs but never forwards them), so we build the request ourselves
    and fall back to `send_file` only if that fails.
    """
    try:
        from telethon import utils
        from telethon.extensions import html
        from telethon.tl import functions

        peer = await safe_call(bot.get_input_entity, chat_id, what="get_input_entity", retries=1)
        input_media = utils.get_input_media(media)
        text, entities = html.parse(caption_text or "")
        await safe_call(
            bot, functions.messages.SendMediaRequest(
                peer=peer, media=input_media, message=text,
                entities=entities, noforwards=True,
            ),
            what="send_media(noforwards)",
        )
        return
    except Exception as exc:
        log.debug("noforwards send failed (%s) — falling back to send_file", exc)

    await safe_call(
        bot.send_file, chat_id, media,
        caption=caption_text or None, what="send_file",
    )


async def _try_direct(file_row: dict, chat_id: int, caption_text: str) -> bool:
    try:
        msg = await safe_call(bot.get_messages, file_row["chat_id"],
                              ids=file_row["msg_id"], what="get_messages", retries=1)
    except Exception as exc:
        log.debug("direct fetch failed for file %s: %s", file_row["id"], exc)
        return False
    if not msg or not getattr(msg, "media", None):
        return False
    await _send_media(chat_id, msg.media, caption_text)
    db.bump_views(file_row["id"])
    return True


async def _try_jit(file_row: dict, chat_id: int, caption_text: str) -> Delivery:
    store = db.store(file_row["store_id"])
    if not store:
        return Delivery(False, NOT_FOUND, "store removed")
    admin_id = store["admin_id"]
    client = user_clients.get(admin_id)
    if client is None:
        return Delivery(False, NO_SESSION, "no session for this store owner")
    if not runtime.bot_username:
        return Delivery(False, ERROR, "bot username not resolved yet")

    try:
        async with jit_locks[admin_id]:
            # Resolve the bot entity once per session — resolving on every click
            # is what used to trigger username FloodWaits.
            if admin_id not in bot_entity_cache:
                bot_entity_cache[admin_id] = await safe_call(
                    client.get_entity, runtime.bot_username, what="get_entity")
            bot_entity = bot_entity_cache[admin_id]

            fetch_peer = session_owners.get(admin_id, admin_id)
            waiter = asyncio.ensure_future(wait_for_forward(fetch_peer))
            try:
                forwarded = await safe_call(
                    client.forward_messages,
                    bot_entity, file_row["msg_id"], file_row["chat_id"],
                    what="forward_messages",
                )
            except Exception:
                waiter.cancel()
                raise
            incoming = await waiter

            if not incoming or not getattr(incoming, "media", None):
                return Delivery(False, ERROR, "forwarded copy never arrived")

            try:
                await _send_media(chat_id, incoming.media, caption_text)
            finally:
                if cfg.CLEANUP_JIT_COPY:
                    try:
                        await safe_call(incoming.delete, what="delete_copy",
                                        raise_after_retries=False)
                    except Exception as exc:
                        log.debug("could not delete temporary copy: %s", exc)
                    # the outgoing forward from `forwarded` is the same chat copy
                    if forwarded is not None and getattr(forwarded, "id", None) != incoming.id:
                        try:
                            await safe_call(forwarded.delete, what="delete_copy",
                                            raise_after_retries=False)
                        except Exception:
                            pass
            db.bump_views(file_row["id"])
            return Delivery(True)
    except FloodWaitError:
        return Delivery(False, FLOOD)
    except Exception as exc:
        log.warning("JIT delivery failed for file %s: %s", file_row["id"], exc)
        return Delivery(False, ERROR, str(exc)[:200])


async def deliver(chat_id: int, file_row: dict, caption_text: str | None = None) -> Delivery:
    """Send one stored file to `chat_id`."""
    if not file_row:
        return Delivery(False, NOT_FOUND)
    if caption_text is None:
        caption_text = runtime.caption()
    caption = caption_text or ""

    try:
        if await _try_direct(file_row, chat_id, caption):
            return Delivery(True)
    except FloodWaitError:
        return Delivery(False, FLOOD)
    except Exception as exc:
        log.debug("direct send failed for file %s: %s", file_row["id"], exc)

    result = await _try_jit(file_row, chat_id, caption)
    if result.ok or result.reason in (NOT_FOUND, NO_SESSION, FLOOD):
        return result
    # Retry the direct path once more: the userbot may have just pulled the file
    # into the bot's reach (e.g. right after the session reconnected).
    try:
        if await _try_direct(file_row, chat_id, caption):
            return Delivery(True)
    except Exception:
        pass
    return result


async def deliver_file_id(chat_id: int, file_id: int, caption_text: str | None = None) -> Delivery:
    row = db.file(file_id)
    if not row:
        return Delivery(False, NOT_FOUND)
    return await deliver(chat_id, row, caption_text)


async def deliver_many(chat_id: int, file_rows: list[dict],
                       caption_text: str | None = None, delay: float | None = None) -> int:
    sent = 0
    for row in file_rows:
        result = await deliver(chat_id, row, caption_text)
        if result.ok:
            sent += 1
        await asyncio.sleep(delay if delay is not None else 0.4)
    return sent

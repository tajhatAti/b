"""Media cache — make every file reachable by the *bot* exactly once.

The old behaviour
-----------------
When the bot could not read a source message it asked the store owner's userbot
to forward the file into the bot's DM and **deleted the copy** right after the
send. With a few users clicking at once the userbot forwarded + deleted the same
video over and over → “বার বার বোর্ডে ফরওয়ার্ড হয়ে ডিলিট হচ্ছিল” → FloodWait, and
some users received nothing.

The new behaviour
-----------------
A file is pulled into a bot-readable place **once** and remembered
(`files.mirror_chat/mirror_msg`). After that every delivery — store click, search,
limited link, broadcast to 5,000 people — is a plain `send_file` by the bot: no
session traffic, no forward, no delete, no FloodWait.

Mirror target, in order:
    1. the storage channel configured in the panel (`STORAGE_CHANNEL`)
    2. the store owner's DM with the bot (always available)
    3. any other connected userbot session

Techniques borrowed from the owner's own forwarder (`website.example.py`):
   * ranged multi-connection download for big files
   * re-upload with `supports_streaming=True` + thumbnail (restricted sources)
   * FloodWait → wait, then retry on another session
"""
from __future__ import annotations

import asyncio
import os
import tempfile

from telethon.errors import FloodWaitError

from app import runtime
from app.logger import log
from app.services import settings
from app.services.telegram import safe_call
from app.storage import db

CHUNK = 1024 * 1024
MAX_RANGED_THREADS = 4


def _owner_session(file_row: dict, store: dict | None):
    """The userbot session that should do the heavy lifting for this file."""
    from app.runtime import user_clients
    if store and store.get("admin_id") in user_clients:
        return user_clients[store["admin_id"]]
    for _admin_id, client in list(user_clients.items()):
        return client
    return None


def mirror_peer(file_row: dict, store: dict | None):
    """Where the bot-side copy should live."""
    target = (settings.get_str("STORAGE_CHANNEL") or "").strip()
    if target:
        return target
    if store and store.get("admin_id"):
        return int(store["admin_id"])
    return None


def mirror_targets(file_row: dict) -> list[tuple[int, int]]:
    """Every known bot-side location of this file, best first."""
    out: list[tuple[int, int]] = []
    if file_row.get("mirror_chat") and file_row.get("mirror_msg"):
        out.append((int(file_row["mirror_chat"]), int(file_row["mirror_msg"])))
    if file_row.get("chat_id") and file_row.get("msg_id"):
        out.append((int(file_row["chat_id"]), int(file_row["msg_id"])))
    return out


async def _media_from(client, chat_id: int, msg_id: int):
    try:
        msg = await safe_call(client.get_messages, chat_id, ids=msg_id,
                              what="mirror_fetch", retries=1)
    except Exception as exc:
        log.debug("mirror fetch %s/%s failed: %s", chat_id, msg_id, exc)
        return None
    if msg is None or not getattr(msg, "media", None):
        return None
    return msg


async def _download(client, message, path: str, size: int | None = None) -> bool:
    """Download a message's media to `path` (ranged for big files)."""
    try:
        if size and size > 5 * CHUNK:
            await _download_ranged(client, message, path, size)
            return os.path.exists(path) and os.path.getsize(path) > 0
    except Exception as exc:
        log.debug("ranged download failed (%s) — falling back", exc)
    try:
        await client.download_media(message, file=path)
        return os.path.exists(path) and os.path.getsize(path) > 0
    except Exception as exc:
        log.warning("download failed: %s", exc)
        return False


async def _download_ranged(client, message, path: str, size: int) -> None:
    """Chunked parallel download (the trick from the owner's own forwarder).

    Telethon serialises `iter_download` per client, so the parallelism comes from
    splitting the file into ranges that are written at their own offsets.
    """
    parts = max(1, min(MAX_RANGED_THREADS, max(1, size // (20 * CHUNK))))
    chunk = size // parts

    lock = asyncio.Lock()

    async def fetch(start: int, length: int) -> None:
        handle = await asyncio.to_thread(open, path, "r+b")
        try:
            async for blob in client.iter_download(message.media, offset=start, limit=length,
                                                   chunk_size=CHUNK, request_size=CHUNK):
                async with lock:
                    await asyncio.to_thread(_write_at, handle, start, blob)
                start += len(blob)
        finally:
            await asyncio.to_thread(handle.close)

    # Pre-allocate so every worker writes at its own offset.
    with open(path, "wb") as handle:
        handle.truncate(size)
    await asyncio.gather(*[fetch(i * chunk, chunk if i < parts - 1 else size - i * chunk)
                           for i in range(parts)])


def _write_at(handle, offset: int, blob: bytes) -> None:
    handle.seek(offset)
    handle.write(blob)
    handle.flush()


async def ensure_mirror(file_row: dict, store: dict | None = None, force: bool = False) -> dict:
    """Make sure the bot can send this file. Returns the (possibly updated) row."""
    if file_row is None:
        return {}
    if file_row.get("mirror_chat") and file_row.get("mirror_msg") and not force:
        return file_row

    # 1. Maybe the source itself is already bot-readable — then no mirror needed.
    try:
        from app.runtime import bot
        msg = await _media_from(bot, file_row["chat_id"], file_row["msg_id"])
        if msg is not None:
            db.set_mirror(file_row["id"], file_row["chat_id"], file_row["msg_id"])
            return db.file(file_row["id"]) or file_row
    except Exception as exc:
        log.debug("source not readable by the bot: %s", exc)

    client = _owner_session(file_row, store)
    if client is None:
        return file_row
    peer = mirror_peer(file_row, store)
    if peer is None:
        log.debug("no mirror target for file %s", file_row["id"])
        return file_row

    source = await _media_from(client, int(file_row["chat_id"]), int(file_row["msg_id"]))
    if source is None:
        return file_row

    size = int(getattr(getattr(source, "file", None), "size", 0) or file_row.get("bytes") or 0)
    max_mb = settings.get_int("MIRROR_MAX_MB", 1900) or 0
    if max_mb and size and size > max_mb * 1024 * 1024:
        log.warning("file %s is %s MB — bigger than MIRROR_MAX_MB (%s)", file_row["id"],
                    size // (1024 * 1024), max_mb)
        return file_row

    # 2. Try a cheap in-app copy (Telegram side, no download)…
    try:
        from app.runtime import bot
        copied = await safe_call(client.send_file, peer, source.media,
                                 what="mirror_copy", retries=1)
        if copied is not None and getattr(copied, "media", None):
            db.set_mirror(file_row["id"], int(getattr(copied, "chat_id", peer) or peer),
                          int(copied.id))
            log.info("mirrored file %s → %s/%s (copy)", file_row["id"], peer, copied.id)
            return db.file(file_row["id"]) or file_row
    except FloodWaitError:
        raise
    except Exception as exc:
        log.debug("in-app mirror copy failed for %s: %s", file_row["id"], exc)

    # 3. …otherwise download and re-upload (works for restricted/forward-protected).
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    tmp.close()
    try:
        if not await _download(client, source, tmp.name, size or None):
            return file_row
        sent = await safe_call(
            client.send_file, peer, tmp.name,
            caption=(file_row.get("name") or "")[:200],
            supports_streaming=file_row.get("kind") == "Video",
            attributes=None,
            what="mirror_upload", retries=1,
        )
        if sent is not None and getattr(sent, "media", None):
            db.set_mirror(file_row["id"], int(getattr(sent, "chat_id", peer) or peer), int(sent.id))
            db.set_file_bytes(file_row["id"], os.path.getsize(tmp.name))
            log.info("mirrored file %s → %s/%s (re-upload, %.1f MB)", file_row["id"], peer,
                     sent.id, os.path.getsize(tmp.name) / 1048576)
            return db.file(file_row["id"]) or file_row
    except FloodWaitError:
        raise
    except Exception as exc:
        log.warning("mirror upload failed for %s: %s", file_row["id"], exc)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return file_row


async def warm_mirrors(limit: int = 20, delay: float = 1.5) -> dict:
    """Pre-mirror files that still need it (called by the scheduler / a button).

    Doing it in the background keeps every later click instant and stops a
    broadcast from hammering the session.
    """
    from app.services.sender import mirror_ready
    done = failed = 0
    for row in db.missing_mirrors(limit):
        try:
            updated = await ensure_mirror(row, db.store(row["store_id"]))
            if updated.get("mirror_msg"):
                done += 1
            else:
                failed += 1
        except FloodWaitError as exc:
            log.warning("mirror warm-up hit FloodWait (%ss) — stopping for now", exc.seconds)
            break
        except Exception as exc:
            failed += 1
            log.debug("mirror warm-up failed for %s: %s", row["id"], exc)
        await asyncio.sleep(delay)
    return {"mirrored": done, "failed": failed, "ready": mirror_ready()}


async def copy_between_chats(source_chat: int, msg_id: int, target_chat: int,
                             client=None) -> int | None:
    """Copy one message to another chat without a visible forward (used by the
    “add video” flow and by the channel composer)."""
    from app.runtime import bot
    worker = client or bot
    message = await _media_from(worker, source_chat, msg_id)
    if message is None:
        return None
    try:
        sent = await safe_call(worker.send_file, target_chat, message.media,
                               caption=getattr(message, "message", "") or None,
                               what="copy_media", retries=1)
        return int(getattr(sent, "id", 0) or 0) if sent else None
    except Exception as exc:
        log.debug("copy_between_chats failed: %s", exc)
        return None


def stats() -> dict:
    total = len(db.mirrored_files(limit=1_000_000)) and db.count_files()
    mirrored = db.count_mirrors()
    return {"files": total, "mirrored": mirrored, "missing": max(0, total - mirrored)}

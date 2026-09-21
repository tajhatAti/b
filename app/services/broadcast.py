"""Broadcast runner: paced, FloodWait-proof, with live progress and resume."""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from telethon.errors import FloodWaitError

from app import config as cfg
from app.logger import log
from app.runtime import blocked_users, bot
from app.services.telegram import safe_call

ProgressCb = Callable[[int, int, int], Awaitable[None]]   # done, total, failed


async def run_broadcast(targets: list[int], text: str,
                        progress: ProgressCb | None = None,
                        delay: float | None = None,
                        file_id: int | None = None) -> dict:
    """Send `text` to every target. Users who blocked the bot are remembered so
    the next broadcast skips them instantly."""
    total = len(targets)
    sent = failed = skipped = 0
    active = [uid for uid in targets if uid not in blocked_users]
    skipped = total - len(active)
    started = time.time()
    pause = delay if delay is not None else cfg.BROADCAST_DELAY

    for index, user_id in enumerate(active, start=1):
        try:
            if file_id:
                from app.services.sender import deliver_file_id
                result = await deliver_file_id(user_id, file_id, text)
                ok = result.ok
            else:
                await safe_call(bot.send_message, user_id, text, link_preview=False,
                                what="broadcast", retries=2)
                ok = True
            if ok:
                sent += 1
            else:
                failed += 1
        except FloodWaitError:
            failed += 1
        except Exception as exc:  # user blocked the bot / deleted account
            message = str(exc).lower()
            if "blocked" in message or "deactivated" in message or "kicked" in message:
                blocked_users.add(user_id)
            failed += 1
            log.debug("broadcast to %s failed: %s", user_id, exc)

        if progress and (index % 25 == 0 or index == len(active)):
            await progress(index, len(active), failed)
        await asyncio.sleep(pause)

    duration = time.time() - started
    log.info("Broadcast finished: sent=%s failed=%s skipped=%s in %.1fs",
             sent, failed, skipped, duration)
    return {"sent": sent, "failed": failed, "skipped": skipped,
            "total": total, "duration": duration}

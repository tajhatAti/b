"""Broadcast studio v3 — high level, persistent, flood-proof campaigns.

Why a *campaign* instead of the old "loop over a list":

* **Queue in the database** — every target is a row in `campaign_items`. If the
  hosting restarts (or the process is killed mid-broadcast) the campaign is
  resumed automatically, and no user gets the message twice.
* **Audience segments** — all / premium / free / one store / people who never
  bought / drip subscribers. Resolved once, then frozen into the queue.
* **Any content** — plain text, one video, or several files per message. This is
  what makes “broadcast this video” work from the file manager or the website.
* **Flood-proof pacing** — a delay between messages, a longer pause every
  `BROADCAST_BATCH` messages, and `safe_call` waits out any FloodWait instead of
  dropping the message.
* **Live progress + cancel** — progress callbacks for the bot message and the web
  panel, and `cancel_campaign()` stops cleanly mid-run.
* **Personalisation + preview** — `{name}` becomes the user's name; `preview()`
  shows exactly what the message will look like before anything is sent.
* **Test send** — send the campaign to the admins only and check it first.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from telethon.errors import FloodWaitError

from app import config as cfg
from app.logger import log
from app.runtime import blocked_users, bot, spawn
from app.services.telegram import safe_call
from app.storage import db

ProgressCb = Callable[[int, int, int], Awaitable[None]]   # done, total, failed

# ------------------------------------------------------------------ statuses
DRAFT = "draft"
QUEUED = "queued"          # created, waiting for the bot to come online
SCHEDULED = "scheduled"    # waiting for `scheduled_at`
RUNNING = "running"
DONE = "done"
CANCELLED = "cancelled"
FAILED = "failed"

ACTIVE_STATUSES = (QUEUED, SCHEDULED, RUNNING)

# ----------------------------------------------------------------- audiences
AUDIENCE_LABELS: dict[str, str] = {
    "all": "📣 All users",
    "premium": "💎 Premium buyers",
    "free": "🆓 Free users",
}
STORE_AUDIENCE_LABELS = {
    "store": "🏪 Everyone in this store",
    "sub": "🔔 Subscribers of this store",
    "nosale": "🎯 Never bought this store",
}


def audience_label(spec: str) -> str:
    spec = (spec or "all").strip()
    if spec in AUDIENCE_LABELS:
        return AUDIENCE_LABELS[spec]
    kind, _, rid = spec.partition(":")
    return f"{STORE_AUDIENCE_LABELS.get(kind, kind)} #{rid}"


def resolve_audience(admin_id: int, spec: str) -> list[int]:
    """Turn an audience code into a list of Telegram user ids."""
    from app.services.access import broadcast_targets

    spec = (spec or "all").strip() or "all"
    kind, _, raw = spec.partition(":")
    # admin_id 0/None means “whole installation” (used by the website panel)
    scope = admin_id or None
    if kind in ("all", "premium", "free"):
        targets = broadcast_targets(scope, kind)
    elif kind in ("store", "sub", "nosale"):
        store_id = int(raw or 0)
        store = db.store(store_id)
        if store is None:
            return []
        if kind == "sub":
            targets = db.subscribers(store_id)
        elif kind == "nosale":
            granted = {g["user_id"] for g in db.store_grants(store_id)}
            targets = [uid for uid in db.all_user_ids() if uid not in granted]
        else:
            targets = broadcast_targets(admin_id, f"store:{store_id}")
    else:
        targets = broadcast_targets(admin_id, spec)
    # never broadcast to the admins themselves, never twice
    return [uid for uid in dict.fromkeys(int(u) for u in targets)
            if uid not in cfg.ADMIN_IDS]


# ------------------------------------------------------------------ campaigns
def create_campaign(admin_id: int, *, text: str = "", title: str = "",
                    audience: str = "all", files: list[int] | None = None,
                    scheduled_at: float | None = None,
                    start: bool = True) -> dict:
    """Build a campaign and (optionally) put it in the queue right away."""
    files = [int(f) for f in (files or []) if f]
    if scheduled_at and scheduled_at > time.time():
        status = SCHEDULED
    else:
        status = QUEUED if start else DRAFT
    if not title:
        title = (text or "").strip().splitlines()[0][:40] if text else f"{len(files)} file(s)"
    campaign_id = db.create_campaign(
        admin_id, title=title, text=text or "", files=files,
        audience=audience or "all", status=status, scheduled_at=scheduled_at,
    )
    targets = resolve_audience(admin_id, audience or "all")
    db.add_campaign_items(campaign_id, targets)
    db.campaign_counts(campaign_id)
    log.info("Campaign #%s created by admin %s → %s targets (%s)",
             campaign_id, admin_id, len(targets), audience)
    return db.campaign(campaign_id) or {}


def preview(campaign: dict, sample_name: str = "রহিম") -> str:
    """Exactly the text a user will receive (HTML), for the preview screen."""
    text = personalize(campaign.get("text") or "", sample_name, "")
    if not text and campaign.get("file_ids"):
        text = "🎬 <b>New upload</b>"
    return text


def personalize(text: str, name: str, username: str = "") -> str:
    if not text:
        return ""
    first = (name or "").strip().split(" ")[0] if name else ""
    return (text.replace("{name}", first or "bondhu")
                .replace("{username}", ("@" + username) if username else (first or "bondhu")))


# ------------------------------------------------------------------- sending
class BlockedByUser(Exception):
    """The user blocked the bot / deleted the account — stop retrying them."""


async def _send_to_user(campaign: dict, user_id: int) -> None:
    """Send one campaign message. Raises on failure so the queue can record it."""
    file_ids = campaign.get("file_ids") or []
    text = campaign.get("text") or ""
    if file_ids:
        from app.services.sender import deliver_file_id

        user = db.user(user_id) or {}
        body = personalize(text, user.get("name") or "", user.get("username") or "")
        for index, file_id in enumerate(file_ids[: cfg.BROADCAST_MAX_FILES]):
            result = await deliver_file_id(user_id, file_id,
                                           body if index == 0 else (runtime_caption() or ""))
            if not result.ok:
                raise RuntimeError(f"file {file_id}: {result.reason}")
            if index + 1 < min(len(file_ids), cfg.BROADCAST_MAX_FILES):
                await asyncio.sleep(cfg.BROADCAST_MEDIA_DELAY)
        return

    if not text:
        raise RuntimeError("empty message")
    user = db.user(user_id) or {}
    body = personalize(text, user.get("name") or "", user.get("username") or "")
    await safe_call(bot.send_message, user_id, body, link_preview=False,
                    what="broadcast", retries=2)


def runtime_caption() -> str:
    from app import runtime
    return runtime.caption()


def _classify(exc: Exception) -> str:
    message = str(exc).lower()
    for needle in ("blocked", "deactivated", "kicked", "user is deleted",
                   "privacy", "not enough rights", "chat not found", "user not found"):
        if needle in message:
            return "blocked"
    return "failed"


async def _pause(campaign_id: int, index: int) -> bool:
    """Pacing between messages. Returns False when the campaign was cancelled."""
    await asyncio.sleep(cfg.BROADCAST_DELAY)
    if cfg.BROADCAST_BATCH and index % cfg.BROADCAST_BATCH == 0:
        log.info("Campaign #%s: pause after %s messages", campaign_id, index)
        await asyncio.sleep(cfg.BROADCAST_BATCH_PAUSE)
    return not is_cancelled(campaign_id)


# ---------------------------------------------------------------- run engine
_runners: dict[int, asyncio.Task] = {}
_cancel: set[int] = set()


def is_running(campaign_id: int) -> bool:
    task = _runners.get(campaign_id)
    return bool(task and not task.done())


def is_cancelled(campaign_id: int) -> bool:
    return campaign_id in _cancel


def cancel_campaign(campaign_id: int) -> bool:
    """Ask a running campaign to stop. The runner finalises the status."""
    if not is_running(campaign_id):
        db.update_campaign(campaign_id, status=CANCELLED, finished_at=time.time())
        return False
    _cancel.add(campaign_id)
    log.info("Campaign #%s: cancel requested", campaign_id)
    return True


async def run_campaign(campaign_id: int, progress: ProgressCb | None = None,
                       notify_admin: bool = True) -> dict:
    """Send every pending item of a campaign. Safe to call again after a restart."""
    campaign = db.campaign(campaign_id)
    if campaign is None:
        return {"ok": False, "error": "campaign not found"}
    if is_running(campaign_id):
        return {"ok": False, "error": "already running"}

    counts = db.campaign_counts(campaign_id)
    total = counts["total"] or 0
    pending = db.campaign_user_ids(campaign_id, "pending")
    db.update_campaign(campaign_id, status=RUNNING,
                       started_at=campaign.get("started_at") or time.time(),
                       last_error="")
    log.info("Campaign #%s started: %s pending of %s", campaign_id, len(pending), total)

    started = time.time()
    already_done = counts["done"]
    index = 0
    failed_here = 0

    for user_id in pending:
        if is_cancelled(campaign_id):
            break
        index += 1
        if user_id in blocked_users:
            db.mark_campaign_item(campaign_id, user_id, "blocked", "known blocker")
        else:
            try:
                await _send_to_user(campaign, user_id)
                db.mark_campaign_item(campaign_id, user_id, "sent")
            except FloodWaitError as exc:
                # safe_call already waits; this is the "safety cap" escape hatch.
                db.mark_campaign_item(campaign_id, user_id, "failed", f"flood {exc.seconds}s")
                failed_here += 1
            except Exception as exc:
                kind = _classify(exc)
                if kind == "blocked":
                    blocked_users.add(user_id)
                else:
                    failed_here += 1
                db.mark_campaign_item(campaign_id, user_id, kind, str(exc))
                log.debug("campaign #%s → %s %s", campaign_id, user_id, kind)

        if progress and (index % 10 == 0 or index == len(pending)):
            fresh = db.campaign_counts(campaign_id)
            await progress(fresh["done"], fresh["total"], fresh["failed"])
        if not await _pause(campaign_id, index):
            break

    live = db.campaign_counts(campaign_id)
    cancelled = is_cancelled(campaign_id)
    status = CANCELLED if cancelled else (DONE if live["pending"] == 0 else DONE)
    duration = time.time() - started
    db.update_campaign(campaign_id, status=status, finished_at=time.time())
    _cancel.discard(campaign_id)

    result = {
        "ok": True, "campaign_id": campaign_id, "status": status,
        "sent": live["sent"], "failed": live["failed"], "blocked": live["blocked"],
        "pending": live["pending"], "total": live["total"], "duration": duration,
        "skipped": already_done, "this_run": index, "failed_now": failed_here,
    }
    log.info("Campaign #%s %s: sent=%s failed=%s blocked=%s in %.1fs",
             campaign_id, status, live["sent"], live["failed"], live["blocked"], duration)
    if notify_admin and campaign.get("admin_id"):
        await notify_owner(campaign, result)
    return result


def start_campaign(campaign_id: int, progress: ProgressCb | None = None,
                   notify_admin: bool = True) -> asyncio.Task:
    """Run a campaign in the background (never blocks the handler)."""
    task = spawn(run_campaign(campaign_id, progress=progress, notify_admin=notify_admin))
    _runners[campaign_id] = task
    task.add_done_callback(lambda _t: _runners.pop(campaign_id, None))
    return task


async def notify_owner(campaign: dict, result: dict) -> None:
    """Tell the admin who created the campaign how it went."""
    admin_id = campaign.get("admin_id")
    if not admin_id:
        return
    text = (
        f"📢 <b>Broadcast finished</b> — #{result.get('campaign_id')}\n"
        f"🎯 Audience: {audience_label(campaign.get('audience') or 'all')}\n"
        f"📨 Sent: <b>{result.get('sent', 0)}</b> · ⚠️ Failed: <b>{result.get('failed', 0)}</b>\n"
        f"🚫 Blocked us: <b>{result.get('blocked', 0)}</b> · 🕓 {int(result.get('duration', 0))}s"
    )
    try:
        await safe_call(bot.send_message, admin_id, text, what="campaign_report",
                        retries=1, raise_after_retries=False)
    except Exception as exc:
        log.debug("could not report campaign to admin: %s", exc)


async def test_send(campaign_id: int, admin_ids: list[int]) -> dict:
    """Send the campaign message to the admins only (nobody else)."""
    campaign = db.campaign(campaign_id)
    if campaign is None:
        return {"ok": False, "error": "not found"}
    sent = failed = 0
    for admin_id in admin_ids:
        try:
            await _send_to_user(campaign, admin_id)
            sent += 1
        except Exception as exc:
            failed += 1
            log.warning("test send of campaign #%s to %s failed: %s",
                        campaign_id, admin_id, exc)
    return {"ok": sent > 0, "sent": sent, "failed": failed}


def resume_unfinished() -> int:
    """Restart campaigns that were interrupted (called at boot / by scheduler)."""
    resumed = 0
    for campaign in db.queued_campaigns():
        if is_running(campaign["id"]):
            continue
        start_campaign(campaign["id"])
        resumed += 1
    if resumed:
        log.info("Resuming %s unfinished campaign(s)", resumed)
    return resumed


def campaign_progress(campaign_id: int) -> dict:
    """Small JSON friendly snapshot for the web panel / bot status message."""
    campaign = db.campaign(campaign_id)
    if campaign is None:
        return {}
    counts = db.campaign_counts(campaign_id, sync=False)
    done = counts["done"]
    total = counts["total"] or 1
    percent = int(done * 100 / total) if total else 0
    return {
        "id": campaign_id,
        "status": campaign["status"],
        "running": is_running(campaign_id),
        "sent": counts["sent"], "failed": counts["failed"],
        "blocked": counts["blocked"], "pending": counts["pending"],
        "total": counts["total"], "percent": percent,
    }


# ------------------------------------------------------- legacy entry points
async def run_broadcast(targets: list[int], text: str,
                        progress: ProgressCb | None = None,
                        delay: float | None = None,
                        file_id: int | None = None) -> dict:
    """Old API kept for compatibility (used by the simple bot flow and tests).

    Internally it now routes through the campaign engine, so behaviour
    (blocked-user memory, flood handling, pacing) is identical everywhere.
    """
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
                if not ok:
                    failed += 1
                else:
                    sent += 1
            else:
                await safe_call(bot.send_message, user_id, text, link_preview=False,
                                what="broadcast", retries=2)
                sent += 1
        except FloodWaitError:
            failed += 1
        except Exception as exc:  # user blocked the bot / deleted account
            if _classify(exc) == "blocked":
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

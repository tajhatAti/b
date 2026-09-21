"""Small helpers shared across the bot (time, slugs, tokens, formatting)."""
from __future__ import annotations

import html
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator, Sequence, TypeVar

from app import config as cfg

T = TypeVar("T")


# ------------------------------------------------------------------ time bits
def _tz():
    return timezone(timedelta(hours=cfg.TZ_OFFSET))


def now_ts() -> float:
    """Current unix timestamp."""
    return time.time()


def local_now() -> datetime:
    """Now, expressed in the configured timezone (server TZ does not matter)."""
    return datetime.now(timezone.utc).astimezone(_tz())


def local_date_str() -> str:
    return local_now().strftime("%Y-%m-%d")


def local_time_str() -> str:
    return local_now().strftime("%H:%M")


def fmt_ts(ts: float | int | None, pattern: str = "%Y-%m-%d") -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), tz=_tz()).strftime(pattern)
    except (ValueError, OSError, OverflowError):
        return "—"


def human_delta(seconds: float | int) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "0m"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def remaining_text(expires_at: float | None) -> str:
    if expires_at is None:
        return "♾ Lifetime"
    left = expires_at - time.time()
    if left <= 0:
        return "⌛ Expired"
    return f"{human_delta(left)} left"


# ----------------------------------------------------------------- text/data
def esc(value: object) -> str:
    """Escape user supplied text before it goes into an HTML formatted message."""
    return html.escape(str(value if value is not None else ""))


def slugify(name: str, max_len: int = 20) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "_", name or "").strip("_").lower() or "store"
    return base[:max_len]


def new_token(length: int = 10) -> str:
    """URL safe token used in deep links (?start=...) so links never blow the
    64 character payload limit."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def money(amount: float, currency: str = "৳") -> str:
    if float(amount).is_integer():
        return f"{currency}{int(amount)}"
    return f"{currency}{amount:.2f}"


def human_size(size: int | None) -> str:
    if not size:
        return ""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < step or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= step
    return ""


def chunked(seq: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def button_data(button) -> bytes | None:
    """Read the callback payload of an inline button.

    Newer Telethon layers wrap the payload in `button.type.data`, older ones keep
    it on `button.data`, so we accept both.
    """
    data = getattr(button, "data", None)
    if data is None:
        data = getattr(getattr(button, "type", None), "data", None)
    if isinstance(data, str):
        return data.encode()
    return data if isinstance(data, bytes) else None


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def unique_keep_order(items: Iterable[T]) -> list[T]:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out

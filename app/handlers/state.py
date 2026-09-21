"""Short lived conversations state (who is typing what right now)."""
from __future__ import annotations

# admin_id -> {"action": str, "ctx": dict}
pending_input: dict[int, dict] = {}

# user_id -> store_id while waiting for a search keyword
search_pending: dict[int, int] = {}

# admin_id -> [file_id, ...] while in /done batch mode
batch_mode: dict[int, list[int]] = {}

# admin_id -> {"store_id": int, "page": int, "selected": set[int]} multi-link tool
link_gen: dict[int, dict] = {}

# admin_id -> [ (chat_id, title), ... ] cached dialog list for the scan picker
dialog_cache: dict[int, list[tuple[int, str]]] = {}


# admin_id -> context shared between the buttons of a multi-step flow
flow_ctx: dict[int, dict] = {}


def ask(admin_id: int, action: str, **ctx) -> None:
    pending_input[admin_id] = {"action": action, "ctx": ctx}


def flow(admin_id: int, **ctx) -> None:
    """Remember where a button flow is (merged, so later steps can add fields)."""
    flow_ctx.setdefault(admin_id, {}).update(ctx)


def ctx(admin_id: int) -> dict:
    return flow_ctx.get(admin_id, {})


def end_flow(admin_id: int) -> None:
    flow_ctx.pop(admin_id, None)


def take(admin_id: int) -> dict | None:
    return pending_input.pop(admin_id, None)


def peek(admin_id: int) -> dict | None:
    return pending_input.get(admin_id)


def clear(admin_id: int) -> None:
    pending_input.pop(admin_id, None)
    search_pending.pop(admin_id, None)
    flow_ctx.pop(admin_id, None)
    batch_mode.pop(admin_id, None)
    link_gen.pop(admin_id, None)

"""Plans, orders, coupons, trials and revenue helpers (business layer)."""
from __future__ import annotations

import time

from app import config as cfg
from app.logger import log
from app.services import access
from app.storage import db
from app.utils import money


def payment_methods() -> list[tuple[str, str]]:
    """(label, value) pairs configured by the admin, for the payment screen."""
    methods = []
    if cfg.PAY_BKASH:
        methods.append(("bKash", cfg.PAY_BKASH))
    if cfg.PAY_NAGAD:
        methods.append(("Nagad", cfg.PAY_NAGAD))
    if cfg.PAY_ROCKET:
        methods.append(("Rocket", cfg.PAY_ROCKET))
    if cfg.PAY_UPI:
        methods.append(("UPI", cfg.PAY_UPI))
    if cfg.PAY_CRYPTO:
        methods.append(("Crypto", cfg.PAY_CRYPTO))
    return methods


def methods_text() -> str:
    methods = payment_methods()
    if not methods:
        return ""
    return "\n".join(f"• <b>{name}</b>: <code>{value}</code>" for name, value in methods)


def price_text(amount: float) -> str:
    return money(amount, cfg.CURRENCY)


def plan_label(plan: dict) -> str:
    return f"{plan['name']} — {price_text(plan['price'])}"


def default_plans(store_id: int) -> list[dict]:
    """Sensible starter plans so a new store can sell immediately."""
    if db.plans(store_id, only_active=True):
        return db.plans(store_id, only_active=True)
    return []


def store_sales(store_id: int) -> dict:
    orders = [o for o in db.orders(limit=500) if o["store_id"] == store_id]
    approved = [o for o in orders if o["status"] == "approved"]
    pending = [o for o in orders if o["status"] == "pending"]
    return {
        "total_orders": len(orders),
        "approved": len(approved),
        "pending": len(pending),
        "revenue": sum(o["amount"] or 0 for o in approved),
    }


def create_order_from_plan(user_id: int, store_id: int, plan: dict, method: str,
                           discount_percent: int = 0, coupon: str | None = None) -> dict:
    """Create a pending payment request. Free plans are auto-approved."""
    amount = float(plan["price"] or 0)
    if discount_percent:
        amount = round(amount * (100 - discount_percent) / 100, 2)

    note = f"coupon:{coupon}" if coupon else ""
    order_id = db.create_order(
        user_id=user_id, store_id=store_id, plan=plan, method=method,
        amount=amount, currency=cfg.CURRENCY, note=note,
    )

    if amount <= 0 and cfg.AUTO_APPROVE_ZERO:
        approve_order(order_id, admin_id=0, note="auto (free plan)")
        log.info("Order %s auto-approved (free plan) for user %s", order_id, user_id)

    return db.order(order_id)


def approve_order(order_id: int, admin_id: int, note: str = "") -> dict | None:
    """Grant the plan's access and mark the order approved."""
    order = db.order(order_id)
    if order is None or order["status"] == "approved":
        return order

    store = db.store(order["store_id"])
    days = int(order["days"] or 0)
    if store and days:
        access.grant_days(store["id"], order["user_id"], days, source="payment")
    elif store:
        access.grant_access(store["id"], order["user_id"], None, source="payment")

    db.decide_order(order_id, "approved", admin_id, note)
    if order["note"] and order["note"].startswith("coupon:"):
        db.use_coupon(order["note"].split(":", 1)[1])
    log.info("Order %s approved by %s", order_id, admin_id)
    return db.order(order_id)


def reject_order(order_id: int, admin_id: int, note: str = "") -> dict | None:
    db.decide_order(order_id, "rejected", admin_id, note)
    log.info("Order %s rejected by %s", order_id, admin_id)
    return db.order(order_id)


def grant_trial(user_id: int, store: dict) -> bool:
    """One free trial per user per store (length from TRIAL_HOURS)."""
    if cfg.TRIAL_HOURS <= 0:
        return False
    if db.trial_used(user_id, store["id"]):
        return False
    access.grant_access(store["id"], user_id,
                        time.time() + cfg.TRIAL_HOURS * 3600, source="trial")
    db.mark_trial(user_id, store["id"])
    return True


def trial_available(user_id: int, store: dict) -> bool:
    return cfg.TRIAL_HOURS > 0 and not db.trial_used(user_id, store["id"])


def apply_coupon(code: str, user_id: int, store: dict, plan: dict | None = None) -> tuple[bool, str]:
    """Validate a coupon. Percent coupons return the discounted price, day
    coupons unlock access straight away."""
    ok, reason = db.coupon_valid(code, store["id"])
    if not ok:
        return False, reason

    coupon = db.coupon(code)
    if coupon["days"]:
        access.grant_days(store["id"], user_id, coupon["days"], source="coupon")
        db.use_coupon(coupon["code"])
        return True, f"days:{coupon['days']}"

    if plan is None:
        return False, "Coupon needs a plan selected."
    new_price = round(float(plan["price"]) * (100 - coupon["percent"]) / 100, 2)
    return True, f"percent:{coupon['percent']}:{new_price}"


def upsell_text(store: dict, user_id: int) -> str:
    """Short pitch appended to locked-store screens."""
    plans = db.plans(store["id"], only_active=True)
    if not plans:
        return ""
    cheapest = min(plans, key=lambda p: p["price"])
    lines = ["", "💎 <b>Available plans</b>"]
    for plan in plans[:4]:
        lines.append(f"• {plan['name']} — {price_text(plan['price'])}")
    lines.append(f"<i>Starting from {price_text(cheapest['price'])}</i>")
    return "\n".join(lines)

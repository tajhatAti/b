"""SQLite storage layer.

Why SQLite: it needs no server (works on any web-hosting disk quota), gives us
atomic writes (no more corrupted JSON after a crash), indexed lookups even with
hundreds of thousands of files, and lets us increment a view counter without
rewriting the whole database file on every click.

An existing `bot_db.json` is imported automatically on the first run.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from app import config as cfg
from app.logger import log
from app.utils import slugify, unique_keep_order

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS users (
    user_id   INTEGER PRIMARY KEY,
    name      TEXT,
    username  TEXT,
    joined_at REAL,
    last_seen REAL,
    lang      TEXT DEFAULT 'bn',
    sort_pref TEXT DEFAULT 'default'
);
CREATE TABLE IF NOT EXISTS admin_state (
    admin_id        INTEGER PRIMARY KEY,
    active_store_id INTEGER
);
CREATE TABLE IF NOT EXISTS stores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    slug        TEXT    UNIQUE,
    created_at  REAL,
    is_premium  INTEGER DEFAULT 1,
    cover       TEXT,
    description TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'File',
    chat_id    INTEGER,
    msg_id     INTEGER,
    code       TEXT    UNIQUE,
    size       INTEGER,
    duration   INTEGER,
    created_at REAL,
    views      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS links (
    token      TEXT PRIMARY KEY,
    admin_id   INTEGER,
    kind       TEXT,
    created_at REAL,
    expires_at REAL
);
CREATE TABLE IF NOT EXISTS link_files (
    token   TEXT,
    file_id INTEGER,
    PRIMARY KEY (token, file_id)
);
CREATE TABLE IF NOT EXISTS grants (
    store_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    expires_at REAL,
    granted_at REAL,
    source     TEXT,
    PRIMARY KEY (store_id, user_id)
);
CREATE TABLE IF NOT EXISTS referrals (
    user_id     INTEGER PRIMARY KEY,
    referrer_id INTEGER,
    rewarded    INTEGER DEFAULT 0,
    created_at  REAL
);
CREATE TABLE IF NOT EXISTS referral_cfg (
    admin_id INTEGER PRIMARY KEY,
    store_id INTEGER,
    days     INTEGER
);
CREATE TABLE IF NOT EXISTS drip (
    store_id       INTEGER PRIMARY KEY,
    admin_id       INTEGER,
    count          INTEGER DEFAULT 3,
    send_time      TEXT,
    last_sent_date TEXT,
    enabled        INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drip_subs (
    store_id INTEGER,
    user_id  INTEGER,
    PRIMARY KEY (store_id, user_id)
);
CREATE TABLE IF NOT EXISTS drip_sent (
    store_id INTEGER,
    file_id  INTEGER,
    PRIMARY KEY (store_id, file_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    admin_id    INTEGER PRIMARY KEY,
    session_str TEXT,
    owner_id    INTEGER,
    owner_name  TEXT,
    added_at    REAL
);
CREATE TABLE IF NOT EXISTS bot_chats (
    chat_id INTEGER PRIMARY KEY,
    title   TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_store   ON files(store_id);
CREATE INDEX IF NOT EXISTS idx_files_code    ON files(code);
CREATE INDEX IF NOT EXISTS idx_files_created ON files(created_at);
CREATE INDEX IF NOT EXISTS idx_stores_admin  ON stores(admin_id);
CREATE INDEX IF NOT EXISTS idx_grants_user   ON grants(user_id);
CREATE INDEX IF NOT EXISTS idx_links_exp     ON links(expires_at);
CREATE INDEX IF NOT EXISTS idx_refs_referrer ON referrals(referrer_id);

-- ===================== v2.1: business, support, engagement =====================
CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    days       INTEGER NOT NULL DEFAULT 30,
    price      REAL    NOT NULL DEFAULT 0,
    active     INTEGER DEFAULT 1,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    store_id   INTEGER,
    plan_id    INTEGER,
    plan_name  TEXT,
    days       INTEGER,
    amount     REAL,
    currency   TEXT,
    method     TEXT,
    status     TEXT DEFAULT 'pending',
    txn_ref    TEXT,
    note       TEXT,
    proof      TEXT,
    created_at REAL,
    decided_at REAL,
    decided_by INTEGER,
    admin_note TEXT
);
CREATE TABLE IF NOT EXISTS coupons (
    code       TEXT PRIMARY KEY,
    store_id   INTEGER,
    percent    INTEGER DEFAULT 0,
    days       INTEGER DEFAULT 0,
    max_uses   INTEGER DEFAULT 0,
    used       INTEGER DEFAULT 0,
    expires_at REAL,
    active     INTEGER DEFAULT 1,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS trials (
    user_id  INTEGER,
    store_id INTEGER,
    used_at  REAL,
    PRIMARY KEY (user_id, store_id)
);
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    username    TEXT,
    message     TEXT,
    status      TEXT DEFAULT 'open',
    created_at  REAL,
    answered_at REAL,
    answered_by INTEGER,
    reply       TEXT
);
CREATE TABLE IF NOT EXISTS requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    store_id   INTEGER,
    keyword    TEXT,
    status     TEXT DEFAULT 'open',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS favorites (
    user_id    INTEGER,
    file_id    INTEGER,
    created_at REAL,
    PRIMARY KEY (user_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_plans_store  ON plans(store_id);
CREATE INDEX IF NOT EXISTS idx_orders_user  ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(status);
CREATE INDEX IF NOT EXISTS idx_tickets_st   ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_fav_user     ON favorites(user_id);

-- ===================== v2.2: bans, auto-sync, welcome =====================
CREATE TABLE IF NOT EXISTS banned (
    user_id   INTEGER PRIMARY KEY,
    reason    TEXT,
    banned_at REAL,
    banned_by INTEGER
);
CREATE TABLE IF NOT EXISTS auto_sync (
    store_id    INTEGER PRIMARY KEY,
    chat_id     INTEGER,
    title       TEXT,
    last_scan   REAL,
    interval_h  INTEGER DEFAULT 24,
    enabled     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS welcome_cfg (
    admin_id INTEGER PRIMARY KEY,
    text     TEXT
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(SCHEMA)
        self._upgrade()
        self.conn.commit()

    def _upgrade(self) -> None:
        """Add columns that were introduced after the first release."""
        for table, column, ddl in (
            ("users", "lang", "ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'bn'"),
            ("users", "sort_pref", "ALTER TABLE users ADD COLUMN sort_pref TEXT DEFAULT 'default'"),
            ("orders", "proof", "ALTER TABLE orders ADD COLUMN proof TEXT"),
        ):
            columns = {row["name"] for row in
                       self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in columns:
                try:
                    self.conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass

    # ------------------------------------------------------------------ basics
    def _q(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def _run(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None

    # -------------------------------------------------------------------- meta
    def get_meta(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._run(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    # ------------------------------------------------------------------- users
    def touch_user(self, user_id: int, name: str | None = None,
                   username: str | None = None) -> bool:
        """Register/refresh a user. Returns True when the user is brand new."""
        now = time.time()
        row = self._one("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if row is None:
            self._run(
                "INSERT INTO users(user_id, name, username, joined_at, last_seen) "
                "VALUES(?, ?, ?, ?, ?)",
                (user_id, name or "Unknown", username, now, now),
            )
            return True
        self._run(
            "UPDATE users SET last_seen = ?, "
            "name = COALESCE(?, name), username = COALESCE(?, username) "
            "WHERE user_id = ?",
            (now, name, username, user_id),
        )
        return False

    def user(self, user_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM users WHERE user_id = ?", (user_id,)))

    def all_user_ids(self) -> list[int]:
        return [r["user_id"] for r in self._q("SELECT user_id FROM users ORDER BY user_id")]

    def user_count(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM users")["c"]

    # ------------------------------------------------------------ admin state
    def active_store_id(self, admin_id: int) -> int | None:
        row = self._one("SELECT active_store_id FROM admin_state WHERE admin_id = ?", (admin_id,))
        return row["active_store_id"] if row else None

    def set_active_store(self, admin_id: int, store_id: int | None) -> None:
        self._run(
            "INSERT INTO admin_state(admin_id, active_store_id) VALUES(?, ?) "
            "ON CONFLICT(admin_id) DO UPDATE SET active_store_id = excluded.active_store_id",
            (admin_id, store_id),
        )

    # ------------------------------------------------------------------ stores
    def _free_slug(self, name: str) -> str:
        base = slugify(name)
        slug, i = base, 1
        while self._one("SELECT 1 FROM stores WHERE slug = ?", (slug,)):
            i += 1
            slug = f"{base[:17]}_{i}"
        return slug

    def create_store(self, admin_id: int, name: str) -> dict:
        row = self._one(
            "SELECT * FROM stores WHERE admin_id = ? AND name = ?", (admin_id, name)
        )
        if row:
            return dict(row)
        self._run(
            "INSERT INTO stores(admin_id, name, slug, created_at, is_premium) "
            "VALUES(?, ?, ?, ?, 1)",
            (admin_id, name, self._free_slug(name), time.time()),
        )
        return dict(self._one(
            "SELECT * FROM stores WHERE admin_id = ? AND name = ?", (admin_id, name)
        ))

    def store(self, store_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM stores WHERE id = ?", (store_id,)))

    def store_by_name(self, admin_id: int, name: str) -> dict | None:
        return self._dict(
            self._one("SELECT * FROM stores WHERE admin_id = ? AND name = ?", (admin_id, name))
        )

    def store_by_slug(self, slug: str) -> dict | None:
        return self._dict(self._one("SELECT * FROM stores WHERE slug = ?", (slug,)))

    def stores_admin(self, admin_id: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM stores WHERE admin_id = ? ORDER BY id", (admin_id,)
        )]

    def all_stores(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM stores ORDER BY id")]

    def update_store(self, store_id: int, **fields: Any) -> None:
        allowed = {"name", "slug", "is_premium", "cover", "description"}
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        params.append(store_id)
        self._run(f"UPDATE stores SET {', '.join(sets)} WHERE id = ?", params)

    def delete_store(self, store_id: int) -> None:
        """Delete a store *and everything that pointed at it* (no orphan data)."""
        file_ids = [r["id"] for r in self._q("SELECT id FROM files WHERE store_id = ?", (store_id,))]
        tokens = [r["token"] for r in self._q(
            "SELECT DISTINCT token FROM link_files WHERE file_id IN "
            "(SELECT id FROM files WHERE store_id = ?)", (store_id,)
        )] if file_ids else []
        self._run("DELETE FROM favorites WHERE file_id IN "
                  "(SELECT id FROM files WHERE store_id = ?)", (store_id,))
        self._run("DELETE FROM requests WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM plans WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM files WHERE store_id = ?", (store_id,))
        if file_ids:
            marks = ",".join("?" for _ in file_ids)
            self._run(f"DELETE FROM link_files WHERE file_id IN ({marks})", file_ids)
            self._run(f"DELETE FROM drip_sent WHERE file_id IN ({marks})", file_ids)
        for token in tokens:
            if not self._one("SELECT 1 FROM link_files WHERE token = ?", (token,)):
                self._run("DELETE FROM links WHERE token = ?", (token,))
        self._run("DELETE FROM grants WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM drip WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM drip_subs WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM drip_sent WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM referral_cfg WHERE store_id = ?", (store_id,))
        self._run("DELETE FROM admin_state WHERE active_store_id = ?", (store_id,))
        self._run("DELETE FROM stores WHERE id = ?", (store_id,))

    def store_stats(self, store_id: int) -> dict:
        row = self._one(
            "SELECT COUNT(*) AS files, COALESCE(SUM(views), 0) AS views "
            "FROM files WHERE store_id = ?", (store_id,)
        )
        grants = self._one("SELECT COUNT(*) AS c FROM grants WHERE store_id = ?", (store_id,))["c"]
        subs = self._one("SELECT COUNT(*) AS c FROM drip_subs WHERE store_id = ?", (store_id,))["c"]
        return {"files": row["files"], "views": row["views"], "grants": grants, "subs": subs}

    # ------------------------------------------------------------------- files
    def add_file(self, store_id: int, name: str, kind: str, chat_id: int,
                 msg_id: int, code: str | None = None, size: int | None = None,
                 duration: int | None = None) -> int | None:
        """Add media to a store. Returns the new id, or None for duplicates."""
        if code and self._one("SELECT 1 FROM files WHERE code = ?", (code,)):
            return None
        if self._one(
            "SELECT 1 FROM files WHERE store_id = ? AND chat_id = ? AND msg_id = ?",
            (store_id, chat_id, msg_id),
        ):
            return None
        cur = self._run(
            "INSERT INTO files(store_id, name, kind, chat_id, msg_id, code, size, duration, created_at, views) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (store_id, name, kind, chat_id, msg_id, code, size, duration, time.time()),
        )
        return cur.lastrowid

    def file(self, file_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM files WHERE id = ?", (file_id,)))

    def file_by_code(self, code: str) -> dict | None:
        return self._dict(self._one("SELECT * FROM files WHERE code = ?", (code,)))

    def files_of(self, store_id: int, newest_first: bool = False) -> list[dict]:
        order = "created_at DESC, id DESC" if newest_first else "id"
        return [dict(r) for r in self._q(
            f"SELECT * FROM files WHERE store_id = ? ORDER BY {order}", (store_id,)
        )]

    def search_files(self, store_id: int, keyword: str, limit: int = 20) -> list[dict]:
        like = f"%{keyword.lower()}%"
        return [dict(r) for r in self._q(
            "SELECT * FROM files WHERE store_id = ? AND LOWER(name) LIKE ? LIMIT ?",
            (store_id, like, limit),
        )]

    def latest_files(self, store_id: int, limit: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM files WHERE store_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (store_id, limit),
        )]

    def rename_file(self, file_id: int, name: str) -> None:
        self._run("UPDATE files SET name = ? WHERE id = ?", (name, file_id))

    def delete_file(self, file_id: int) -> int | None:
        row = self._one("SELECT store_id FROM files WHERE id = ?", (file_id,))
        if not row:
            return None
        store_id = row["store_id"]
        self._run("DELETE FROM files WHERE id = ?", (file_id,))
        self._run("DELETE FROM favorites WHERE file_id = ?", (file_id,))
        self._run("DELETE FROM link_files WHERE file_id = ?", (file_id,))
        self._run("DELETE FROM drip_sent WHERE file_id = ?", (file_id,))
        return store_id

    def move_file(self, file_id: int, store_id: int) -> bool:
        if not self._one("SELECT 1 FROM stores WHERE id = ?", (store_id,)):
            return False
        self._run("UPDATE files SET store_id = ? WHERE id = ?", (store_id, file_id))
        return True

    def bump_views(self, file_id: int) -> None:
        self._run("UPDATE files SET views = views + 1 WHERE id = ?", (file_id,))

    def total_files(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM files")["c"]

    def total_views(self) -> int:
        return self._one("SELECT COALESCE(SUM(views), 0) AS c FROM files")["c"]

    def top_files(self, limit: int = 5) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT f.*, s.name AS store_name FROM files f "
            "LEFT JOIN stores s ON s.id = f.store_id "
            "ORDER BY f.views DESC LIMIT ?", (limit,)
        )]

    def store_id_of_file(self, file_id: int) -> int | None:
        row = self._one("SELECT store_id FROM files WHERE id = ?", (file_id,))
        return row["store_id"] if row else None

    # ------------------------------------------------------------------- links
    def create_link(self, admin_id: int, file_ids: Iterable[int],
                    expires_at: float | None, kind: str = "multi") -> str:
        from app.utils import new_token
        token = new_token(10)
        self._run(
            "INSERT INTO links(token, admin_id, kind, created_at, expires_at) VALUES(?, ?, ?, ?, ?)",
            (token, admin_id, kind, time.time(), expires_at),
        )
        for fid in unique_keep_order(file_ids):
            self._run("INSERT OR IGNORE INTO link_files(token, file_id) VALUES(?, ?)", (token, fid))
        return token

    def link(self, token: str) -> dict | None:
        return self._dict(self._one("SELECT * FROM links WHERE token = ?", (token,)))

    def link_file_ids(self, token: str) -> list[int]:
        return [r["file_id"] for r in self._q(
            "SELECT lf.file_id FROM link_files lf JOIN files f ON f.id = lf.file_id "
            "WHERE lf.token = ? ORDER BY f.id", (token,)
        )]

    def delete_link(self, token: str) -> None:
        self._run("DELETE FROM link_files WHERE token = ?", (token,))
        self._run("DELETE FROM links WHERE token = ?", (token,))

    def purge_expired_links(self) -> int:
        now = time.time()
        rows = self._q("SELECT token FROM links WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
        for row in rows:
            self.delete_link(row["token"])
        return len(rows)

    def link_count(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM links")["c"]

    # ------------------------------------------------------------------ grants
    def grant(self, store_id: int, user_id: int, expires_at: float | None,
              source: str = "admin") -> None:
        self._run(
            "INSERT INTO grants(store_id, user_id, expires_at, granted_at, source) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(store_id, user_id) DO UPDATE SET "
            "expires_at = excluded.expires_at, granted_at = excluded.granted_at, "
            "source = excluded.source",
            (store_id, user_id, expires_at, time.time(), source),
        )

    def extend_grant(self, store_id: int, user_id: int, days: int, source: str = "referral") -> float:
        """Add days on top of a still valid grant (instead of overwriting it)."""
        row = self._one("SELECT expires_at FROM grants WHERE store_id = ? AND user_id = ?",
                        (store_id, user_id))
        now = time.time()
        if row is None:
            base = now
        elif row["expires_at"] is None:
            return now  # already lifetime, nothing to extend
        else:
            base = max(row["expires_at"], now)
        new_expiry = base + days * 86400
        self.grant(store_id, user_id, new_expiry, source)
        return new_expiry

    def revoke(self, store_id: int, user_id: int) -> None:
        self._run("DELETE FROM grants WHERE store_id = ? AND user_id = ?", (store_id, user_id))

    def grant_row(self, store_id: int, user_id: int) -> dict | None:
        return self._dict(self._one(
            "SELECT * FROM grants WHERE store_id = ? AND user_id = ?", (store_id, user_id)
        ))

    def store_grants(self, store_id: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT g.*, u.name AS user_name FROM grants g "
            "LEFT JOIN users u ON u.user_id = g.user_id WHERE g.store_id = ? "
            "ORDER BY g.granted_at DESC", (store_id,)
        )]

    def user_grants(self, user_id: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT g.*, s.name AS store_name, s.is_premium FROM grants g "
            "JOIN stores s ON s.id = g.store_id WHERE g.user_id = ?", (user_id,)
        )]

    def grant_count(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM grants")["c"]

    def paid_users(self) -> set[int]:
        return {r["user_id"] for r in self._q("SELECT DISTINCT user_id FROM grants")}

    def expired_grants(self, within_seconds: float = 3 * 86400) -> list[dict]:
        """Grants that expire within `within_seconds` (used for renewal reminders)."""
        now = time.time()
        return [dict(r) for r in self._q(
            "SELECT g.*, s.name AS store_name FROM grants g JOIN stores s ON s.id = g.store_id "
            "WHERE g.expires_at IS NOT NULL AND g.expires_at > ? AND g.expires_at < ?",
            (now, now + within_seconds),
        )]

    # --------------------------------------------------------------- referrals
    def set_referrer(self, user_id: int, referrer_id: int) -> bool:
        if user_id == referrer_id:
            return False
        if self._one("SELECT 1 FROM referrals WHERE user_id = ?", (user_id,)):
            return False
        self._run(
            "INSERT INTO referrals(user_id, referrer_id, rewarded, created_at) VALUES(?, ?, 0, ?)",
            (user_id, referrer_id, time.time()),
        )
        return True

    def referrer_of(self, user_id: int) -> int | None:
        row = self._one("SELECT referrer_id FROM referrals WHERE user_id = ?", (user_id,))
        return row["referrer_id"] if row else None

    def is_rewarded(self, user_id: int) -> bool:
        row = self._one("SELECT rewarded FROM referrals WHERE user_id = ?", (user_id,))
        return bool(row and row["rewarded"])

    def mark_rewarded(self, user_id: int) -> None:
        self._run("UPDATE referrals SET rewarded = 1 WHERE user_id = ?", (user_id,))

    def invite_count(self, referrer_id: int) -> int:
        return self._one(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?", (referrer_id,)
        )["c"]

    def top_referrers(self, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT referrer_id, COUNT(*) AS invites FROM referrals GROUP BY referrer_id ORDER BY invites DESC LIMIT ?",
            (limit,)
        )]

    def set_referral_cfg(self, admin_id: int, store_id: int, days: int) -> None:
        self._run(
            "INSERT INTO referral_cfg(admin_id, store_id, days) VALUES(?, ?, ?) "
            "ON CONFLICT(admin_id) DO UPDATE SET store_id = excluded.store_id, days = excluded.days",
            (admin_id, store_id, days),
        )

    def referral_cfg(self, admin_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM referral_cfg WHERE admin_id = ?", (admin_id,)))

    def all_referral_cfgs(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM referral_cfg")]

    # -------------------------------------------------------------------- drip
    def upsert_drip(self, store_id: int, admin_id: int, count: int, send_time: str) -> None:
        self._run(
            "INSERT INTO drip(store_id, admin_id, count, send_time, last_sent_date, enabled) "
            "VALUES(?, ?, ?, ?, '', 1) "
            "ON CONFLICT(store_id) DO UPDATE SET count = excluded.count, "
            "send_time = excluded.send_time, enabled = 1",
            (store_id, admin_id, count, send_time),
        )

    def set_drip_enabled(self, store_id: int, enabled: bool) -> None:
        self._run("UPDATE drip SET enabled = ? WHERE store_id = ?", (1 if enabled else 0, store_id))

    def set_drip_last_sent(self, store_id: int, date_str: str) -> None:
        self._run("UPDATE drip SET last_sent_date = ? WHERE store_id = ?", (date_str, store_id))

    def drip(self, store_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM drip WHERE store_id = ?", (store_id,)))

    def all_drips(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM drip WHERE enabled = 1")]

    def toggle_sub(self, store_id: int, user_id: int) -> bool:
        if self.is_sub(store_id, user_id):
            self._run("DELETE FROM drip_subs WHERE store_id = ? AND user_id = ?", (store_id, user_id))
            return False
        self._run("INSERT OR IGNORE INTO drip_subs(store_id, user_id) VALUES(?, ?)", (store_id, user_id))
        return True

    def is_sub(self, store_id: int, user_id: int) -> bool:
        return bool(self._one(
            "SELECT 1 FROM drip_subs WHERE store_id = ? AND user_id = ?", (store_id, user_id)
        ))

    def subscribers(self, store_id: int) -> list[int]:
        return [r["user_id"] for r in self._q(
            "SELECT user_id FROM drip_subs WHERE store_id = ?", (store_id,)
        )]

    def drop_sub(self, store_id: int, user_id: int) -> None:
        self._run("DELETE FROM drip_subs WHERE store_id = ? AND user_id = ?", (store_id, user_id))

    def sent_file_ids(self, store_id: int) -> set[int]:
        return {r["file_id"] for r in self._q(
            "SELECT file_id FROM drip_sent WHERE store_id = ?", (store_id,)
        )}

    def mark_sent(self, store_id: int, file_ids: Iterable[int]) -> None:
        for fid in file_ids:
            self._run("INSERT OR IGNORE INTO drip_sent(store_id, file_id) VALUES(?, ?)",
                      (store_id, fid))

    # ---------------------------------------------------------------- sessions
    def save_session(self, admin_id: int, session_str: str, owner_id: int | None = None,
                     owner_name: str | None = None) -> None:
        self._run(
            "INSERT INTO sessions(admin_id, session_str, owner_id, owner_name, added_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(admin_id) DO UPDATE SET session_str = excluded.session_str, "
            "owner_id = excluded.owner_id, owner_name = excluded.owner_name",
            (admin_id, session_str, owner_id, owner_name, time.time()),
        )

    def session(self, admin_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM sessions WHERE admin_id = ?", (admin_id,)))

    def all_sessions(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM sessions ORDER BY admin_id")]

    def delete_session(self, admin_id: int) -> None:
        self._run("DELETE FROM sessions WHERE admin_id = ?", (admin_id,))

    # --------------------------------------------------------------- bot chats
    def add_bot_chat(self, chat_id: int, title: str = "") -> None:
        self._run("INSERT OR IGNORE INTO bot_chats(chat_id, title) VALUES(?, ?)", (chat_id, title))

    def bot_chats(self) -> set[int]:
        return {r["chat_id"] for r in self._q("SELECT chat_id FROM bot_chats")}

    # ------------------------------------------------------------------- stats
    def stats(self) -> dict:
        return {
            "users": self.user_count(),
            "files": self.total_files(),
            "views": self.total_views(),
            "stores": self._one("SELECT COUNT(*) AS c FROM stores")["c"],
            "grants": self.grant_count(),
            "links": self.link_count(),
            "paid_users": len(self.paid_users()),
            "referrals": self._one("SELECT COUNT(*) AS c FROM referrals")["c"],
            "orders_pending": self.pending_order_count(),
            "tickets_open": self.open_ticket_count(),
            "requests_open": self.open_request_count(),
            "revenue": self.revenue()["total"],
        }

    # ------------------------------------------------------- export / backup
    def export_dict(self) -> dict:
        """Human readable snapshot (same shape as the old bot_db.json + extras)."""
        stores_out: dict[str, dict] = {}
        for store in self.all_stores():
            files_out = {}
            for f in self.files_of(store["id"]):
                files_out[str(f["id"])] = {
                    "id": f["id"], "code": f["code"], "name": f["name"], "type": f["kind"],
                    "msg_id": f["msg_id"], "chat_id": f["chat_id"], "views": f["views"],
                    "created_at": f["created_at"],
                }
            stores_out[str(store["id"])] = {
                "name": store["name"], "slug": store["slug"], "admin_id": store["admin_id"],
                "is_premium": store["is_premium"], "files": files_out,
            }
        return {
            "version": 2,
            "exported_at": time.time(),
            "stores": stores_out,
            "users": {str(u["user_id"]): {"name": u["name"], "joined": u["joined_at"]}
                      for u in self._q("SELECT * FROM users")},
            "grants": [
                {"store_id": g["store_id"], "user_id": g["user_id"],
                 "expires_at": g["expires_at"], "source": g["source"]}
                for g in self._q("SELECT * FROM grants")
            ],
            "referrals": {str(r["user_id"]): r["referrer_id"] for r in self._q("SELECT * FROM referrals")},
            "drip": {str(d["store_id"]): dict(d) for d in self._q("SELECT * FROM drip")},
            "stats": self.stats(),
        }

    def backup_json(self, directory: str, keep: int = 7) -> str:
        directory_path = Path(directory)
        directory_path.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = directory_path / f"bot_data-{stamp}.json"
        target.write_text(json.dumps(self.export_dict(), ensure_ascii=False, indent=1),
                          encoding="utf-8")
        # copy the raw sqlite file too — it is the real backup
        try:
            shutil.copy2(self.path, directory_path / f"bot_data-{stamp}.sqlite3")
        except Exception as exc:  # pragma: no cover
            log.warning("sqlite backup copy failed: %s", exc)
        backups = sorted(directory_path.glob("bot_data-*"))
        for old in backups[: max(0, len(backups) - keep * 2)]:
            try:
                old.unlink()
            except OSError:
                pass
        return str(target)


    # ------------------------------------------------------------------ plans
    def add_plan(self, store_id: int, name: str, days: int, price: float) -> int:
        cur = self._run(
            "INSERT INTO plans(store_id, name, days, price, active, created_at) VALUES(?, ?, ?, ?, 1, ?)",
            (store_id, name, days, price, time.time()),
        )
        return cur.lastrowid

    def plans(self, store_id: int, only_active: bool = False) -> list[dict]:
        sql = "SELECT * FROM plans WHERE store_id = ?"
        if only_active:
            sql += " AND active = 1"
        sql += " ORDER BY price, days"
        return [dict(r) for r in self._q(sql, (store_id,))]

    def plan(self, plan_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM plans WHERE id = ?", (plan_id,)))

    def delete_plan(self, plan_id: int) -> None:
        self._run("DELETE FROM plans WHERE id = ?", (plan_id,))

    def toggle_plan(self, plan_id: int) -> None:
        self._run("UPDATE plans SET active = 1 - active WHERE id = ?", (plan_id,))

    # ----------------------------------------------------------------- orders
    def create_order(self, user_id: int, store_id: int, plan: dict, method: str,
                     amount: float, currency: str, txn_ref: str = "",
                     note: str = "", proof: str = "") -> int:
        cur = self._run(
            "INSERT INTO orders(user_id, store_id, plan_id, plan_name, days, amount, currency, "
            "method, status, txn_ref, note, proof, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (user_id, store_id, plan["id"], plan["name"], plan["days"], amount, currency,
             method, txn_ref, note, proof, time.time()),
        )
        return cur.lastrowid

    def order(self, order_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM orders WHERE id = ?", (order_id,)))

    def orders(self, status: str | None = None, user_id: int | None = None,
               limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM orders WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._q(sql, params)]

    def update_order_proof(self, order_id: int, proof: str) -> None:
        self._run("UPDATE orders SET proof = ? WHERE id = ?", (proof, order_id))

    def decide_order(self, order_id: int, status: str, admin_id: int,
                     admin_note: str = "") -> None:
        self._run(
            "UPDATE orders SET status = ?, decided_at = ?, decided_by = ?, admin_note = ? "
            "WHERE id = ?",
            (status, time.time(), admin_id, admin_note, order_id),
        )

    def pending_order_count(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM orders WHERE status = 'pending'")["c"]

    def revenue(self) -> dict:
        row = self._one(
            "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count, "
            "COALESCE(AVG(amount), 0) AS avg FROM orders WHERE status = 'approved'"
        )
        pending = self.pending_order_count()
        return {"total": row["total"], "count": row["count"], "avg": row["avg"], "pending": pending}

    def revenue_by_day(self, days: int = 14) -> list[tuple[str, float]]:
        since = time.time() - days * 86400
        rows = self._q(
            "SELECT date(decided_at, 'unixepoch') AS d, SUM(amount) AS total "
            "FROM orders WHERE status = 'approved' AND decided_at > ? GROUP BY d ORDER BY d",
            (since,),
        )
        return [(r["d"], r["total"]) for r in rows if r["d"]]

    def user_orders(self, user_id: int, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT o.*, s.name AS store_name FROM orders o LEFT JOIN stores s ON s.id = o.store_id "
            "WHERE o.user_id = ? ORDER BY o.created_at DESC LIMIT ?", (user_id, limit)
        )]

    # ---------------------------------------------------------------- coupons
    def add_coupon(self, code: str, store_id: int | None, percent: int = 0,
                   days: int = 0, max_uses: int = 0,
                   expires_at: float | None = None) -> None:
        self._run(
            "INSERT INTO coupons(code, store_id, percent, days, max_uses, used, expires_at, active, created_at) "
            "VALUES(?, ?, ?, ?, ?, 0, ?, 1, ?) ON CONFLICT(code) DO UPDATE SET "
            "store_id = excluded.store_id, percent = excluded.percent, days = excluded.days, "
            "max_uses = excluded.max_uses, expires_at = excluded.expires_at, active = 1",
            (code.upper(), store_id, percent, days, max_uses, expires_at, time.time()),
        )

    def coupon(self, code: str) -> dict | None:
        return self._dict(self._one("SELECT * FROM coupons WHERE code = ?", (code.upper(),)))

    def coupons(self, store_id: int | None = None) -> list[dict]:
        if store_id:
            return [dict(r) for r in self._q(
                "SELECT * FROM coupons WHERE store_id = ? OR store_id IS NULL ORDER BY created_at DESC",
                (store_id,))]
        return [dict(r) for r in self._q("SELECT * FROM coupons ORDER BY created_at DESC")]

    def use_coupon(self, code: str) -> None:
        self._run("UPDATE coupons SET used = used + 1 WHERE code = ?", (code.upper(),))

    def delete_coupon(self, code: str) -> None:
        self._run("DELETE FROM coupons WHERE code = ?", (code.upper(),))

    def coupon_valid(self, code: str, store_id: int) -> tuple[bool, str]:
        row = self.coupon(code)
        if row is None or not row["active"]:
            return False, "Coupon not found."
        if row["store_id"] not in (None, store_id):
            return False, "This coupon is for another store."
        if row["expires_at"] and row["expires_at"] < time.time():
            return False, "This coupon has expired."
        if row["max_uses"] and row["used"] >= row["max_uses"]:
            return False, "This coupon has reached its limit."
        return True, ""

    # ----------------------------------------------------------------- trials
    def trial_used(self, user_id: int, store_id: int) -> bool:
        return bool(self._one("SELECT 1 FROM trials WHERE user_id = ? AND store_id = ?",
                              (user_id, store_id)))

    def mark_trial(self, user_id: int, store_id: int) -> None:
        self._run("INSERT OR REPLACE INTO trials(user_id, store_id, used_at) VALUES(?, ?, ?)",
                  (user_id, store_id, time.time()))

    # ---------------------------------------------------------------- tickets
    def open_ticket(self, user_id: int, username: str, message: str) -> int:
        cur = self._run(
            "INSERT INTO tickets(user_id, username, message, status, created_at) "
            "VALUES(?, ?, ?, 'open', ?)", (user_id, username, message, time.time()))
        return cur.lastrowid

    def ticket(self, ticket_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM tickets WHERE id = ?", (ticket_id,)))

    def tickets(self, status: str | None = "open", limit: int = 20) -> list[dict]:
        if status:
            return [dict(r) for r in self._q(
                "SELECT * FROM tickets WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit))]
        return [dict(r) for r in self._q(
            "SELECT * FROM tickets ORDER BY created_at DESC LIMIT ?", (limit,))]

    def answer_ticket(self, ticket_id: int, admin_id: int, reply: str) -> None:
        self._run(
            "UPDATE tickets SET status = 'answered', answered_at = ?, answered_by = ?, reply = ? "
            "WHERE id = ?", (time.time(), admin_id, reply, ticket_id))

    def close_ticket(self, ticket_id: int) -> None:
        self._run("UPDATE tickets SET status = 'closed' WHERE id = ?", (ticket_id,))

    def open_ticket_count(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM tickets WHERE status = 'open'")["c"]

    def ticket_count_for(self, user_id: int) -> int:
        return self._one("SELECT COUNT(*) AS c FROM tickets WHERE user_id = ?", (user_id,))["c"]

    # --------------------------------------------------------------- requests
    def add_request(self, user_id: int, store_id: int | None, keyword: str) -> int:
        cur = self._run(
            "INSERT INTO requests(user_id, store_id, keyword, status, created_at) "
            "VALUES(?, ?, ?, 'open', ?)", (user_id, store_id, keyword, time.time()))
        return cur.lastrowid

    def requests(self, status: str | None = "open", limit: int = 20) -> list[dict]:
        if status:
            return [dict(r) for r in self._q(
                "SELECT r.*, s.name AS store_name FROM requests r LEFT JOIN stores s ON s.id = r.store_id "
                "WHERE r.status = ? ORDER BY r.created_at DESC LIMIT ?", (status, limit))]
        return [dict(r) for r in self._q(
            "SELECT r.*, s.name AS store_name FROM requests r LEFT JOIN stores s ON s.id = r.store_id "
            "ORDER BY r.created_at DESC LIMIT ?", (limit,))]

    def close_request(self, request_id: int) -> None:
        self._run("UPDATE requests SET status = 'done' WHERE id = ?", (request_id,))

    def open_request_count(self) -> int:
        return self._one("SELECT COUNT(*) AS c FROM requests WHERE status = 'open'")["c"]

    # -------------------------------------------------------------- favorites
    def toggle_favorite(self, user_id: int, file_id: int) -> bool:
        if self._one("SELECT 1 FROM favorites WHERE user_id = ? AND file_id = ?", (user_id, file_id)):
            self._run("DELETE FROM favorites WHERE user_id = ? AND file_id = ?", (user_id, file_id))
            return False
        self._run("INSERT INTO favorites(user_id, file_id, created_at) VALUES(?, ?, ?)",
                  (user_id, file_id, time.time()))
        return True

    def is_favorite(self, user_id: int, file_id: int) -> bool:
        return bool(self._one("SELECT 1 FROM favorites WHERE user_id = ? AND file_id = ?",
                              (user_id, file_id)))

    def favorites(self, user_id: int, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT f.*, s.name AS store_name FROM favorites fv "
            "JOIN files f ON f.id = fv.file_id LEFT JOIN stores s ON s.id = f.store_id "
            "WHERE fv.user_id = ? ORDER BY fv.created_at DESC LIMIT ?", (user_id, limit))]

    # ------------------------------------------------------------- searching
    def search_all_stores(self, keyword: str, limit: int = 20) -> list[dict]:
        """Cross-store search (inline mode, global search)."""
        like = f"%{keyword.lower()}%"
        return [dict(r) for r in self._q(
            "SELECT f.*, s.name AS store_name, s.is_premium, s.admin_id FROM files f "
            "JOIN stores s ON s.id = f.store_id WHERE LOWER(f.name) LIKE ? "
            "ORDER BY f.views DESC LIMIT ?", (like, limit))]

    def popular_files(self, store_id: int, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM files WHERE store_id = ? ORDER BY views DESC, id DESC LIMIT ?",
            (store_id, limit))]

    def newest_files(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT f.*, s.name AS store_name FROM files f JOIN stores s ON s.id = f.store_id "
            "ORDER BY f.created_at DESC LIMIT ?", (limit,))]

    # -------------------------------------------------- user preferences / i18n
    def set_user_lang(self, user_id: int, lang: str) -> None:
        self._run("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))

    def user_lang(self, user_id: int) -> str:
        row = self._one("SELECT lang FROM users WHERE user_id = ?", (user_id,))
        return (row["lang"] if row and row["lang"] else "") or "bn"

    def set_user_sort(self, user_id: int, mode: str) -> None:
        self._run("UPDATE users SET sort_pref = ? WHERE user_id = ?", (mode, user_id))

    def user_sort(self, user_id: int) -> str:
        row = self._one("SELECT sort_pref FROM users WHERE user_id = ?", (user_id,))
        return (row["sort_pref"] if row and row["sort_pref"] else "") or "default"

    # -------------------------------------------------------- store extras
    def set_store_meta(self, store_id: int, cover: str | None = None,
                       description: str | None = None) -> None:
        if cover is not None:
            self.update_store(store_id, cover=cover)
        if description is not None:
            self.update_store(store_id, description=description)

    def reset_store_sales(self, store_id: int) -> None:
        self._run("DELETE FROM orders WHERE store_id = ? AND status != 'approved'", (store_id,))

    # ------------------------------------------------------------------ bans
    def ban_user(self, user_id: int, reason: str = "", banned_by: int = 0) -> None:
        self._run(
            "INSERT INTO banned(user_id, reason, banned_at, banned_by) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET reason = excluded.reason, banned_at = excluded.banned_at, banned_by = excluded.banned_by",
            (user_id, reason, time.time(), banned_by),
        )
        # Also revoke all grants
        self._run("DELETE FROM grants WHERE user_id = ?", (user_id,))

    def unban_user(self, user_id: int) -> None:
        self._run("DELETE FROM banned WHERE user_id = ?", (user_id,))

    def is_banned(self, user_id: int) -> bool:
        return bool(self._one("SELECT 1 FROM banned WHERE user_id = ?", (user_id,)))

    def banned_users(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM banned ORDER BY banned_at DESC")]

    # -------------------------------------------------------------- auto sync
    def set_auto_sync(self, store_id: int, chat_id: int, title: str, interval_h: int = 24, enabled: bool = True) -> None:
        self._run(
            "INSERT INTO auto_sync(store_id, chat_id, title, last_scan, interval_h, enabled) VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(store_id) DO UPDATE SET chat_id = excluded.chat_id, title = excluded.title, interval_h = excluded.interval_h, enabled = excluded.enabled",
            (store_id, chat_id, title, time.time(), interval_h, 1 if enabled else 0),
        )

    def auto_sync_cfg(self, store_id: int) -> dict | None:
        return self._dict(self._one("SELECT * FROM auto_sync WHERE store_id = ?", (store_id,)))

    def all_auto_sync(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM auto_sync WHERE enabled = 1")]

    def disable_auto_sync(self, store_id: int) -> None:
        self._run("UPDATE auto_sync SET enabled = 0 WHERE store_id = ?", (store_id,))

    # -------------------------------------------------------------- welcome
    def set_welcome(self, admin_id: int, text: str) -> None:
        self._run(
            "INSERT INTO welcome_cfg(admin_id, text) VALUES(?, ?) "
            "ON CONFLICT(admin_id) DO UPDATE SET text = excluded.text",
            (admin_id, text),
        )

    def welcome_text(self, admin_id: int) -> str:
        row = self._one("SELECT text FROM welcome_cfg WHERE admin_id = ?", (admin_id,))
        return row["text"] if row else ""

    # ------------------------------------------------------- legacy migration
    def migrate_legacy(self, legacy_path: str) -> dict | None:
        """Import the old JSON database. Safe to call repeatedly."""
        path = Path(legacy_path)
        if not path.exists():
            return None
        if self.get_meta("legacy_imported") == "yes":
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("Legacy import failed to read %s: %s", path, exc)
            return None

        counters = {"users": 0, "stores": 0, "files": 0, "grants": 0, "links": 0, "sessions": 0}
        slug_to_store: dict[str, int] = {}
        name_to_store: dict[tuple[int, str], int] = {}
        old_fid_map: dict[str, int] = {}

        # users
        for raw_uid, info in (data.get("user_details") or {}).items():
            uid = int(raw_uid)
            if not self.user(uid):
                self.touch_user(uid, (info or {}).get("name"), None)
                joined = (info or {}).get("joined")
                if joined:
                    self._run("UPDATE users SET joined_at = ? WHERE user_id = ?", (joined, uid))
                counters["users"] += 1

        # stores + files
        for raw_admin, store_map in (data.get("stores") or {}).items():
            admin_id = int(raw_admin)
            for store_name, files in (store_map or {}).items():
                store = self.create_store(admin_id, store_name)
                name_to_store[(admin_id, store_name)] = store["id"]
                counters["stores"] += 1
                for old_fid, meta in (files or {}).items():
                    meta = meta or {}
                    new_id = self.add_file(
                        store["id"],
                        meta.get("name") or old_fid,
                        meta.get("type") or "File",
                        int(meta.get("chat_id") or 0),
                        int(meta.get("msg_id") or 0),
                        code=old_fid,
                        size=meta.get("size"),
                    )
                    if new_id:
                        old_fid_map[old_fid] = new_id
                        if meta.get("views"):
                            self._run("UPDATE files SET views = ? WHERE id = ?",
                                      (int(meta["views"]), new_id))
                        counters["files"] += 1

        # slugs
        for slug, pair in (data.get("store_slugs") or {}).items():
            try:
                admin_id, store_name = int(pair[0]), pair[1]
            except (TypeError, ValueError, IndexError):
                continue
            store_id = name_to_store.get((admin_id, store_name))
            if store_id and not self._one("SELECT 1 FROM stores WHERE slug = ?", (slug,)):
                self.update_store(store_id, slug=slug)
                slug_to_store[slug] = store_id

        # premium grants
        for raw_admin, store_map in (data.get("premium_access") or {}).items():
            for store_name, users in (store_map or {}).items():
                store_id = name_to_store.get((int(raw_admin), store_name))
                if not store_id:
                    continue
                for raw_uid, exp in (users or {}).items():
                    self.grant(store_id, int(raw_uid), exp, source="legacy")
                    counters["grants"] += 1

        # temp links
        for token, info in (data.get("temp_links") or {}).items():
            info = info or {}
            fids = [old_fid_map[f] for f in (info.get("fids") or []) if f in old_fid_map]
            if not fids:
                continue
            self._run(
                "INSERT OR REPLACE INTO links(token, admin_id, kind, created_at, expires_at) "
                "VALUES(?, ?, 'legacy', ?, ?)",
                (token, int(info.get("admin_uid") or 0), time.time(), info.get("expires_at")),
            )
            for fid in fids:
                self._run("INSERT OR IGNORE INTO link_files(token, file_id) VALUES(?, ?)", (token, fid))
            counters["links"] += 1

        # sessions
        for raw_admin, session_str in (data.get("admin_sessions") or {}).items():
            if session_str:
                self.save_session(int(raw_admin), session_str)
                counters["sessions"] += 1

        # bot admin chats
        for chat_id in (data.get("bot_admin_chats") or []):
            self.add_bot_chat(int(chat_id))

        # referrals
        for raw_uid, raw_ref in (data.get("referred_by") or {}).items():
            self._run(
                "INSERT OR IGNORE INTO referrals(user_id, referrer_id, rewarded, created_at) "
                "VALUES(?, ?, ?, ?)",
                (int(raw_uid), int(raw_ref),
                 1 if int(raw_uid) in {int(x) for x in (data.get("referral_rewarded") or [])} else 0,
                 time.time()),
            )
        for raw_admin, conf in (data.get("referral_config") or {}).items():
            store_id = name_to_store.get((int(raw_admin), (conf or {}).get("store")))
            if store_id:
                self.set_referral_cfg(int(raw_admin), store_id, int((conf or {}).get("days") or 0))

        # drip
        def _split_key(key: str) -> tuple[int, str]:
            admin_part, _, store_part = key.partition(":")
            try:
                return int(admin_part), store_part
            except ValueError:
                return 0, store_part

        sent_raw = data.get("drip_sent_ids") or {}
        subs_raw = data.get("drip_subscribers") or {}
        for key, conf in (data.get("drip_config") or {}).items():
            admin_id, store_name = _split_key(key)
            store_id = name_to_store.get((admin_id, store_name)) or name_to_store.get(
                (int((conf or {}).get("admin_uid") or 0), (conf or {}).get("store") or store_name)
            )
            if not store_id:
                continue
            self.upsert_drip(store_id, admin_id, int(conf.get("count") or 3), conf.get("time") or "19:30")
            self.set_drip_enabled(store_id, bool(conf.get("enabled")))
            self.set_drip_last_sent(store_id, conf.get("last_sent_date") or "")
            for old_fid in sent_raw.get(key, []):
                if old_fid in old_fid_map:
                    self.mark_sent(store_id, [old_fid_map[old_fid]])
            for uid in subs_raw.get(key, []):
                self._run("INSERT OR IGNORE INTO drip_subs(store_id, user_id) VALUES(?, ?)",
                          (store_id, int(uid)))

        # settings
        if data.get("force_channel"):
            self.set_meta("force_channel", data["force_channel"])
        if data.get("custom_caption"):
            self.set_meta("custom_caption", data["custom_caption"])
        if data.get("active_store"):
            for raw_admin, store_name in data["active_store"].items():
                store_id = name_to_store.get((int(raw_admin), store_name))
                if store_id:
                    self.set_active_store(int(raw_admin), store_id)

        self.set_meta("legacy_imported", "yes")
        log.info("Legacy JSON imported: %s", counters)
        return counters


class _LazyDB:
    """Module level handle so handlers can just do `db.files_of(...)`."""

    def __init__(self) -> None:
        self._impl: Database | None = None

    def bind(self, path: str) -> Database:
        self._impl = Database(path)
        return self._impl

    @property
    def impl(self) -> Database:
        if self._impl is None:
            self.bind(cfg.DB_FILE)
        assert self._impl is not None
        return self._impl

    def __getattr__(self, item: str):
        return getattr(self.impl, item)


db = _LazyDB()

"""The website: a public store front **and** the full admin panel.

One FastAPI app, two halves:

    Public  (no login)
        /                 store front — every store with an “Open in Telegram” link
        /stores           all stores
        /s/<slug>         one store (+ file list for free stores)
        /search?q=        search free content
        /health           JSON health check for uptime monitors

    Admin   (login with WEB_USER / WEB_PASS — cookie, HTTP-Basic also accepted)
        /admin                    dashboard: users, views, revenue, alerts
        /admin/broadcast          📢 broadcast studio (create + history + live progress)
        /admin/broadcast/<id>     live progress of one campaign
        /admin/files              every file + 📢 “broadcast this video”
        /admin/stores|users|orders|download|settings

How to run it
    # only the website
    python -m web.dashboard
    # website + bot in one process (what hosting panels like CodeNest expect)
    python run.py

The panel talks to the *same* SQLite file as the bot, so nothing needs syncing.
Broadcasts started here are queued in the database; if the bot is offline the
campaign waits and starts by itself the moment the bot is online again.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import re
import secrets
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import quote_plus

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Form, Request                                    # noqa: E402
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,     # noqa: E402
                               RedirectResponse, Response)

from app import config as cfg                                                 # noqa: E402
from app import runtime                                                       # noqa: E402
from app.logger import log, setup_logging                                     # noqa: E402
from app.services import (access, basepath, billing, bot_texts, broadcast,    # noqa: E402
                          channels, forcejoin, secrets_guard, settings)
from app.storage import db                                                    # noqa: E402
from app.utils import esc, fmt_ts, human_size, money                          # noqa: E402
from web import panels                                                        # noqa: E402

STARTED_AT = time.time()
COOKIE_NAME = "sb_session"

# --------------------------------------------------------------------- helpers
def _bot_username() -> str:
    """Bot username for deep links — works even while the bot is offline."""
    return runtime.bot_username or db.get_meta("bot_username", "") or ""


def _panel_password() -> str:
    """WEB_PASS, or a generated one (logged once) so the panel is never wide open."""
    if cfg.WEB_PASS:
        return cfg.WEB_PASS
    generated = db.get_meta("web_generated_pass", "")
    if not generated:
        generated = secrets.token_urlsafe(9)
        db.set_meta("web_generated_pass", generated)
        log.warning("WEB_PASS was not set — generated a temporary one: %s "
                    "(set WEB_PASS in config.env / hosting env to change it)", generated)
    return generated


def _session_token() -> str:
    secret = cfg.WEB_SECRET or _panel_password()
    raw = f"{cfg.WEB_USER}:{secret}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _cookie_ok(request: Request) -> bool:
    cookie = request.cookies.get(COOKIE_NAME, "")
    return bool(cookie) and hmac.compare_digest(cookie, _session_token())


def _basic_ok(request: Request) -> bool:
    header = request.headers.get("Authorization")
    if not header or not header.startswith("Basic "):
        return False
    import base64
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        user, _, password = decoded.partition(":")
    except Exception:
        return False
    return (hmac.compare_digest(user, cfg.WEB_USER)
            and hmac.compare_digest(password, _panel_password()))


def _authed(request: Request) -> bool:
    return _cookie_ok(request) or _basic_ok(request)


def deep_link(payload: str) -> str:
    username = _bot_username()
    if not username:
        return ""
    return f"https://t.me/{username}?start={quote_plus(payload)}"


def _start(ts: float | None, pattern: str = "%d %b %H:%M") -> str:
    return fmt_ts(ts, pattern) if ts else "—"


# ---------------------------------------------------------------------- layout
CSS = """
 :root { color-scheme: dark; --bg:#0b0f16; --card:#141a24; --line:#222b3a;
         --mut:#8fa0b8; --acc:#22c55e; --acc2:#2563eb; --warn:#f59e0b; --bad:#ef4444; }
 * { box-sizing: border-box; }
 body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans Bengali", sans-serif;
        margin:0; background:var(--bg); color:#e8eef7; line-height:1.5; }
 header { background:#101725; padding:14px 20px; display:flex; justify-content:space-between;
          align-items:center; flex-wrap:wrap; gap:12px; border-bottom:1px solid var(--line); }
 header h1 { font-size:17px; margin:0; display:flex; align-items:center; gap:8px; }
 nav a { color:var(--mut); text-decoration:none; margin-right:14px; font-size:14px; }
 nav a.on, nav a:hover { color:#e8eef7; }
 main { padding:20px; max-width:1150px; margin:0 auto; }
 .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(165px,1fr)); gap:12px; }
 .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; }
 .card .l { color:var(--mut); font-size:13px; } .card .n { font-size:25px; font-weight:700; margin-top:4px; }
 table { width:100%; border-collapse:collapse; margin-top:10px; font-size:14px; }
 th,td { text-align:left; padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
 th { color:var(--mut); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.03em; }
 a.btn, button { background:var(--acc2); color:#fff; border:0; padding:9px 13px; border-radius:9px;
                 text-decoration:none; font-size:13.5px; cursor:pointer; display:inline-block; }
 a.ok, button.ok { background:#059669; } a.bad, button.bad { background:#b91c1c; }
 a.grey, button.grey { background:#374151; } a.warn, button.warn { background:#b45309; }
 input, textarea, select { background:#0b111b; color:#e8eef7; border:1px solid #2b3648;
                 border-radius:9px; padding:10px 12px; width:100%; margin:6px 0; font:inherit; }
 label { font-size:13px; color:var(--mut); }
 .muted { color:var(--mut); font-size:13px; }
 .chk { display:flex; align-items:center; gap:8px; color:#e8eef7; font-size:14px; margin:8px 0; }
 .chk input { width:auto; margin:0; }
 .pill { background:#1f2937; padding:2px 9px; border-radius:999px; font-size:12px; }
 .flash { background:#06443a; border:1px solid #0b7a5f; padding:11px 14px; border-radius:11px; margin-bottom:14px; }
 .warnbox { background:#4a2c06; border:1px solid #92610d; padding:11px 14px; border-radius:11px; margin-bottom:14px; }
 .hero { padding:38px 0 22px; }
 .hero h2 { font-size:34px; margin:0 0 10px; line-height:1.2; }
 .hero p { color:var(--mut); font-size:16px; max-width:640px; }
 .stores { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:14px; margin-top:18px; }
 .store { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px;
          display:flex; flex-direction:column; gap:8px; }
 .store h3 { margin:0; font-size:17px; }
 .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
 .bar { background:#0b111b; border-radius:999px; height:12px; overflow:hidden; border:1px solid var(--line); }
 .bar > i { display:block; height:100%; background:linear-gradient(90deg,#22c55e,#2563eb); }
 footer { color:var(--mut); font-size:13px; text-align:center; padding:28px 16px; }
 .logline { white-space:pre-wrap; word-break:break-word; border-bottom:1px solid #1a2334; padding:3px 2px; }
 .logline:first-child { color:#e8eef7; }
 .howto { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px 16px;
          margin:16px 0; display:flex; flex-wrap:wrap; gap:12px; align-items:center; font-size:14px; }
 .howto b { color:var(--acc); }
 .howto span { background:#1f2937; border-radius:999px; padding:4px 12px; }
 ol.steps { padding-left:22px; } ol.steps li { margin:8px 0; }
 details { background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:12px 14px; margin:10px 0; }
 details summary { cursor:pointer; font-weight:600; }
 details p { color:var(--mut); margin:10px 0 2px; }
 .kv { display:grid; grid-template-columns:auto 1fr; gap:4px 14px; font-size:14px; }
 .kv div:nth-child(odd) { color:var(--mut); }
"""


def layout(title: str, body: str, active: str = "", admin: bool = True) -> str:
    public_nav = [
        ("/", "🏪 Stores"),
        ("/search", "🔍 Search"),
        ("/help", "❓ Help"),
    ]
    admin_nav = [
        ("/admin", "📊 Dashboard"),
        ("/admin/broadcast", "📢 Broadcast"),
        ("/admin/channels", "📡 Channels"),
        ("/admin/analytics", "📊 Analytics"),
        ("/admin/texts", "📝 Bot texts"),
        ("/admin/links", "🔗 Links"),
        ("/admin/stores", "🏪 Stores"),
        ("/admin/files", "🗂 Files"),
        ("/admin/users", "👥 Users"),
        ("/admin/orders", "🧾 Orders"),
        ("/admin/download", "📥 Export"),
        ("/admin/settings", "🛠 Settings"),
        ("/admin/self-test", "🩺 Health"),
        ("/admin/security", "🔐 Security"),
    ]
    items = public_nav + admin_nav
    nav = "".join(
        f'<a class="{"on" if href == active else ""}" href="{href}">{label}</a>'
        for href, label in items
    )
    right = ('<a class="btn grey" href="/admin/logout">Logout</a>' if admin
             else '<a class="btn" href="/admin">Admin</a>')
    brand = html.escape(settings.get_str("WEB_TITLE", "Store") or "Store") + " · Bot"
    # Behind a hosting panel the site lives under /live/<job>/ (the proxy sends
    # X-Forwarded-Prefix). `<base href>` makes every link in the page resolve
    # through that prefix, so the same HTML works on the panel and on the root.
    base = basepath.current() or ""
    page = basepath.relativize(f"""<!doctype html><html lang="bn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<base href="__BASEPATH__/">
<title>{html.escape(title)} · {brand}</title>
<meta name="description" content="Telegram store — files delivered instantly by bot.">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎬</text></svg>">
<style>{CSS}</style></head><body>
<header>
  <h1>🎬 {brand}</h1>
  <nav>{nav}</nav>
  {right}
</header>
<main>{body}</main>
<footer>Powered by the Telegram store bot · <span class="muted">files are delivered by the bot itself</span></footer>
</body></html>""")
    # The <base> itself must stay root-absolute — relativize() runs first.
    return page.replace("__BASEPATH__", base)


def card(label: str, value) -> str:
    return f'<div class="card"><div class="l">{esc(label)}</div><div class="n">{value}</div></div>'


def store_cards(stores: list[dict]) -> str:
    if not stores:
        return '<div class="card"><p class="muted">এখনো কোনো স্টোর নেই — অ্যাডমিন শীঘ্রই কনটেন্ট যোগ করবেন।</p></div>'
    blocks = []
    for store in stores:
        stats = db.store_stats(store["id"])
        lock = "🔒 Premium" if store["is_premium"] else "🆓 Free"
        link = deep_link(f"s{store['id']}") or deep_link(store["slug"] or "")
        desc = esc((store.get("description") or "")[:140])
        blocks.append(f"""<div class="store">
   <h3>{esc(store['name'])}</h3>
   <div class="row"><span class="pill">{lock}</span>
        <span class="pill">📂 {stats['files']} files</span>
        <span class="pill">👁 {stats['views']}</span></div>
   {f'<p class="muted">{desc}</p>' if desc else ''}
   <div class="row">
     {f'<a class="btn" href="{link}">▶️ Telegram-এ খুলুন</a>' if link else ''}
     <a class="btn grey" href="/s/{esc(store['slug'] or store['id'])}">বিস্তারিত</a>
   </div>
 </div>""")
    return f'<div class="stores">{"".join(blocks)}</div>'


# ================================================================= public site
def create_app() -> FastAPI:
    app = FastAPI(title="Store Bot Web", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------------- site
    @app.middleware("http")
    async def honor_proxy_prefix(request: Request, call_next):
        """Live behind CodeNest/RunSpace: keep every link inside /live/<job>/.

        The panel's gateway strips the prefix before forwarding it and tells us
        what it was in `X-Forwarded-Prefix`. We remember it for this request (so
        `layout()` can emit the right `<base href>`) and set Starlette's
        `root_path`, so anything the framework generates matches as well.
        """
        prefix = basepath.set_prefix(request.headers.get("x-forwarded-prefix")
                                     or request.headers.get("x-script-name"))
        if prefix:
            request.scope["root_path"] = prefix
        try:
            return await call_next(request)
        finally:
            basepath.set_prefix("")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> str:
        stores = db.all_stores()
        stats = db.stats()
        bots = _bot_username()
        body = f"""
        <div class="hero">
          <h2>এক ট্যাপে ফাইল, সিরিজ আর ভিডিও 🎬</h2>
          <p>{esc(settings.get_str("SITE_TAGLINE")) or "টেলিগ্রাম বটে স্টোর থেকে ফাইল বেছে নিন — বটই ফাইলটি পাঠিয়ে দেবে। নিচের যেকোনো স্টোরে ঢুকুন।"}</p>
          <div class="row">
            {f'<a class="btn" href="https://t.me/{bots}">🤖 বটে যান (@{bots})</a>' if bots else ''}
            <a class="btn grey" href="#stores">🏪 স্টোর দেখুন</a>
          </div>
        </div>
        <div class="grid">
          {card("Stores", stats['stores'])}
          {card("Files", stats['files'])}
          {card("Members", stats['users'])}
          {card("Views", stats['views'])}
        </div>
        <div class="howto">
          <b>কীভাবে ফাইল পাবেন?</b>
          <span>১️⃣ বট খুলুন</span><span>২️⃣ স্টোর বাছুন</span>
          <span>৩️⃣ ভিডিওর নামে চাপ দিন — সোজা ভিডিও</span>
        </div>
        <h3 id="stores">🏪 সব স্টোর</h3>
        {store_cards(stores)}
        """
        return layout("Home", body, "/", admin=_cookie_ok(request))

    @app.get("/help", response_class=HTMLResponse)
    async def public_help() -> str:
        """A plain-language FAQ — the “users don't understand much” fix."""
        contact = (settings.get_str("SUPPORT_CONTACT") or "").strip()
        if contact.startswith(("http://", "https://", "tg://")):
            contact_link = f'<a class="btn" href="{esc(contact)}">📞 অ্যাডমিনের সাথে কথা বলুন</a>'
        elif contact.startswith("@"):
            contact_link = (f'<a class="btn" href="https://t.me/{esc(contact[1:])}">'
                            f'📞 অ্যাডমিনের সাথে কথা বলুন</a>')
        else:
            contact_link = ""
        bots = _bot_username()
        open_bot = f'<a class="btn" href="https://t.me/{bots}">🤖 বট খুলুন</a>' if bots else ""
        steps = """
        <ol class="steps">
          <li><b>বট খুলুন</b> — নিচের বাটনে চাপ দিলেই টেলিগ্রামে চলে যাবেন।</li>
          <li><b>স্টোর বেছে নিন</b> — 🏪 বাটনে চাপ দিলে সব স্টোর দেখতে পাবেন।</li>
          <li><b>ভিডিওর নামে চাপ দিন</b> — ভিডিও সাথে সাথে চ্যাটে চলে আসবে, আর কিছু করতে হবে না।</li>
        </ol>"""
        faq = """
        <details open><summary>ভিডিও পেতে কি টাকা লাগবে?</summary>
        <p>ফ্রি স্টোরের সব ভিডিও বিনা পয়সায়। 🔒 দিয়ে লেখা স্টোরগুলো প্রিমিয়াম —
        সেগুলোর জন্য বটের ভিতরে 💎 প্ল্যান থেকে কিনতে হয় (বিকাশ/নগদ/USDT)।</p></details>

        <details><summary>চ্যানেল জয়েন করতে বলছে কেন?</summary>
        <p>কিছু স্টোরে ভিডিওর আগে চ্যানেল জয়েন করা লাগে। একবার জয়েন করে
        “✅ আমি জয়েন করেছি” চাপ দিলেই ভিডিও চলে আসবে — বারবার লাগবে না।</p></details>

        <details><summary>“লিমিট শেষ” লিখছে — মানে কী?</summary>
        <p>কিছু লিংকে কতবার খোলা যাবে সেটা ঠিক করা থাকে (যেমন ১০০ বার)। সেটা শেষ হলে
        নতুন লিংক লাগবে — অ্যাডমিনের সাথে যোগাযোগ করুন, খুব দ্রুত দিয়ে দেবেন।</p></details>

        <details><summary>ভিডিও ডাউনলোড করলে কি সেভ হয়ে থাকবে?</summary>
        <p>হ্যাঁ। বট থেকে পাওয়া ভিডিও আপনার টেলিগ্রাম চ্যাটেই থাকে — পরে আবার দেখতে পারবেন,
        ইন্টারনেট ছাড়াও।</p></details>

        <details><summary>কিছু খুঁজে পাচ্ছি না</summary>
        <p>🔍 Search-এ নাম লিখে দেখুন, নাহলে বটে 🆘 Help বা “অ্যাডমিনের সাথে কথা বলুন”
        বাটনে চাপ দিন — সরাসরি মেসেজ চলে যাবে।</p></details>
        """
        body = f"""
        <h2>❓ কীভাবে ব্যবহার করবেন</h2>
        {steps}
        <div class="row">{open_bot}{contact_link}</div>
        <h3>সাধারণ প্রশ্ন</h3>
        {faq}
        """
        return layout("Help", body, "/help")

    @app.get("/stores", response_class=HTMLResponse)
    async def public_stores() -> str:
        return layout("Stores", f"<h3>🏪 সব স্টোর</h3>{store_cards(db.all_stores())}", "/stores")

    @app.get("/search", response_class=HTMLResponse)
    async def public_search(q: str = "") -> str:
        rows = []
        if q.strip():
            for store in db.all_stores():
                if store["is_premium"]:
                    continue
                for file_row in db.search_files(store["id"], q.strip().lower(), 30):
                    link = deep_link(f"f{file_row['id']}")
                    rows.append(
                        f"<tr><td>{esc(file_row['name'])}</td><td>{esc(store['name'])}</td>"
                        f"<td>{file_row['views']}</td>"
                        f"<td>{f'<a class=btn href={link}>পাঠান</a>' if link else ''}</td></tr>")
        table = ("<table><tr><th>Name</th><th>Store</th><th>Views</th><th></th></tr>"
                 f"{''.join(rows)}</table>") if rows else (
            '<p class="muted">কিছু পাওয়া যায়নি (প্রিমিয়াম স্টোরের কনটেন্ট নাম শুধু বটে দেখা যায়)।</p>')
        body = (f"<h3>🔍 সার্চ</h3>"
                f"<form method=get><input name=q placeholder='ফাইলের নাম লিখুন' value='{esc(q)}'></form>"
                f"{table}")
        return layout("Search", body, "/search")

    def _find_store(slug: str) -> dict | None:
        """Find a store from a URL slug — tolerant about -/_ and numeric ids."""
        candidates = [slug, slug.replace("-", "_"), slug.replace("_", "-")]
        for candidate in candidates:
            if not candidate:
                continue
            found = db.store_by_slug(candidate)
            if found:
                return found
            if candidate.isdigit():
                return db.store(int(candidate))
        return None

    @app.get("/s/{slug}", response_class=HTMLResponse)
    async def public_store(slug: str) -> str:
        store = _find_store(slug)
        if store is None:
            return HTMLResponse(layout("Not found", "<h3>স্টোরটি পাওয়া যায়নি</h3>"), status_code=404)
        stats = db.store_stats(store["id"])
        link = deep_link(f"s{store['id']}")
        if store["is_premium"]:
            files_html = ('<p class="muted">🔒 এটি একটি প্রিমিয়াম স্টোর — ফাইলের তালিকা ও '
                          'ডাউনলোড বটের ভিতরে (অ্যাক্সেস কেনার পর)।</p>')
        else:
            rows_list = []
            for file_row in db.files_of(store["id"], newest_first=True)[:200]:
                send = deep_link(f"f{file_row['id']}")
                button = f'<a class="btn" href="{send}">পাঠান</a>' if send else ""
                rows_list.append(
                    f"<tr><td>{esc(file_row['name'])}</td><td>{esc(file_row['kind'])}</td>"
                    f"<td>{file_row['views']}</td><td>{button}</td></tr>")
            rows = "".join(rows_list)
            files_html = (f"<table><tr><th>Name</th><th>Type</th><th>Views</th><th></th></tr>{rows}</table>"
                          if rows else '<p class="muted">এই স্টোরে এখনো ফাইল নেই।</p>')
        body = f"""
        <h2>{esc(store['name'])} <span class="pill">{'🔒 Premium' if store['is_premium'] else '🆓 Free'}</span></h2>
        <p class="muted">{esc(store.get('description') or '')}</p>
        <div class="grid">{card("Files", stats['files'])}{card("Views", stats['views'])}{card("Members", stats['grants'])}</div>
        <p>{f'<a class="btn" href="{link}">▶️ বটে খুলুন</a>' if link else '<span class="muted">বট এখনো অনলাইন হয়নি</span>'}</p>
        {files_html}
        """
        return layout(store["name"], body, "/stores")

    # ------------------------------------------------------------------- auth
    @app.get("/admin/login", response_class=HTMLResponse)
    async def login_form(error: str = "") -> str:
        note = '<div class="warnbox">ইউজারনেম বা পাসওয়ার্ড ভুল।</div>' if error else ""
        body = f"""
        {note}
        <div class="card" style="max-width:420px;margin:40px auto">
          <h3>🔐 অ্যাডমিন লগইন</h3>
          <form method=post action=/admin/login>
            <label>Username</label><input name=username value="admin" autocomplete=username>
            <label>Password</label><input name=password type=password autocomplete=current-password>
            <button type=submit>লগইন</button>
          </form>
          <p class="muted">পাসওয়ার্ড সেট করা থাকে <code>WEB_PASS</code> এনভায়রনমেন্ট ভেরিয়েবলে।
          সেট না থাকলে বট লগে একটা টেম্পোরারি পাসওয়ার্ড দেখানো হয়।</p>
        </div>"""
        return layout("Login", body, "", admin=False)

    @app.post("/admin/login")
    async def login(username: str = Form(""), password: str = Form("")):
        if not (hmac.compare_digest(username, cfg.WEB_USER)
                and hmac.compare_digest(password, _panel_password())):
            return RedirectResponse("/admin/login?error=1", status_code=303)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(COOKIE_NAME, _session_token(), httponly=True,
                            samesite="lax", max_age=cfg.WEB_SESSION_HOURS * 3600,
                            path="/")
        return response

    @app.get("/admin/logout")
    async def logout():
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    def _guard(request: Request):
        """Return a response when the request must be blocked, else None."""
        if _authed(request):
            return None
        if request.headers.get("Authorization"):
            return Response("Unauthorized", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="store-admin"'})
        return RedirectResponse("/admin/login", status_code=303)

    # -------------------------------------------------------------- dashboard
    @app.get("/admin", response_class=HTMLResponse)
    async def admin_home(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        stats = db.stats()
        revenue = db.revenue()
        today = datetime.now().strftime("%Y-%m-%d")
        recent = db.campaigns(limit=5)
        campaign_rows = "".join(
            f"<tr><td>#{c['id']}</td><td>{esc(c['title'])}</td>"
            f"<td>{esc(broadcast.audience_label(c['audience']))}</td>"
            f"<td>{c['status']}</td><td>{c['sent']}/{c['total']}</td>"
            f"<td><a class='btn grey' href='/admin/broadcast/{c['id']}'>খুলুন</a></td></tr>"
            for c in recent) or "<tr><td colspan=6 class='muted'>এখনো ব্রডকাস্ট নেই</td></tr>"
        bot_state = ("🟢 চলছে" if runtime.bot_online() else "🔴 বট অফলাইন "
                     "(ব্রডকাস্ট কিউতে থাকবে)")
        body = f"""
        <h3>📊 ড্যাশবোর্ড</h3>
        <div class="grid">
          {card("Users", stats['users'])} {card("Files", stats['files'])}
          {card("Stores", stats['stores'])} {card("Views", stats['views'])}
          {card("Granted", stats['grants'])} {card("Revenue", money(revenue['total'], settings.get_str('CURRENCY', '৳')))}
          {card("Pending orders", revenue['pending'])} {card("Open tickets", stats['tickets_open'])}
        </div>
        <p class="muted">বট: {bot_state} · সার্ভার আপটাইম: {int(time.time() - STARTED_AT)}s · আজ: {today}</p>
        <h3>📢 সাম্প্রতিক ব্রডকাস্ট</h3>
        <table><tr><th>#</th><th>Title</th><th>Audience</th><th>Status</th><th>Sent</th><th></th></tr>
        {campaign_rows}</table>
        <p><a class="btn" href="/admin/broadcast">📢 নতুন ব্রডকাস্ট</a>
           <a class="btn grey" href="/admin/files">🗂 ফাইল</a></p>
        """
        return layout("Dashboard", body, "/admin")

    # ------------------------------------------------------- broadcast studio
    def _files_options(selected: int = 0) -> str:
        options = ['<option value="0">— কোনো ফাইল ছাড়া (শুধু টেক্সট) —</option>']
        for store in db.all_stores():
            for file_row in db.files_of(store["id"], newest_first=True)[:200]:
                mark = " selected" if file_row["id"] == selected else ""
                options.append(
                    f'<option value="{file_row["id"]}"{mark}>'
                    f'{esc(store["name"])} · {esc(file_row["name"][:60])}</option>')
        return "".join(options)

    def _audience_options() -> str:
        rows = ['<option value="all">📣 All users</option>',
                '<option value="premium">💎 Premium buyers</option>',
                '<option value="free">🆓 Free users</option>']
        for store in db.all_stores():
            for kind, label in (("store", "সবাই"), ("sub", "সাবস্ক্রাইবার"), ("nosale", "কখনো কেনেনি")):
                rows.append(f'<option value="{kind}:{store["id"]}">🏪 {esc(store["name"])} · {label}</option>')
        return "".join(rows)

    def _campaign_table(campaigns: list[dict]) -> str:
        rows = []
        for campaign in campaigns:
            counts = db.campaign_counts(campaign["id"], sync=False)
            rows.append(
                f"<tr><td>#{campaign['id']}</td>"
                f"<td>{esc(campaign['title'] or '')}<div class='muted'>{esc((campaign['text'] or '')[:70])}</div></td>"
                f"<td>{esc(broadcast.audience_label(campaign['audience']))}</td>"
                f"<td><span class='pill'>{campaign['status']}</span></td>"
                f"<td>✅ {counts['sent']} · ⚠️ {counts['failed']} · 🚫 {counts['blocked']}<div class='muted'>🕓 {counts['pending']}/{counts['total']}</div></td>"
                f"<td>{_start(campaign['created_at'])}</td>"
                f"<td><a class='btn grey' href='/admin/broadcast/{campaign['id']}'>খুলুন</a></td></tr>")
        return ("<table><tr><th>#</th><th>Message</th><th>Audience</th><th>Status</th>"
                "<th>Progress</th><th>Created</th><th></th></tr>"
                f"{''.join(rows)}</table>") if rows else "<p class='muted'>এখনো কোনো ক্যাম্পেইন নেই।</p>"

    @app.get("/admin/broadcast", response_class=HTMLResponse)
    async def studio(request: Request, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        bot_ready = runtime.bot_online()
        body = f"""
        {note}{warnbox}
        <h3>📢 ব্রডকাস্ট স্টুডিও</h3>
        <p class="muted">অডিয়েন্স বাছুন, মেসেজ লিখুন (বা একটি ভিডিও/ফাইল বেছে নিন) — কিউ
        ডেটাবেজে থাকে, তাই বট রিস্টার্ট হলেও ব্রডকাস্ট থেমে যায় না।
        বট <b>{'🟢 অনলাইন' if bot_ready else '🔴 অফলাইন'}</b>।</p>
        <div class="card">
          <form method=post action="/admin/broadcast/new">
            <label>Audience</label>
            <select name=audience>{_audience_options()}</select>
            <label>Attach a file / video (optional)</label>
            <select name=file_id>{_files_options()}</select>
            <label>Message (HTML allowed, <code>{{name}}</code> = user's name)</label>
            <textarea name=text rows=6 placeholder="আজকের নতুন ভিডিও এসেছে 🎬"></textarea>
            <label>ইনলাইন বাটন — প্রতি লাইনে <code>লেবেল | লিংক</code>
                   (একই লাইনে <code>&amp;&amp;</code> দিলে পাশাপাশি)</label>
            <textarea name=buttons rows=3>{settings.get_str('BROADCAST_DEFAULT_BUTTONS') or "📢 জয়েন | https://t.me/mychannel"}</textarea>
            <div class="chk"><input type=checkbox name=online value=1 checked id=ob>
              <label for=ob>মেসেজের নিচে “🟢 অনলাইন — স্টোর খুলুন” বাটন দিন</label></div>
            <div class="row">
              <button class="ok" name=action value=start>🚀 তৈরি করে শুরু করুন</button>
              <button class="grey" name=action value=draft>📝 খসড়া রাখুন</button>
              <button class="warn" name=action value=test>🧪 আমাকে টেস্ট পাঠান</button>
            </div>
            <p class="muted">অডিয়েন্স সাইজ এখন: সব {len(broadcast.resolve_audience(0, 'all'))} জন ·
            প্রিমিয়াম {len(broadcast.resolve_audience(0, 'premium'))} জন ·
            ফ্রি {len(broadcast.resolve_audience(0, 'free'))} জন</p>
          </form>
        </div>
        <p class="row">
          <a class="btn grey" href="/admin/links">🔗 লিমিটেড লিংক বানান</a>
          <a class="btn grey" href="/admin/channels/compose">📡 চ্যানেলে পোস্ট</a>
          <a class="btn warn" href="/admin/digest">🆕 সাপ্তাহিক ডাইজেস্ট এখনই পাঠান</a>
        </p>
        <h3>📜 ক্যাম্পেইন</h3>
        {_campaign_table(db.campaigns(limit=40))}
        """
        return layout("Broadcast", body, "/admin/broadcast")

    @app.post("/admin/broadcast/new")
    async def studio_create(request: Request, audience: str = Form("all"),
                            file_id: int = Form(0), text: str = Form(""),
                            action: str = Form("start"), buttons: str = Form(""),
                            online: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        body = (text or "").strip()
        files = [file_id] if file_id else []
        if not body and not files:
            return RedirectResponse("/admin/broadcast?warn=মেসেজ+বা+ফাইল+কিছুই+দিননি",
                                    status_code=303)
        targets = broadcast.resolve_audience(0, audience)
        if not targets:
            return RedirectResponse("/admin/broadcast?warn=এই+অডিয়েন্সে+কেউ+নেই", status_code=303)
        owner = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 0
        # status "queued": it starts now when the bot is online, otherwise the
        # scheduler starts it the moment the bot connects.
        campaign = broadcast.create_campaign(owner, text=body, audience=audience,
                                            files=files, start=(action != "draft"),
                                            buttons=buttons,
                                            online_button=1 if online else 0)
        campaign_id = campaign["id"]
        if action == "test":
            if not cfg.ADMIN_IDS:
                return RedirectResponse("/admin/broadcast?warn=ADMIN_IDS+সেট+নেই,+টেস্ট+পাঠানো+যাবে+না",
                                        status_code=303)
            if not runtime.bot_online():
                return RedirectResponse(
                    "/admin/broadcast?warn=বট+অফলাইন+—+টেস্ট+পাঠানো+যাবে+না", status_code=303)
            outcome = await broadcast.test_send(campaign_id, [cfg.ADMIN_IDS[0]])
            if not outcome.get("ok"):
                detail = (outcome.get("errors") or ["unknown"])[0]
                return RedirectResponse(
                    f"/admin/broadcast/{campaign_id}?warn=টেস্ট+ব্যর্থ:+{esc(detail)}"[:400],
                    status_code=303)
            return RedirectResponse(
                f"/admin/broadcast/{campaign_id}?flash=🧪+টেস্ট+সফল+(#{campaign_id})",
                status_code=303)
        if action == "draft":
            return RedirectResponse(
                f"/admin/broadcast?flash=📝+খসড়া+সেভ+(#{campaign_id})", status_code=303)
        # start: only if the bot is online in this process, otherwise it stays queued
        if runtime.bot_online():
            broadcast.start_campaign(campaign_id)
            return RedirectResponse(
                f"/admin/broadcast/{campaign_id}?flash=🚀+শুরু+হয়েছে", status_code=303)
        return RedirectResponse(
            f"/admin/broadcast/{campaign_id}?warn=বট+অফলাইন+—+অনলাইন+হলেই+অটো+শুরু+হবে",
            status_code=303)

    @app.get("/admin/broadcast/{campaign_id}", response_class=HTMLResponse)
    async def studio_detail(request: Request, campaign_id: int, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        campaign = db.campaign(campaign_id)
        if campaign is None:
            return RedirectResponse("/admin/broadcast?warn=ক্যাম্পেইন+নেই", status_code=303)
        counts = db.campaign_counts(campaign_id)
        progress = broadcast.campaign_progress(campaign_id)
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        items = db.campaign_items(campaign_id, limit=200)
        sample_rows = []
        for item in items[:60]:
            user = db.user(item["user_id"]) or {}
            who = esc(user.get("name") or "") or "—"
            if user.get("username"):
                who += f" <span class='muted'>@{esc(user['username'])}</span>"
            flag = {"sent": "✅", "pending": "⏳", "blocked": "🚫",
                    "failed": "⚠️"}.get(item["status"], "•")
            note = broadcast.explain_error(item["error"]) if item["error"] else ""
            sample_rows.append(
                f"<tr><td><a href='/admin/users/{item['user_id']}'>{item['user_id']}</a><br>"
                f"<span class='muted'>{who}</span></td>"
                f"<td>{flag} {esc(item['status'])}</td><td class='muted'>{esc(note)}</td></tr>")
        sample = "".join(sample_rows)

        # “Why did only the test message arrive?” — answered right on this page.
        reasons = broadcast.failure_breakdown(campaign_id)
        reason_html = "".join(
            f"<li><b>{esc(group['hint'])}</b> — {group['count']} জন "
            f"<span class='muted'>({', '.join(str(u) for u in group['users'])}…)</span></li>"
            for group in reasons)
        why = (f"<div class='card'><h4>❓ কেন সবাই পায়নি</h4><ul>{reason_html}</ul>"
               f"<p class='muted'>একই কারণে আটকে থাকা সবাইকে এক ক্লিকে আবার পাঠাতে "
               f"🔁 Retry failed চাপুন। ইউজার বটকে ব্লক করলে সেটা টেলিগ্রামের নিয়ম — "
               f"শুধু বট-ই মেসেজ পাঠাতে পারে না, কোনো উপায় নেই।</p></div>") if reasons else ""
        missing_media = [fid for fid in (campaign.get("file_ids") or [])
                         if not (db.file(fid) or {}).get("mirror_msg")]
        cache_warn = (f"<div class='warnbox'>🎞 {len(missing_media)} টি ফাইলের বট-কপি এখনো "
                      f"তৈরি হয়নি — প্রথম পাঠানোর সময় তৈরি হবে (একটু ধীর লাগতে পারে)। "
                      f"আগেই সবার জন্য তৈরি করতে <a href='/admin/files'>📥 ফাইল পেজ</a> থেকে "
                      f"“সব ফাইল ক্যাশ করুন” চাপুন।</div>") if missing_media else ""
        body = f"""
        {note}{warnbox}
        <h3>📢 ক্যাম্পেইন #{campaign_id}</h3>
        <div class="grid">
          {card("Status", campaign['status'])} {card("Total", counts['total'])}
          {card("Sent", counts['sent'])} {card("Failed", counts['failed'])}
          {card("Blocked", counts['blocked'])} {card("Pending", counts['pending'])}
        </div>
        <div class="bar" style="margin:14px 0"><i style="width:{progress['percent']}%"></i></div>
        <p class="muted">Audience: {esc(broadcast.audience_label(campaign['audience']))} ·
        Created {_start(campaign['created_at'])} ·
        Started {_start(campaign['started_at'])} · Finished {_start(campaign['finished_at'])}</p>
        <div class="card"><h4>প্রিভিউ</h4><div>{broadcast.preview(campaign) or '<span class=muted>— (শুধু ফাইল) —</span>'}</div></div>
        <div class="row" style="margin:14px 0">
          <form method=post action="/admin/broadcast/{campaign_id}/start"><button class="ok">🚀 Send now</button></form>
          <form method=post action="/admin/broadcast/{campaign_id}/cancel"><button class="bad">⏹ Stop</button></form>
          <form method=post action="/admin/broadcast/{campaign_id}/retry"><button class="warn">🔁 Retry failed</button></form>
          <form method=post action="/admin/broadcast/{campaign_id}/test"><button class="grey">🧪 Test to me</button></form>
          <form method=post action="/admin/broadcast/{campaign_id}/delete"><button class="bad">🗑 Delete</button></form>
        </div>
        {cache_warn}{why}
        <h4>কিউ (প্রথম ৬০ জন) — কে পেল, কে পেল না</h4>
        <table><tr><th>ইউজার</th><th>অবস্থা</th><th>কারণ / নোট</th></tr>{sample or "<tr><td colspan=3 class=muted>খালি</td></tr>"}</table>
        <script>
          const bar = document.querySelector('.bar > i');
          async function tick() {{
            try {{
              const r = await fetch('/admin/api/campaign/{campaign_id}');
              const d = await r.json();
              if (bar) bar.style.width = d.percent + '%';
              document.querySelectorAll('.card .n')[1].textContent = d.total;
              document.querySelectorAll('.card .n')[2].textContent = d.sent;
              document.querySelectorAll('.card .n')[3].textContent = d.failed;
              document.querySelectorAll('.card .n')[4].textContent = d.blocked;
              document.querySelectorAll('.card .n')[5].textContent = d.pending;
              if (!d.running) {{ clearInterval(timer); }}
            }} catch (e) {{}}
          }}
          const timer = setInterval(tick, 3000);
        </script>
        """
        return layout(f"Campaign {campaign_id}", body, "/admin/broadcast")

    @app.post("/admin/broadcast/{campaign_id}/start")
    async def studio_start(request: Request, campaign_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        if not runtime.bot_online():
            # Queue it (not “draft”) so the scheduler starts it by itself the
            # moment the bot signs in — that is what the message promises.
            db.update_campaign(campaign_id, status=broadcast.QUEUED)
            return RedirectResponse(
                f"/admin/broadcast/{campaign_id}?warn=বট+অফলাইন+—+অনলাইন+হলেই+অটো+শুরু+হবে",
                status_code=303)
        db.rearm_campaign_items(campaign_id, ("failed",))
        db.campaign_counts(campaign_id)
        broadcast.start_campaign(campaign_id)
        return RedirectResponse(f"/admin/broadcast/{campaign_id}?flash=🚀+শুরু+হয়েছে",
                                status_code=303)

    @app.post("/admin/broadcast/{campaign_id}/cancel")
    async def studio_cancel(request: Request, campaign_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        broadcast.cancel_campaign(campaign_id)
        return RedirectResponse(f"/admin/broadcast/{campaign_id}?flash=⏹+থামানো+হলো",
                                status_code=303)

    @app.post("/admin/broadcast/{campaign_id}/retry")
    async def studio_retry(request: Request, campaign_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        rearmed = db.rearm_campaign_items(campaign_id, ("failed", "blocked"))
        db.campaign_counts(campaign_id)
        if runtime.bot_online() and rearmed:
            broadcast.start_campaign(campaign_id)
        return RedirectResponse(
            f"/admin/broadcast/{campaign_id}?flash=🔁+{rearmed}+জনকে+আবার", status_code=303)

    @app.post("/admin/broadcast/{campaign_id}/test")
    async def studio_test(request: Request, campaign_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        if not cfg.ADMIN_IDS:
            return RedirectResponse(
                f"/admin/broadcast/{campaign_id}?warn=ADMIN_IDS+সেট+নেই", status_code=303)
        result = await broadcast.test_send(campaign_id, [cfg.ADMIN_IDS[0]])
        message = "🧪+টেস্ট+পাঠানো+হয়েছে" if result.get("ok") else "❌+টেস্ট+পাঠানো+যায়নি"
        return RedirectResponse(f"/admin/broadcast/{campaign_id}?flash={message}", status_code=303)

    @app.post("/admin/broadcast/{campaign_id}/delete")
    async def studio_delete(request: Request, campaign_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.delete_campaign(campaign_id)
        return RedirectResponse("/admin/broadcast?flash=🗑+মুছে+ফেলা+হয়েছে", status_code=303)

    @app.get("/admin/api/campaign/{campaign_id}")
    async def campaign_api(request: Request, campaign_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        return JSONResponse(broadcast.campaign_progress(campaign_id))

    # --------------------------------------------------------------- content
    @app.get("/admin/stores", response_class=HTMLResponse)
    async def admin_stores(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        rows = []
        for store in db.all_stores():
            stats = db.store_stats(store["id"])
            sales = billing.store_sales(store["id"])
            drip = db.drip(store["id"])
            drip_txt = f"{drip['count']}/day {drip['send_time']}" if drip and drip["enabled"] else "off"
            rows.append(
                f"<tr><td><b>{esc(store['name'])}</b><div class='muted'>#{store['id']} · "
                f"{'🔒 premium' if store['is_premium'] else '🆓 free'}</div></td>"
                f"<td>{stats['files']}</td><td>{stats['views']}</td><td>{stats['grants']}</td>"
                f"<td>{money(sales['revenue'], settings.get_str('CURRENCY', '৳'))}</td>"
                f"<td>{len(db.plans(store['id'], only_active=True))}</td><td>{drip_txt}</td>"
                f"<td><a class='btn' href='/admin/stores/{store['id']}'>🛠 ম্যানেজ</a> "
                f"<a class='btn grey' href='/admin/analytics/store/{store['id']}'>📊</a> "
                f"<a class='btn grey' href='/s/{esc(store['slug'] or store['id'])}'>🌐</a></td></tr>")
        return layout("Stores", f"<h3>🏪 স্টোর</h3><table><tr><th>Store</th><th>Files</th><th>Views</th>"
                                f"<th>Granted</th><th>Revenue</th><th>Plans</th><th>Drip</th><th></th></tr>"
                                f"{''.join(rows)}</table>", "/admin/stores")

    @app.get("/admin/files", response_class=HTMLResponse)
    async def admin_files(request: Request, q: str = "", store: int = 0, flash: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        if q:
            matches = db.search_all_stores(q, 200)
        elif store:
            matches = db.files_of(store)
        else:
            matches = db.newest_files(200)
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        rows = []
        for row in matches:
            store_row = db.store(row["store_id"]) or {}
            rows.append(
                f"<tr><td>{row['id']}</td><td>{esc(row['name'])}</td>"
                f"<td>{esc(store_row.get('name') or '')}</td><td>{esc(row['kind'])}</td>"
                f"<td>{row['views']}</td><td>{human_size(row.get('size'))}</td>"
                f"<td><a class='btn' href='/admin/files/broadcast/{row['id']}'>📢</a> "
                f"<a class='btn bad' href='/admin/files/delete/{row['id']}'>🗑</a></td></tr>")
        body = f"""{note}
        {panels.files_tools_body()}
        <h3>🗂 ফাইল</h3>
        <form method=get><input name=q placeholder="সব স্টোরে সার্চ" value="{esc(q)}"></form>
        <table><tr><th>ID</th><th>Name</th><th>Store</th><th>Type</th><th>Views</th><th>Size</th><th></th></tr>
        {''.join(rows)}</table>"""
        return layout("Files", body, "/admin/files")

    @app.post("/admin/files/bulk-delete")
    async def admin_files_bulk_delete(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        form = await request.form()
        raw = str(form.get("ids", ""))
        ids = [int(part) for part in re.findall(r"\d+", raw)]
        if not ids:
            return RedirectResponse("/admin/files?flash=কোনো+ID+দেননি",
                                    status_code=303)
        count = db.delete_files(ids)
        db.log_event("files_purged", user_id=0, detail=f"ids={ids[:40]} n={count}")
        return RedirectResponse(f"/admin/files?flash=🗑+{count}+টি+ফাইল+মুছে+ফেলা+হলো",
                                status_code=303)

    @app.get("/admin/files/broadcast/{file_id}", response_class=HTMLResponse)
    async def file_broadcast(request: Request, file_id: int):
        """📢 per-video broadcast — the same campaign engine as the studio."""
        blocked = _guard(request)
        if blocked:
            return blocked
        file_row = db.file(file_id)
        if file_row is None:
            return RedirectResponse("/admin/files?flash=ফাইল+নেই", status_code=303)
        store_row = db.store(file_row["store_id"]) or {}
        body = f"""
        <h3>📢 ব্রডকাস্ট: {esc(file_row['name'])}</h3>
        <p class="muted">🏪 {esc(store_row.get('name') or '')} · 👁 {file_row['views']} views ·
        {esc(file_row['kind'])} {human_size(file_row.get('size'))}</p>
        <div class="card">
          <form method=post action="/admin/files/broadcast/{file_id}">
            <label>Audience</label><select name=audience>{_audience_options()}</select>
            <label>Caption (optional, <code>{{name}}</code> সমর্থিত)</label>
            <textarea name=text rows=4 placeholder="এই ভিডিওটি আপনার জন্য 🎬"></textarea>
            <label>ইনলাইন বাটন — প্রতি লাইনে একটা: <code>লেবেল | https://লিংক</code>
                   (একই লাইনে <code>&amp;&amp;</code> দিলে পাশাপাশি)</label>
            <textarea name=buttons rows=2 placeholder="🟢 স্টোর খুলুন | https://t.me/your_bot"></textarea>
            <label class="chk"><input type=checkbox name=online value=1 checked>
                   🟢 “অনলাইন — স্টোর খুলুন” বাটন যোগ করুন</label>
            <div class="row">
              <button class="ok" name=action value=start>🚀 শুরু করুন</button>
              <button class="warn" name=action value=test>🧪 আমাকে টেস্ট পাঠান</button>
              <a class="btn grey" href="/admin/broadcast">📢 পুরো স্টুডিও</a>
            </div>
          </form>
        </div>"""
        return layout("File broadcast", body, "/admin/files")

    @app.post("/admin/files/broadcast/{file_id}")
    async def file_broadcast_post(request: Request, file_id: int, audience: str = Form("all"),
                                  text: str = Form(""), action: str = Form("start"),
                                  buttons: str = Form(""), online: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        file_row = db.file(file_id)
        if file_row is None:
            return RedirectResponse("/admin/files?flash=ফাইল+নেই", status_code=303)
        if not broadcast.resolve_audience(0, audience):
            return RedirectResponse("/admin/files?flash=এই+অডিয়েন্সে+কেউ+নেই", status_code=303)
        owner = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 0
        campaign = broadcast.create_campaign(
            owner, text=(text or "").strip(), title=file_row["name"][:40],
            audience=audience, files=[file_id], start=True,
            buttons=buttons.strip(), online_button=1 if online else 0)
        if action == "test":
            if cfg.ADMIN_IDS:
                await broadcast.test_send(campaign["id"], [cfg.ADMIN_IDS[0]])
            return RedirectResponse(f"/admin/broadcast/{campaign['id']}?flash=🧪+টেস্ট+পাঠানো+হয়েছে",
                                    status_code=303)
        if runtime.bot_online():
            broadcast.start_campaign(campaign["id"])
            return RedirectResponse(f"/admin/broadcast/{campaign['id']}?flash=🚀+শুরু+হয়েছে",
                                    status_code=303)
        return RedirectResponse(
            f"/admin/broadcast/{campaign['id']}?warn=বট+অফলাইন+—+অনলাইন+হলেই+অটো+শুরু+হবে",
            status_code=303)

    @app.get("/admin/files/delete/{file_id}")
    async def admin_delete_file(request: Request, file_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.delete_file(file_id)
        return RedirectResponse("/admin/files?flash=🗑+মুছে+ফেলা+হয়েছে", status_code=303)

    # ---------------------------------------------------------------- orders
    @app.get("/admin/orders", response_class=HTMLResponse)
    async def admin_orders(request: Request, flash: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        rows = []
        for order in db.orders(limit=60):
            store_row = db.store(order["store_id"]) or {}
            user = db.user(order["user_id"]) or {}
            actions = ""
            if order["status"] == "pending":
                actions = (f"<a class='btn ok' href='/admin/orders/approve/{order['id']}'>✅</a> "
                           f"<a class='btn bad' href='/admin/orders/reject/{order['id']}'>❌</a>")
            rows.append(
                f"<tr><td>#{order['id']}</td><td>{esc(store_row.get('name') or '')}</td>"
                f"<td>{esc(user.get('name') or '')}<div class='muted'>{order['user_id']}</div></td>"
                f"<td>{esc(order['plan_name'] or '')}</td>"
                f"<td>{money(order['amount'] or 0, order['currency'] or '')}</td>"
                f"<td>{order['status']}<div class='muted'>{esc((order['proof'] or '')[:60])}</div></td>"
                f"<td>{_start(order['created_at'])}</td><td>{actions}</td></tr>")
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        return layout("Orders", f"""{note}<h3>🧾 অর্ডার</h3>
        <table><tr><th>#</th><th>Store</th><th>User</th><th>Plan</th><th>Amount</th><th>Status</th>
        <th>Created</th><th></th></tr>{''.join(rows)}</table>""", "/admin/orders")

    @app.get("/admin/orders/approve/{order_id}")
    async def admin_approve(request: Request, order_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        billing.approve_order(order_id, admin_id=0, note="approved from dashboard")
        return RedirectResponse(f"/admin/orders?flash=Approved+%23{order_id}", status_code=303)

    @app.get("/admin/orders/reject/{order_id}")
    async def admin_reject(request: Request, order_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        billing.reject_order(order_id, admin_id=0, note="rejected from dashboard")
        return RedirectResponse(f"/admin/orders?flash=Rejected+%23{order_id}", status_code=303)

    # ----------------------------------------------------------------- users
    @app.get("/admin/users", response_class=HTMLResponse)
    async def admin_users(request: Request, q: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        everyone = db.all_user_ids()
        if q:
            needle = q.strip().lower()
            everyone = [uid for uid in everyone
                        if needle in str(uid)
                        or needle in ((db.user(uid) or {}).get("name") or "").lower()]
        rows = []
        for uid in everyone[:300]:
            user = db.user(uid) or {}
            grants = db.user_grants(uid)
            badges = ", ".join(
                f"{esc(g['store_name'])} "
                f"{'♾' if g['expires_at'] is None else _start(g['expires_at'], '%d %b')}"
                for g in grants) or '<span class="muted">free</span>'
            box = (f'<input type="checkbox" name="ids" value="{uid}">')
            rows.append(f"<tr><td>{box} <a href='/admin/users/{uid}'>{uid}</a></td>"
                        f"<td>{esc(user.get('name') or '')}</td>"
                        f"<td>{esc(user.get('username') or '')}</td>"
                        f"<td>{_start(user.get('joined_at'))}</td><td>{badges}</td>"
                        f"<td><a class='btn bad' href='/admin/users/{uid}/delete"
                        f"?confirm=1'>🗑</a></td></tr>")
        return layout("Users", f"""<h3>👥 ইউজার ({len(everyone)})</h3>
        <form method=get><input name=q placeholder="id বা নাম" value="{esc(q)}"></form>
        {panels.users_tools_body(len(everyone))}
        <form method=post action="/admin/users/bulk-delete"
              onsubmit="return confirm('নির্বাচিত ইউজারদের সব ডেটা মুছে যাবে। চালিয়ে যাবেন?')">
        <table><tr><th></th><th>ID</th><th>Name</th><th>Username</th><th>Joined</th>
        <th>Access</th><th></th></tr>
        {''.join(rows)}</table>
        <div class="row" style="margin-top:10px">
          <button class="btn bad" type="submit">🗑 নির্বাচিত ইউজার মুছুন</button>
        </div></form>""", "/admin/users")

    @app.post("/admin/users/bulk-delete")
    async def admin_users_bulk_delete(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        form = await request.form()
        ids = [int(part) for part in re.findall(r"\d+", str(form.get("ids", "")))]
        admins = set(cfg.ADMIN_IDS)
        ids = [uid for uid in ids if uid not in admins]
        count = db.delete_users(ids)
        db.log_event("users_purged", user_id=0, detail=f"n={count}")
        return RedirectResponse(f"/admin/users?flash=🗑+{count}+জন+ইউজার+মুছে+ফেলা+হলো",
                                status_code=303)

    @app.get("/admin/users/purge", response_class=HTMLResponse)
    async def admin_users_purge_preview(request: Request, days: int = 90,
                                        never_started: int = 0):
        """🧹 পুরোনো ইউজার একসাথে পরিষ্কার — আগে দেখায় কতজন যাবে, তারপর মোছে।"""
        blocked = _guard(request)
        if blocked:
            return blocked
        ids = db.purge_user_ids(days=days, never_started=bool(never_started),
                                keep_admins=cfg.ADMIN_IDS)
        sample = "".join(
            f"<tr><td>{uid}</td><td>{esc((db.user(uid) or {}).get('name') or '')}</td>"
            f"<td>{_start((db.user(uid) or {}).get('last_seen'))}</td></tr>"
            for uid in ids[:100])
        body = f"""
        <h3>🧹 পুরোনো ইউজার পরিষ্কার</h3>
        <p class="muted">যারা অনেক দিন বট ব্যবহার করেননি (বা কখনো <code>/start</code> দেননি)
        — এখান থেকে একবারে মুছে ফেলা যায়। অ্যাডমিনরা কখনো মুছবে না।</p>
        <form method=get class="row">
          <label>কত দিন অ্যাকটিভ না <input name="days" value="{days}" size="5"></label>
          <label><input type="checkbox" name="never_started" value="1"
            {'checked' if never_started else ''}> শুধু যারা কখনো শুরুই করেনি</label>
          <button class="btn grey" type="submit">🔍 কতজন হবে দেখুন</button>
        </form>
        <p><b>{len(ids)}</b> জন ইউজার মুছে ফেলা হবে।</p>
        <form method=post action="/admin/users/purge"
              onsubmit="return confirm('{len(ids)} জন ইউজারের সব ডেটা মুছে যাবে। নিশ্চিত?')">
          <input type="hidden" name="days" value="{days}">
          <input type="hidden" name="never_started" value="{1 if never_started else 0}">
          <button class="btn bad" type="submit">🗑 {len(ids)} জন মুছে ফেলুন</button>
        </form>
        {f'<table><tr><th>ID</th><th>নাম</th><th>শেষ দেখা</th></tr>{sample}</table>' if ids else ''}
        """
        return layout("Purge users", body, "/admin/users")

    @app.post("/admin/users/purge")
    async def admin_users_purge(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        form = await request.form()
        days = int(str(form.get("days", "90")) or 90)
        never = str(form.get("never_started", "0")) == "1"
        ids = db.purge_user_ids(days=days, never_started=never, keep_admins=cfg.ADMIN_IDS)
        count = db.delete_users(ids)
        db.log_event("users_purged", user_id=0, detail=f"days={days} n={count}")
        return RedirectResponse(f"/admin/users?flash=🧹+{count}+জন+পুরোনো+ইউজার+মুছে+ফেলা+হলো",
                                status_code=303)

    @app.get("/admin/users/{user_id}/delete")
    async def admin_user_delete(request: Request, user_id: int, confirm: int = 0):
        blocked = _guard(request)
        if blocked:
            return blocked
        if user_id in set(cfg.ADMIN_IDS):
            return RedirectResponse("/admin/users?flash=অ্যাডমিনকে+মোছা+যাবে+না",
                                    status_code=303)
        ok = db.delete_user(user_id)
        label = "🗑+ইউজার+মুছে+ফেলা+হলো" if ok else "ইউজার+নেই"
        return RedirectResponse(f"/admin/users?flash={label}", status_code=303)

    # -------------------------------------------------------------- download
    @app.get("/admin/download", response_class=HTMLResponse)
    async def admin_download(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        size = os.path.getsize(cfg.DB_FILE) if os.path.exists(cfg.DB_FILE) else 0
        body = f"""
        <h3>📥 এক্সপোর্ট</h3>
        <div class="card">
          <p class="muted">DB: <code>{esc(cfg.DB_FILE)}</code> · {human_size(size)}</p>
          <p><a class="btn" href="/admin/download/db">🗄 SQLite</a>
             <a class="btn grey" href="/admin/download/json">📄 JSON</a></p>
          <p><a class="btn grey" href="/admin/download/csv/users">👥 Users CSV</a>
             <a class="btn grey" href="/admin/download/csv/orders">🧾 Orders CSV</a>
             <a class="btn grey" href="/admin/download/csv/files">🗂 Files CSV</a></p>
        </div>"""
        return layout("Export", body, "/admin/download")

    @app.get("/admin/download/db")
    async def download_db(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        if not os.path.exists(cfg.DB_FILE):
            return JSONResponse({"error": "DB file not found"}, status_code=404)
        return FileResponse(cfg.DB_FILE, filename="bot_data.sqlite3",
                            media_type="application/octet-stream")

    @app.get("/admin/download/json")
    async def download_json(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        import json
        import tempfile
        data = db.export_dict()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8")
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp.close()
        return FileResponse(tmp.name, filename=f"bot_data-{int(time.time())}.json",
                            media_type="application/json")

    def _csv_response(header: list[str], rows: list[list], filename: str) -> FileResponse:
        import csv
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w",
                                          encoding="utf-8", newline="")
        writer = csv.writer(tmp)
        writer.writerow(header)
        writer.writerows(rows)
        tmp.close()
        return FileResponse(tmp.name, filename=filename, media_type="text/csv")

    @app.get("/admin/download/csv/users")
    async def csv_users(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        rows = []
        for uid in db.all_user_ids():
            user = db.user(uid) or {}
            grants = ";".join(f"{g['store_name']}:{g['store_id']}" for g in db.user_grants(uid))
            rows.append([uid, user.get("name"), user.get("username"), user.get("joined_at"),
                         user.get("last_seen"), grants])
        return _csv_response(["user_id", "name", "username", "joined_at", "last_seen", "grants"],
                             rows, "users.csv")

    @app.get("/admin/download/csv/orders")
    async def csv_orders(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        rows = [[o["id"], o["user_id"], o["store_id"], o["plan_name"], o["days"], o["amount"],
                 o["currency"], o["method"], o["status"], o["created_at"], o.get("proof")]
                for o in db.orders(limit=10000)]
        return _csv_response(["id", "user_id", "store_id", "plan_name", "days", "amount",
                              "currency", "method", "status", "created_at", "proof"],
                             rows, "orders.csv")

    @app.get("/admin/download/csv/files")
    async def csv_files(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        rows = []
        for store in db.all_stores():
            for f in db.files_of(store["id"]):
                rows.append([f["id"], f["store_id"], store["name"], f["name"], f["kind"],
                             f["views"], f["created_at"]])
        return _csv_response(["id", "store_id", "store_name", "name", "kind", "views",
                              "created_at"], rows, "files.csv")

    # -------------------------------------------------------------- settings
    def _field(item, key_prefix: str = "") -> str:
        """One form row, rendered from the settings registry."""
        name = f"{key_prefix}{item.key}"
        value = settings.get(item.key)
        if item.kind == "bool":
            checked = " checked" if value else ""
            return (f'<label class="chk"><input type="checkbox" name="{name}" '
                    f'value="1"{checked}> {esc(item.label)}</label>')
        if item.kind == "long":
            inner = (f'<textarea name="{name}" rows="3" placeholder="{esc(item.placeholder)}">'
                     f'{esc(value)}</textarea>')
        else:
            shown = "" if value is None else value
            input_type = "number" if item.kind in ("int", "float") else "text"
            step = ' step="any"' if item.kind == "float" else ""
            inner = (f'<input type="{input_type}"{step} name="{name}" '
                     f'value="{esc(shown)}" placeholder="{esc(item.placeholder)}">')
        badge = {"db": "প্যানেল", "env": "config.env", "default": "ডিফল্ট"}.get(
            settings.source(item.key), "")
        hint = f'<div class="muted">{esc(item.hint)}</div>' if item.hint else ""
        return (f'<label>{esc(item.label)} <span class="pill">{badge}</span></label>'
                f'{inner}{hint}')

    @app.get("/admin/settings", response_class=HTMLResponse)
    async def admin_settings(request: Request, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        bot_ok = runtime.bot_online()
        groups_html = []
        for group in settings.all_groups():
            fields = "".join(_field(item) for item in group.settings)
            extra = ""
            if group.key == "force_join":
                link = forcejoin.join_url()
                resolved_id = db.get_meta("force_channel_id", "")
                extra = (f'<p class="muted">🔗 জয়েন লিংক: '
                         f'<code>{esc(link) or "—"}</code> · '
                         f'🆔 <code>{esc(resolved_id) or "— এখনো রেজলভ হয়নি —"}</code></p>'
                         f'<p class="muted">প্রাইভেট চ্যানেল হলে ইনভাইট লিংক '
                         f'(<code>t.me/+…</code>) দিন — বট সেটাও বুঝবে।</p>')
            groups_html.append(f"""
            <div class="card" style="margin-bottom:14px">
              <h3>{group.icon} {esc(group.label)}</h3>
              <p class="muted">{esc(group.blurb)}</p>
              <form method=post action="/admin/settings/save">
                <input type="hidden" name="__group" value="{group.key}">
                {fields}
                {extra}
                <div class="row" style="margin-top:10px">
                  <button class="ok" type="submit">💾 সেভ করুন</button>
                </div>
              </form>
            </div>""")
        body = f"""
        {note}{warnbox}
        <h3>⚙️ সেটিংস</h3>
        <p class="muted">সব কিছু এখান থেকেই বদলানো যায় — বট রিস্টার্ট বা ফাইল এডিট লাগে না।
        বটের ⚙️ Settings স্ক্রিনেও একই সেটিংস আছে। badge বলে দেয় মানটা কোথা থেকে আসছে
        (প্যানেল / config.env / ডিফল্ট); কিছু সেভ করলে সেটা environment মানকে ওভাররাইড করে।</p>
        <div class="grid">
          {card("Bot", f"@{_bot_username() or '—'}")}
          {card("Bot online", "✅" if bot_ok else "❌")}
          {card("Uptime", f"{int(time.time() - STARTED_AT)}s")}
          {card("DB size", human_size(os.path.getsize(cfg.DB_FILE) if os.path.exists(cfg.DB_FILE) else 0))}
        </div>
        <p class="row" style="margin:14px 0">
          <a class="btn grey" href="/admin/settings/export">⬇️ config.env হিসেবে এক্সপোর্ট</a>
          <a class="btn grey" href="/admin/download">📥 ডেটা এক্সপোর্ট</a>
        </p>
        {''.join(groups_html)}
        """
        return layout("Settings", body, "/admin/settings")

    @app.post("/admin/settings/save")
    async def admin_settings_save(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        form = await request.form()
        group_key = str(form.get("__group", "")).strip()
        group = next((g for g in settings.all_groups() if g.key == group_key), None)
        if group is None:
            return RedirectResponse("/admin/settings?warn=অজানা+গ্রুপ", status_code=303)
        values: dict[str, str] = {}
        for item in group.settings:
            raw = form.get(item.key)
            if item.kind == "bool":
                values[item.key] = "1" if raw is not None else "0"
            else:
                values[item.key] = "" if raw is None else str(raw)
        errors = settings.set_many(values)
        if group_key == "force_join":
            forcejoin.clear_cache()
            if not errors and settings.get_str("FORCE_CHANNEL"):
                settings.set("FORCE_JOIN_ENABLED", "1")
        if errors:
            joined = "+".join(f"{key}:{msg}" for key, msg in errors.items())
            return RedirectResponse(f"/admin/settings?warn={joined}", status_code=303)
        return RedirectResponse(
            f"/admin/settings?flash=✅+{group.label}+সেভ+হলো", status_code=303)

    @app.get("/admin/settings/forcejoin/check")
    async def admin_forcejoin_check(request: Request):
        """Resolve the channel now (the bot or a session has to be online)."""
        blocked = _guard(request)
        if blocked:
            return blocked
        result = await forcejoin.resolve(refresh=True, use_cache=False)
        if result.get("ok"):
            message = (f"✅+চ্যানেল+পাওয়া+গেছে:+{result['title']}+"
                       f"({result['chat_id']})")
        else:
            message = f"⚠️+চ্যানেল+পাওয়া+যায়নি:+{result.get('error') or '?'}"
        return RedirectResponse(f"/admin/settings?flash={message}", status_code=303)

    @app.get("/admin/settings/export")
    async def admin_settings_export(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        body = settings.env_preview()
        return Response(body, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition":
                                 'attachment; filename="config.env"'})

    # ----------------------------------------------------------------- health
    # ================================================= channels / composer (v3)
    @app.get("/admin/channels", response_class=HTMLResponse)
    async def admin_channels(request: Request, flash: str = "", warn: str = "",
                             refresh: int = 0):
        blocked = _guard(request)
        if blocked:
            return blocked
        if refresh:
            for row in db.join_channels():
                if not row.get("chat_id"):
                    info = await channels.add_channel(row["ref"], row.get("store_id"))
                    if info.get("chat_id"):
                        db.update_join_channel(row["id"], chat_id=info["chat_id"],
                                               title=info.get("title") or row.get("title"))
        body = panels.channels_body(flash, warn)
        return layout("Channels", body, "/admin/channels")

    @app.post("/admin/channels/add")
    async def admin_channels_add(request: Request, ref: str = Form(""),
                                 store_id: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        scope = int(store_id) if store_id.strip().isdigit() else None
        result = await channels.add_channel(ref, scope)
        if result.get("ok"):
            return RedirectResponse(
                f"/admin/channels?flash=✅+যোগ+হয়েছে:+{esc(result.get('title') or ref)}",
                status_code=303)
        return RedirectResponse(
            f"/admin/channels?warn=চ্যানেল+পাওয়া+গেল+ন:+{esc(result.get('error') or '')}"[:400],
            status_code=303)

    @app.get("/admin/channels/toggle/{channel_id}")
    async def admin_channels_toggle(request: Request, channel_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        row = db.join_channel(channel_id)
        if row:
            db.update_join_channel(channel_id, enabled=0 if row.get("enabled") else 1)
        return RedirectResponse("/admin/channels?flash=✅+আপডেট", status_code=303)

    @app.get("/admin/channels/check/{channel_id}")
    async def admin_channels_check(request: Request, channel_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        row = db.join_channel(channel_id)
        if row is None:
            return RedirectResponse("/admin/channels?warn=চ্যানেল+নেই", status_code=303)
        info = await channels.channel_info(row.get("chat_id") or row["ref"])
        if info.get("ok"):
            db.update_join_channel(channel_id, chat_id=info["chat_id"],
                                   title=info.get("title") or row.get("title"),
                                   last_check=time.time(), error="")
            if info.get("chat_id"):
                db.set_meta(f"chat_title:{info['chat_id']}", info.get("title") or "")
            return RedirectResponse(
                f"/admin/channels?flash=✅+{info.get('title')}+·+{info.get('members')}+সদস্য",
                status_code=303)
        db.update_join_channel(channel_id, last_check=time.time(),
                               error=str(info.get("error") or "")[:180])
        return RedirectResponse(
            f"/admin/channels?warn={esc(str(info.get('error') or 'পাওয়া যায়নি'))}"[:400],
            status_code=303)

    @app.get("/admin/channels/delete/{channel_id}")
    async def admin_channels_delete(request: Request, channel_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.delete_join_channel(channel_id)
        return RedirectResponse("/admin/channels?flash=🗑+মুছে+ফেলা+হলো", status_code=303)

    @app.get("/admin/channels/compose", response_class=HTMLResponse)
    async def admin_compose(request: Request, store: int = 0, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        composer = channels.composer_defaults(store or None)
        return layout("Channel post", panels.compose_body(composer, store or None, flash, warn),
                      "/admin/channels")

    @app.post("/admin/channels/compose")
    async def admin_compose_post(request: Request, target: str = Form(""),
                                 text: str = Form(""), buttons: str = Form(""),
                                 store_id: str = Form(""), file_ids: list[str] = Form(default=[]),
                                 send_test: str = Form(""), link_limit: int = Form(-1)):
        blocked = _guard(request)
        if blocked:
            return blocked
        ids = [int(f) for f in file_ids if str(f).strip().isdigit()]
        scope = int(store_id) if store_id.strip().isdigit() else None
        composer = {"text": text, "buttons": buttons, "store_id": scope}
        if send_test:
            ok = await broadcast.test_send_text(text or "(ফাইল পোস্ট)", buttons or "")
            return layout("Channel post",
                          panels.compose_body(composer, scope,
                                              "🧪 টেস্ট পাঠানো হয়েছে" if ok
                                              else "", "" if ok else "টেস্ট পাঠানো যায়নি"),
                          "/admin/channels")
        if not target.strip():
            return layout("Channel post",
                          panels.compose_body(composer, scope, "",
                                              "চ্যানেল বাছুন বা আগে একটা যোগ করুন"),
                          "/admin/channels")
        result = await channels.publish(target, text=text, file_ids=ids, buttons=buttons,
                                       store_id=scope, link_limit=link_limit)
        if result.get("ok"):
            return layout("Channel post",
                          panels.compose_body(composer, scope,
                                              f"✅ পাঠানো হয়েছে (মেসেজ #{result.get('message_id')})",
                                              ""),
                          "/admin/channels")
        return layout("Channel post",
                      panels.compose_body(composer, scope, "",
                                          f"❌ {result.get('error') or 'পাঠানো যায়নি'}"),
                      "/admin/channels")

    # ============================================================ analytics (v3)
    @app.get("/admin/texts", response_class=HTMLResponse)
    async def admin_texts(request: Request, flash: str = "", warn: str = "",
                          lang: str = "bn", q: str = ""):
        """📝 বটের মেসেজ — change any message the bot sends, from the website."""
        blocked = _guard(request)
        if blocked:
            return blocked
        lang = lang if lang in ("bn", "en") else "bn"
        needle = q.strip().lower()
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
        blocks = []
        edited_total = 0
        for group in bot_texts.catalog(lang):
            cards = []
            for row in group["rows"]:
                if needle and needle not in row["key"].lower() \
                        and needle not in row["label"].lower():
                    continue
                if row["edited"]:
                    edited_total += 1
                badge = ('<span class="badge">✏️ এডিট করা</span>' if row["edited"]
                         else '<span class="badge">ডিফল্ট</span>')
                default = esc(row["default"]) or '<span class="muted">(খালি)</span>'
                value = esc(row["value"])
                cards.append(f"""
                <div class="card" style="margin-bottom:12px">
                  <b>{esc(row['label'])}</b> {badge}
                  <p class="muted">কী: <code>{esc(row['key'])}</code></p>
                  <p class="muted">এখন যা যায়:</p>
                  <pre style="white-space:pre-wrap">{esc(row['value'] or row['default'])}</pre>
                  <form method=post action="/admin/texts/save">
                    <input type="hidden" name="key" value="{esc(row['key'])}">
                    <input type="hidden" name="lang" value="{lang}">
                    <textarea name="value" rows="4" placeholder="নতুন লেখা… (খালি রাখলে ডিফল্ট ফিরে আসবে)">{value}</textarea>
                    <p class="muted">ডিফল্ট: {default}</p>
                    <div class="row">
                      <button class="ok" type="submit">💾 সেভ</button>
                      <button class="grey" type="submit" name="value" value="-">♻️ ডিফল্ট ফিরিয়ে দিন</button>
                    </div>
                  </form>
                </div>""")
            if cards:
                blocks.append(f"<h3>{esc(group['group'])}</h3>" + "".join(cards))
        body = f"""
        {note}{warnbox}
        <h3>📝 বটের মেসেজ</h3>
        <p class="muted">বট ইউজারকে যা যা লেখে, সব এখান থেকে বদলানো যায় — কোড এডিট বা
        রিস্টার্ট লাগে না। <code>{{store}}</code>, <code>{{n}}</code> এর মতো
        প্লেসহোল্ডার আগের মতোই কাজ করবে। খালি রেখে সেভ করলে (বা ♻️ চাপলে) ডিফল্ট
        লেখা ফিরে আসে।</p>
        <p class="row">
          <a class="btn {'ok' if lang == 'bn' else 'grey'}" href="/admin/texts?lang=bn">🇧🇩 বাংলা</a>
          <a class="btn {'ok' if lang == 'en' else 'grey'}" href="/admin/texts?lang=en">🇬🇧 English</a>
          <a class="btn grey" href="/admin/settings">🛠 সেটিংস</a>
        </p>
        <form method=get action="/admin/texts" class="row">
          <input type="hidden" name="lang" value="{lang}">
          <input name="q" value="{esc(q)}" placeholder="🔍 মেসেজ খুঁজুন (কী বা নাম)">
          <button class="btn grey" type="submit">খুঁজুন</button>
        </form>
        <p class="muted">{edited_total} টি মেসেজ এখন নিজের লেখায় চলছে।</p>
        {''.join(blocks) or '<p class="muted">কিছু পাওয়া গেল না।</p>'}
        """
        return layout("Bot texts", body, "/admin/texts")

    @app.post("/admin/texts/save")
    async def admin_texts_save(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        form = await request.form()
        key = str(form.get("key", "")).strip()
        lang = str(form.get("lang", "bn")).strip() or "bn"
        value = str(form.get("value", ""))
        ok, error = bot_texts.set(key, value, lang)
        if not ok:
            return RedirectResponse(f"/admin/texts?lang={lang}&warn={esc(error)}",
                                    status_code=303)
        label = "♻️ ডিফল্ট ফিরে এসেছে" if value.strip() in ("", "-") else "✅ সেভ হয়েছে"
        return RedirectResponse(f"/admin/texts?lang={lang}&flash={esc(label)}",
                                status_code=303)

    @app.get("/admin/stores/{store_id}", response_class=HTMLResponse)
    async def admin_store_detail(request: Request, store_id: int, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        store = db.store(store_id)
        if store is None:
            return RedirectResponse("/admin/stores?warn=স্টোর+নেই", status_code=303)
        return layout(store["name"], panels.store_admin_body(store, flash, warn),
                      "/admin/stores")

    @app.get("/admin/analytics", response_class=HTMLResponse)
    async def admin_analytics(request: Request, days: int = 14):
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout("Analytics", panels.analytics_body(days), "/admin/analytics")

    @app.get("/admin/analytics/store/{store_id}", response_class=HTMLResponse)
    async def admin_store_analytics(request: Request, store_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout("Store analytics", panels.store_analytics_body(store_id),
                      "/admin/analytics")

    @app.get("/admin/analytics/file/{file_id}", response_class=HTMLResponse)
    async def admin_file_watchers(request: Request, file_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout("File watchers", panels.file_watchers_body(file_id), "/admin/analytics")

    @app.get("/admin/users/{user_id}", response_class=HTMLResponse)
    async def admin_user_detail(request: Request, user_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout(f"User {user_id}", panels.user_detail_body(user_id), "/admin/users")

    @app.post("/admin/users/{user_id}/message")
    async def admin_user_message(request: Request, user_id: int, text: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        sent = await broadcast.test_send_text(text, "")
        return RedirectResponse(
            f"/admin/users/{user_id}?flash=" + ("✅+পাঠানো+হয়েছে" if sent else "❌+পাঠানো+যায়নি"),
            status_code=303)

    @app.get("/admin/users/{user_id}/ban")
    async def admin_user_ban(request: Request, user_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.ban_user(user_id, reason="banned from the website panel")
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    @app.get("/admin/users/{user_id}/unban")
    async def admin_user_unban(request: Request, user_id: int):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.unban_user(user_id)
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    # ======================================================= limited links (v3)
    @app.get("/admin/links", response_class=HTMLResponse)
    async def admin_links(request: Request, flash: str = "", warn: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        return layout("Links", panels.links_body(flash, warn), "/admin/links")

    @app.post("/admin/links/new")
    async def admin_links_new(request: Request, file_ids: list[str] = Form(default=[]),
                              store_id: str = Form(""), max_clicks: int = Form(0),
                              note: str = Form(""), per_user: int = Form(0)):
        blocked = _guard(request)
        if blocked:
            return blocked
        ids = [int(f) for f in file_ids if str(f).strip().isdigit()]
        if not ids:
            return RedirectResponse("/admin/links?warn=ফাইল+বাছুন", status_code=303)
        token = db.create_link(0, ids, None, kind="limited", max_clicks=max(0, max_clicks),
                               note=note, per_user_limit=max(0, per_user))
        label = f"{max_clicks} ক্লিক" if max_clicks else "আনলিমিটেড"
        if per_user:
            label += f" · প্রতি ইউজার {per_user}"
        return RedirectResponse(
            f"/admin/links?flash=🔗+তৈরি:+{label}+·+{token}", status_code=303)

    @app.get("/admin/links/delete/{token}")
    async def admin_links_delete(request: Request, token: str):
        blocked = _guard(request)
        if blocked:
            return blocked
        db.delete_link(token)
        return RedirectResponse("/admin/links?flash=🗑+মুছে+ফেলা+হলো", status_code=303)

    @app.post("/admin/stores/{store_id}/forcejoin")
    async def admin_store_forcejoin(request: Request, store_id: int, ref: str = Form("")):
        blocked = _guard(request)
        if blocked:
            return blocked
        if ref.strip().lower() in ("off", "none", "-", "বন্ধ", "no"):
            ref = ""
        cleaned = forcejoin.normalize(ref) if ref.strip() else ""
        db.set_store_forcejoin(store_id, cleaned)
        forcejoin.clear_cache()
        if cleaned:
            info = await channels.add_channel(cleaned, store_id)
            note = "✅ সেভ হয়েছে" + (f" · {info.get('title')}" if info.get("ok") else
                                    " (চ্যানেল এখনো পাওয়া যায়নি)")
        else:
            note = "✅ এই স্টোরের আলাদা চ্যানেল বন্ধ করা হলো"
        return RedirectResponse(f"/admin/stores?flash={note}", status_code=303)

    @app.get("/admin/digest")
    async def admin_digest_run(request: Request):
        """🧪 Switch the weekly digest on and send it right now (preview first)."""
        blocked = _guard(request)
        if blocked:
            return blocked
        from app.services.scheduler import run_digest
        result = await run_digest(force=True)
        queued = result.get("queued") or []
        if not queued:
            return RedirectResponse(
                "/admin/broadcast?warn=এই+সপ্তাহে+নতুন+ফাইল+নেই+(বা+কেউ+নেই)",
                status_code=303)
        total = sum(item["targets"] for item in queued)
        return RedirectResponse(
            f"/admin/broadcast?flash=🆕+ডাইজেস্ট+কিউ+হয়েছে:+{len(queued)}+স্টোর,+{total}+ইউজার",
            status_code=303)

    # ==================================================== tools: test / logs (v3)
    @app.get("/admin/security", response_class=HTMLResponse)
    async def admin_security(request: Request, flash: str = ""):
        """🔐 টোকেন/সিক্রেট কোথাও পাবলিকভাবে পড়া যায় কি না — আর ঠিক করার ধাপে ধাপে।"""
        blocked = _guard(request)
        if blocked:
            return blocked
        report = secrets_guard.audit()
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        files = report.get("repo_files") or []
        rows = "".join(
            f"<tr><td><code>{esc(item['file'])}</code></td><td>{item['line']}</td>"
            f"<td><code>{esc(item['preview'])}</code></td>"
            f"<td>{'🚨 গত কমিটেও আছে' if item.get('committed') else '⚠️ ফাইলে আছে'}</td></tr>"
            for item in files)
        env_names = ", ".join(report.get("secrets") or []) or "— কিছু নেই —"
        private_names = ", ".join(report.get("private") or []) or "— কিছু নেই —"
        env_keys = ", ".join(report.get("in_environment") or []) or "— কিছু নেই —"
        clean = not files and not report.get("tracked")
        box = ('<div class="flash">✅ দারুণ — রিপোতে সরাসরি কোনো টোকেন/সিক্রেট পাওয়া যায়নি, '
               'আর config.env কমিটও হয় না।</div>' if clean else
               '<div class="warnbox">🚨 নিচের বিষয়গুলো এখনই ঠিক করা দরকার।</div>')
        history = ("⚠️ হ্যাঁ — পুরোনো কমিটে config.env-এর মান এখনো পড়া যায়"
                   if report.get("in_history") else "না")
        body = f"""
        {note}{box}
        <h3>🔐 নিরাপত্তা (টোকেন ও সিক্রেট)</h3>
        <p class="muted">রিপো পাবলিক (GitHub) — তাই <code>config.env</code> কখনো কমিট করা যায় না।
        টোকেন শুধু হোস্টিং প্যানেলের <b>Environment Variables</b> বা ডিস্কের
        <code>config.env</code>-এ থাকবে (ফাইলটি <code>.gitignore</code>-এ আছে)।</p>
        <div class="grid">
          {card("config.env কমিট হয়েছে?", "🚨 হ্যাঁ" if report.get("tracked") else "✅ না")}
          {card("গিট ইতিহাসে পড়া যায়?", history)}
          {card("env-এ থাকা গোপন মান", esc(env_names))}
          {card("সার্ভারে সেট করা env", esc(env_keys))}
        </div>
        <p class="muted">ব্যক্তিগত (কম গোপন, তবু পাবলিক না করা ভালো): {esc(private_names)}</p>
        <h4>🚨 রিপোর ফাইলে সরাসরি লেখা সিক্রেট</h4>
        {f'<table><tr><th>ফাইল</th><th>লাইন</th><th>মান (লুকানো)</th><th>অবস্থা</th></tr>'
          f'{rows}</table>' if files else '<p class="muted">কিছু পাওয়া যায়নি।</p>'}
        <h4>✅ ঠিক করার ধাপ</h4>
        <ol>
          <li>BotFather → <code>/mybots</code> → আপনার বট → API Token → <b>Revoke</b>,
              তারপর নতুন টোকেন কপি করুন।</li>
          <li>পুরোনো টোকেন/হ্যাশ রিপো থেকে <b>মুছে ফেলুন</b> (উপরের তালিকার ফাইল), সেভ করে কমিট করুন।</li>
          <li>Telegram → Settings → Devices → অচেনা সেশন <b>Terminate</b>।</li>
          <li>নতুন টোকেন হোস্টিং প্যানেল → Environment Variables-এ বসান
              (<code>BOT_TOKEN</code>, <code>API_HASH</code>, <code>API_ID</code>),
              ডিস্কের <code>config.env</code>-এও রাখতে পারেন — কিন্তু কমিট করবেন না।</li>
          <li>রিস্টার্ট দিন — বট নতুন টোকেন নিয়েই চলবে।</li>
        </ol>
        <p class="row">
          <a class="btn grey" href="/admin/security">🔄 আবার পরীক্ষা করুন</a>
          <a class="btn grey" href="/admin/self-test">🩺 Health</a>
        </p>
        """
        return layout("Security", body, "/admin/security")

    @app.get("/admin/self-test", response_class=HTMLResponse)
    async def admin_self_test(request: Request, flash: str = ""):
        blocked = _guard(request)
        if blocked:
            return blocked
        checks = await panels.self_test()
        note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
        return layout("Self test", note + panels.self_test_body(checks), "/admin/self-test")

    @app.get("/admin/logs", response_class=HTMLResponse)
    async def admin_logs(request: Request, level: str = "", lines: int = 300):
        blocked = _guard(request)
        if blocked:
            return blocked
        content: list[str] = []
        try:
            from pathlib import Path
            path = Path(cfg.LOG_FILE)
            if path.exists():
                raw = path.read_text(errors="ignore").splitlines()[-max(50, min(lines, 2000)):]
                if level:
                    raw = [line for line in raw if level.upper() in line.upper()]
                content = raw
        except Exception as exc:
            content = [f"লগ পড়া যায়নি: {exc}"]
        return layout("Logs", panels.logs_body(content, level), "/admin/settings")

    @app.get("/admin/cache/warm")
    async def admin_cache_warm(request: Request, limit: int = 15):
        blocked = _guard(request)
        if blocked:
            return blocked
        from app.services.media_cache import warm_mirrors
        result = await warm_mirrors(limit=limit)
        return RedirectResponse(
            f"/admin/self-test?flash=ক্যাশ:+{result['mirrored']}+হয়েছে,+{result['missing']}+বাকি",
            status_code=303)

    @app.get("/admin/files/dedupe")
    async def admin_files_dedupe(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        removed = db.purge_duplicate_files()
        return RedirectResponse(f"/admin/files?flash=🧹+{removed}+টি+ডুপ্লিকেট+মুছে+ফেলা+হলো",
                                status_code=303)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        """Who am I, where am I listening, and is the bot online?

        Hosting panels and the live-URL gateway poll this, so it also reports the
        port we bound and the prefix we are served under (`/live/<job>`), which is
        exactly what to check when a panel says “no web listener yet”.
        """
        stats = db.stats()
        return JSONResponse({
            "ok": True,
            "uptime_seconds": int(time.time() - STARTED_AT),
            "bot_online": runtime.bot_online(),
            "bot_username": _bot_username(),
            "users": stats["users"],
            "files": stats["files"],
            "stores": stats["stores"],
            "pending_orders": stats["orders_pending"],
            "time": int(time.time()),
            "host": cfg.WEB_HOST,
            "port": cfg.WEB_PORT,
            "from_port_env": cfg.HOSTED,
            "url_prefix": basepath.current() or basepath.normalize(
                request.headers.get("x-forwarded-prefix") or ""),
            "web_enabled": cfg.WEB_ENABLED,
        })

    return app


app = create_app()


def main() -> int:
    setup_logging()
    db.bind(cfg.DB_FILE)
    db.migrate_legacy(cfg.LEGACY_DB_FILE)
    if not cfg.WEB_PASS:
        log.warning("WEB_PASS is not set — a temporary password was generated (see above).")
    import uvicorn
    log.info("Website on http://%s:%s (admin user %s)", cfg.WEB_HOST, cfg.WEB_PORT, cfg.WEB_USER)
    uvicorn.run(app, host=cfg.WEB_HOST, port=cfg.WEB_PORT, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

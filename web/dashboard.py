"""Optional web dashboard (FastAPI) for the store bot.

It talks to the *same* SQLite file as the bot, so you can manage everything from
a browser on the hosting panel:

    pip install -r requirements-web.txt
    WEB_ENABLED=true WEB_USER=admin WEB_PASS=strongpassword \\
    .venv/bin/python -m web.dashboard

Open  http://your-server:8080/  and log in with the basic-auth credentials.

Pages
    /                 dashboard: users, files, views, revenue, alerts
    /stores           all stores with sales/drip status
    /orders           payment desk — approve / reject
    /users?q=         user list + access
    /files?q=         search every file, delete a bad entry
    /broadcast        send a message to a filter (all/premium/free)
    /health           JSON health check (for uptime monitors)
"""
from __future__ import annotations

import base64
import hmac
import html
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Form, Request                        # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402

from app import config as cfg                                       # noqa: E402
from app.logger import log, setup_logging                           # noqa: E402
from app.services import access, billing                            # noqa: E402
from app.storage import db                                          # noqa: E402
from app.utils import esc, fmt_ts, money                            # noqa: E402

app = FastAPI(title="Store Bot Dashboard", docs_url=None, redoc_url=None)
STARTED_AT = time.time()


# --------------------------------------------------------------------- auth
def _check_auth(header: str | None) -> bool:
    if not cfg.WEB_PASS:
        return False
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        user, _, password = decoded.partition(":")
    except Exception:
        return False
    return (hmac.compare_digest(user, cfg.WEB_USER)
            and hmac.compare_digest(password, cfg.WEB_PASS))


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if request.url.path in ("/health",):
        return await call_next(request)
    if not _check_auth(request.headers.get("Authorization")):
        return HTMLResponse(
            "<h3>🔒 Login required</h3><p>Set WEB_USER and WEB_PASS in config.env.</p>",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="store-bot"'},
        )
    return await call_next(request)


# -------------------------------------------------------------------- layout
def layout(title: str, body: str, active: str = "") -> str:
    nav_items = [
        ("/", "📊 Dashboard"),
        ("/stores", "🏪 Stores"),
        ("/orders", "🧾 Orders"),
        ("/users", "👥 Users"),
        ("/files", "🗂 Files"),
        ("/broadcast", "📢 Broadcast"),
        ("/download", "📥 Download"),
    ]
    nav = "".join(
        f'<a class="{"on" if href == active else ""}" href="{href}">{label}</a>'
        for href, label in nav_items
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · Store Bot</title>
<style>
 :root {{ color-scheme: dark; }}
 body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
        margin: 0; background: #10131a; color: #e7ecf3; }}
 header {{ background: #171b26; padding: 14px 20px; display: flex;
           justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }}
 header h1 {{ font-size: 17px; margin: 0; }}
 nav a {{ color: #9fb0c7; text-decoration: none; margin-right: 14px; font-size: 14px; }}
 nav a.on {{ color: #6ee7b7; font-weight: 600; }}
 main {{ padding: 20px; max-width: 1100px; }}
 .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }}
 .card {{ background: #171b26; border: 1px solid #242b3a; border-radius: 12px; padding: 14px; }}
 .card .n {{ font-size: 26px; font-weight: 700; margin-top: 6px; }}
 .card .l {{ color: #93a3b8; font-size: 13px; }}
 table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 14px; }}
 th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #232a38; }}
 th {{ color: #93a3b8; font-weight: 600; font-size: 12px; text-transform: uppercase; }}
 a.btn, button {{ background: #2563eb; color: white; border: 0; padding: 7px 12px;
                  border-radius: 8px; text-decoration: none; font-size: 13px; cursor: pointer; }}
 a.ok {{ background: #059669; }} a.bad {{ background: #b91c1c; }} a.grey {{ background: #374151; }}
 input, textarea, select {{ background: #0d1017; color: #e7ecf3; border: 1px solid #2b3346;
                  border-radius: 8px; padding: 9px 11px; width: 100%; margin: 6px 0; }}
 .muted {{ color: #93a3b8; font-size: 13px; }} .pill {{ background:#1f2937; padding:2px 8px; border-radius:999px; font-size:12px; }}
 .flash {{ background: #064e3b; padding: 10px 14px; border-radius: 10px; margin-bottom: 14px; }}
</style></head><body>
<header>
  <h1>🎬 Store Bot Dashboard <span class="pill">@{cfg.WEB_USER}</span></h1>
  <nav>{nav}</nav>
</header>
<main>{body}</main>
</body></html>"""


def card(label: str, value) -> str:
    return f'<div class="card"><div class="l">{html.escape(label)}</div><div class="n">{value}</div></div>'


# --------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    stats = db.stats()
    revenue = db.revenue()
    body = f"""
    <div class="grid">
      {card("Users", stats['users'])}
      {card("Files", stats['files'])}
      {card("Views", stats['views'])}
      {card("Stores", stats['stores'])}
      {card("Granted users", stats['grants'])}
      {card("Revenue", money(revenue['total'], cfg.CURRENCY))}
      {card("Pending orders", revenue['pending'])}
      {card("Open tickets", stats['tickets_open'])}
      {card("Active links", stats['links'])}
    </div>
    <h3>Top files</h3>
    <table><tr><th>File</th><th>Store</th><th>Views</th></tr>
    {''.join(f"<tr><td>{esc(row['name'])}</td><td>{esc(row['store_name'] or '')}</td>"
             f"<td>{row['views']}</td></tr>" for row in db.top_files(8))}
    </table>
    <p class="muted">Uptime: {int(time.time() - STARTED_AT)}s · DB: <code>{esc(cfg.DB_FILE)}</code></p>
    """
    return layout("Dashboard", body, "/")


@app.get("/stores", response_class=HTMLResponse)
async def stores() -> str:
    rows = []
    for store in db.all_stores():
        stats = db.store_stats(store["id"])
        sales = billing.store_sales(store["id"])
        drip = db.drip(store["id"])
        lock = "🔒 premium" if store["is_premium"] else "🆓 free"
        drip_txt = f"{drip['count']}/day {drip['send_time']}" if drip and drip["enabled"] else "off"
        rows.append(
            f"<tr><td><b>{esc(store['name'])}</b><div class='muted'>#{store['id']} · {lock}</div></td>"
            f"<td>{stats['files']}</td><td>{stats['views']}</td><td>{stats['grants']}</td>"
            f"<td>{money(sales['revenue'], cfg.CURRENCY)}</td>"
            f"<td>{len(db.plans(store['id'], only_active=True))}</td><td>{drip_txt}</td></tr>"
        )
    return layout("Stores", f"""
      <table><tr><th>Store</th><th>Files</th><th>Views</th><th>Granted</th>
      <th>Revenue</th><th>Plans</th><th>Drip</th></tr>{''.join(rows)}</table>
    """, "/stores")


@app.get("/orders", response_class=HTMLResponse)
async def orders(flash: str = "") -> str:
    rows = []
    for order in db.orders(limit=50):
        store = db.store(order["store_id"]) or {}
        user = db.user(order["user_id"]) or {}
        actions = ""
        if order["status"] == "pending":
            actions = (f"<a class='btn ok' href='/orders/approve/{order['id']}'>✅ Approve</a> "
                       f"<a class='btn bad' href='/orders/reject/{order['id']}'>❌ Reject</a>")
        proof = esc((order["proof"] or "")[:60])
        rows.append(
            f"<tr><td>#{order['id']}</td><td>{esc(store.get('name') or '')}</td>"
            f"<td>{esc(user.get('name') or '')}<div class='muted'>{order['user_id']}</div></td>"
            f"<td>{esc(order['plan_name'] or '')}</td>"
            f"<td>{money(order['amount'] or 0, order['currency'] or '')}</td>"
            f"<td>{order['status']}<div class='muted'>{proof}</div></td>"
            f"<td>{fmt_ts(order['created_at'], '%m-%d %H:%M')}</td><td>{actions}</td></tr>"
        )
    note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return layout("Orders", f"""{note}
      <table><tr><th>#</th><th>Store</th><th>User</th><th>Plan</th><th>Amount</th>
      <th>Status</th><th>Created</th><th></th></tr>{''.join(rows)}</table>
    """, "/orders")


@app.get("/orders/approve/{order_id}")
async def approve(order_id: int):
    order = billing.approve_order(order_id, admin_id=0, note="approved from dashboard")
    name = (db.store(order["store_id"]) or {}).get("name", "") if order else ""
    return RedirectResponse(f"/orders?flash=Approved+%23{order_id}+({name})", status_code=303)


@app.get("/orders/reject/{order_id}")
async def reject(order_id: int):
    billing.reject_order(order_id, admin_id=0, note="rejected from dashboard")
    return RedirectResponse(f"/orders?flash=Rejected+%23{order_id}", status_code=303)


@app.get("/users", response_class=HTMLResponse)
async def users(q: str = "") -> str:
    everyone = db.all_user_ids()
    if q:
        needle = q.strip().lower()
        everyone = [uid for uid in everyone
                    if needle in str(uid)
                    or needle in ((db.user(uid) or {}).get("name") or "").lower()]
    rows = []
    for uid in everyone[:200]:
        user = db.user(uid) or {}
        grants = db.user_grants(uid)
        badges = ", ".join(
            f"{esc(g['store_name'])} {'♾' if g['expires_at'] is None else fmt_ts(g['expires_at'])}"
            for g in grants) or '<span class="muted">free</span>'
        rows.append(
            f"<tr><td>{uid}</td><td>{esc(user.get('name') or '')}</td>"
            f"<td>{fmt_ts(user.get('joined_at'))}</td><td>{badges}</td></tr>"
        )
    return layout("Users", f"""
      <form method="get"><input name="q" placeholder="search id or name" value="{esc(q)}"></form>
      <table><tr><th>ID</th><th>Name</th><th>Joined</th><th>Access</th></tr>{''.join(rows)}</table>
    """, "/users")


@app.get("/files", response_class=HTMLResponse)
async def files(q: str = "") -> str:
    if q:
        matches = db.search_all_stores(q, 200)
    else:
        matches = db.newest_files(200)
    rows = []
    for row in matches:
        store = db.store(row["store_id"]) or {}
        rows.append(
            f"<tr><td>{row['id']}</td><td>{esc(row['name'])}</td><td>{esc(store.get('name') or '')}</td>"
            f"<td>{esc(row['kind'])}</td><td>{row['views']}</td>"
            f"<td><a class='btn bad' href='/files/delete/{row['id']}'>🗑</a></td></tr>"
        )
    return layout("Files", f"""
      <form method="get"><input name="q" placeholder="search every store" value="{esc(q)}"></form>
      <table><tr><th>ID</th><th>Name</th><th>Store</th><th>Type</th><th>Views</th><th></th></tr>
      {''.join(rows)}</table>
    """, "/files")


@app.get("/files/delete/{file_id}")
async def delete_file(file_id: int):
    db.delete_file(file_id)
    return RedirectResponse("/files?q=", status_code=303)


@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_form(sent: str = "") -> str:
    note = f'<div class="flash">Broadcast finished: {esc(sent)}</div>' if sent else ""
    options = "".join(f'<option value="{store["id"]}">🏪 {esc(store["name"])}</option>'
                      for store in db.all_stores())
    return layout("Broadcast", f"""{note}
      <div class="card">
        <form method="post" action="/broadcast">
          <label class="muted">Audience</label>
          <select name="audience">
            <option value="all">📣 All users</option>
            <option value="premium">💎 Premium users</option>
            <option value="free">🆓 Free users</option>
            {options}
          </select>
          <label class="muted">Message (HTML allowed)</label>
          <textarea name="text" rows="6" placeholder="আজকের নতুন ড্রপ..."></textarea>
          <button type="submit">📢 Send</button>
        </form>
        <p class="muted">Messages are sent from your bot, paced to respect Telegram
        rate limits. Users who blocked the bot are skipped automatically.</p>
      </div>
    """, "/broadcast")


@app.post("/broadcast", response_class=HTMLResponse)
async def broadcast_send(audience: str = Form("all"), text: str = Form("")):
    if not text.strip():
        return RedirectResponse("/broadcast", status_code=303)
    from app.services.broadcast import run_broadcast
    from app.runtime import get_client

    if audience.startswith("store:"):
        filter_value = audience
    elif audience.isdigit():
        filter_value = f"store:{audience}"
    else:
        filter_value = audience

    admin_id = cfg.ADMIN_IDS[0] if cfg.ADMIN_IDS else 0
    targets = access.broadcast_targets(admin_id, filter_value)
    targets = [uid for uid in targets if uid not in cfg.ADMIN_IDS]

    client = get_client()
    if client is None:
        return RedirectResponse("/broadcast?sent=bot+is+not+running+in+this+process",
                                status_code=303)
    result = await run_broadcast(targets, text.strip(), None, None, None)
    return RedirectResponse(
        f"/broadcast?sent={result['sent']}+sent,+{result['failed']}+failed", status_code=303)


@app.get("/download", response_class=HTMLResponse)
async def download_page() -> str:
    import os
    db_path = cfg.DB_FILE
    size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    size_mb = f"{size / (1024*1024):.2f} MB" if size else "unknown"
    return layout("Download", f"""
      <div class="card">
        <h3>📥 Database download</h3>
        <p class="muted">DB path: <code>{esc(db_path)}</code> · Size: {size_mb}</p>
        <p>
          <a class="btn" href="/download/db">📥 Download SQLite (.sqlite3)</a>
          <a class="btn grey" href="/download/json">📄 Download JSON export</a>
        </p>
        <p class="muted">JSON export is human-readable and contains stores, files, grants, etc. — same as old bot_db.json plus extras.</p>
        <h4>CSV exports</h4>
        <p>
          <a class="btn grey" href="/download/csv/users">👥 Users CSV</a>
          <a class="btn grey" href="/download/csv/orders">🧾 Orders CSV</a>
          <a class="btn grey" href="/download/csv/files">🗂 Files CSV</a>
        </p>
      </div>
    """, "/download")


@app.get("/download/db")
async def download_db():
    path = cfg.DB_FILE
    if not os.path.exists(path):
        return JSONResponse({"error": "DB file not found"}, status_code=404)
    return FileResponse(path, filename="bot_data.sqlite3", media_type="application/octet-stream")


@app.get("/download/json")
async def download_json():
    import json, tempfile
    data = db.export_dict()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8")
    json.dump(data, tmp, ensure_ascii=False, indent=2)
    tmp.close()
    return FileResponse(tmp.name, filename=f"bot_data-{int(time.time())}.json", media_type="application/json")


@app.get("/download/csv/users")
async def download_users_csv():
    import csv, tempfile, os
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w", encoding="utf-8", newline="")
    writer = csv.writer(tmp)
    writer.writerow(["user_id", "name", "username", "joined_at", "last_seen", "grants"])
    for uid in db.all_user_ids():
        user = db.user(uid) or {}
        grants = ";".join(f"{g['store_name']}:{g['store_id']}" for g in db.user_grants(uid))
        writer.writerow([uid, user.get("name"), user.get("username"), user.get("joined_at"), user.get("last_seen"), grants])
    tmp.close()
    return FileResponse(tmp.name, filename="users.csv", media_type="text/csv")


@app.get("/download/csv/orders")
async def download_orders_csv():
    import csv, tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w", encoding="utf-8", newline="")
    writer = csv.writer(tmp)
    writer.writerow(["id", "user_id", "store_id", "plan_name", "days", "amount", "currency", "method", "status", "created_at", "proof"])
    for order in db.orders(limit=10000):
        writer.writerow([order["id"], order["user_id"], order["store_id"], order["plan_name"], order["days"],
                         order["amount"], order["currency"], order["method"], order["status"], order["created_at"], order.get("proof")])
    tmp.close()
    return FileResponse(tmp.name, filename="orders.csv", media_type="text/csv")


@app.get("/download/csv/files")
async def download_files_csv():
    import csv, tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w", encoding="utf-8", newline="")
    writer = csv.writer(tmp)
    writer.writerow(["id", "store_id", "store_name", "name", "kind", "views", "created_at"])
    for store in db.all_stores():
        for f in db.files_of(store["id"]):
            writer.writerow([f["id"], f["store_id"], store["name"], f["name"], f["kind"], f["views"], f["created_at"]])
    tmp.close()
    return FileResponse(tmp.name, filename="files.csv", media_type="text/csv")


@app.get("/health")
async def health() -> JSONResponse:
    stats = db.stats()
    return JSONResponse({
        "ok": True,
        "uptime_seconds": int(time.time() - STARTED_AT),
        "users": stats["users"],
        "files": stats["files"],
        "pending_orders": stats["orders_pending"],
        "time": int(time.time()),
    })


def main() -> int:
    setup_logging()
    problems = cfg.validate()
    if problems:
        print("Config problem:", "; ".join(problems))
        return 2
    if not cfg.WEB_PASS:
        print("Refusing to start: set WEB_PASS (and WEB_USER) in config.env first.")
        return 2

    db.bind(cfg.DB_FILE)
    db.migrate_legacy(cfg.LEGACY_DB_FILE)

    import uvicorn
    log.info("Dashboard on http://%s:%s (user %s)", cfg.WEB_HOST, cfg.WEB_PORT, cfg.WEB_USER)
    uvicorn.run(app, host=cfg.WEB_HOST, port=cfg.WEB_PORT, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

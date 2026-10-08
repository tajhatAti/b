"""Extra admin pages: channels, composer, analytics, user detail, limited links.

Split out of `web/dashboard.py` so the route file stays readable — every function
here returns a ready HTML *body*, and the routes only wire forms to it.
"""
from __future__ import annotations

import time

from app import runtime
from app.services import channels, forcejoin, settings
from app.storage import db
from app.utils import esc, fmt_ts


def _start(ts) -> str:
    return fmt_ts(ts, "%d %b %Y") if ts else "—"


def _pill(text: str) -> str:
    return f'<span class="pill">{esc(text)}</span>'


# ------------------------------------------------------------------- channels
def channels_body(flash: str = "", warn: str = "") -> str:
    rows = []
    for row in db.join_channels():
        scope = "🌍 সব স্টোরে" if row.get("store_id") is None else f"🏪 স্টোর #{row['store_id']}"
        invite = row.get("invite") or forcejoin.join_url(row.get("ref") or "")
        rows.append(
            f"<tr><td><b>{esc(row.get('title') or row.get('ref') or '')}</b>"
            f"<div class='muted'>{esc(row.get('ref') or '')}</div></td>"
            f"<td>{_pill(row.get('kind') or '—')}</td>"
            f"<td>{esc(scope)}</td>"
            f"<td>{'✅' if row.get('chat_id') else '⏳'} {esc(str(row.get('chat_id') or '—'))}</td>"
            f"<td>{'✅' if row.get('enabled') else '🚫'}</td>"
            f"<td><a class='btn grey' href='/admin/channels/check/{row['id']}'>🔎 চেক</a> "
            f"<a class='btn grey' href='/admin/channels/toggle/{row['id']}'>"
            f"{'🚫 বন্ধ' if row.get('enabled') else '✅ চালু'}</a> "
            f"<a class='btn bad' href='/admin/channels/delete/{row['id']}'>🗑</a></td></tr>")
        if invite:
            rows[-1] = rows[-1].replace("<td>✅", f"<td>{_pill('🔗')} ✅", 1)

    stores = "".join(
        f"<option value='{s['id']}'>{esc(s['name'])}</option>" for s in db.all_stores())

    note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
    return f"""{note}{warnbox}
    <h3>📡 চ্যানেল ম্যানেজমেন্ট</h3>
    <p class="muted">যে চ্যানেলগুলোতে বট পোস্ট করতে পারবে — ফোর্স-জয়েন, ক্যাশ এবং
       “চ্যানেলে পাঠান” সবখানে এগুলো ব্যবহার হয়। যেকোনো রেফারেন্স চলবে:
       <code>@username</code>, <code>https://t.me/+invite</code>, <code>-100…</code>।</p>
    <div class="card">
      <form method=post action="/admin/channels/add">
        <label>চ্যানেল লিংক / ইউজারনেম / আইডি</label>
        <input name=ref placeholder="@mychannel অথবা https://t.me/+AbCdEfGh" required>
        <label>কোন স্টোরের জন্য? (খালি = সব স্টোরে)</label>
        <select name=store_id><option value="">🌍 সব স্টোরে (ডিফল্ট)</option>{stores}</select>
        <button class="ok">➕ যোগ করুন</button>
      </form>
    </div>
    <table><tr><th>চ্যানেল</th><th>ধরন</th><th>কার জন্য</th><th>ID</th><th>চালু</th><th></th></tr>
    {''.join(rows) or "<tr><td colspan=6 class='muted'>এখনো কোনো চ্যানেল যোগ করা হয়নি</td></tr>"}</table>
    <p class="row"><a class="btn" href="/admin/channels/compose">✍️ চ্যানেলে পোস্ট করুন</a>
       <a class="btn grey" href="/admin/channels?refresh=1">🔄 আবার রিজলভ করুন</a></p>"""


def compose_body(composer: dict, store_id: int | None, flash: str = "", warn: str = "") -> str:
    store_options = "".join(
        f"<option value='{s['id']}'{' selected' if store_id == s['id'] else ''}>"
        f"{esc(s['name'])}</option>" for s in db.all_stores())
    channel_options = []
    for row in db.join_channels():
        label = row.get("title") or row.get("ref")
        channel_options.append(
            f"<option value='{esc(str(row.get('chat_id') or row.get('ref')))}'>"
            f"{esc(label)}</option>")
    files = db.newest_files(200)
    file_options = "".join(
        f"<option value='{f['id']}'>#{f['id']} · {esc(f['name'])} · {esc(f['kind'])}</option>"
        for f in files)
    note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
    preview = channels.composer_preview(composer["text"], composer["buttons"], store_id)
    preview_buttons = "".join(
        f"<div class='row'>{''.join(f'<span class=pill>{esc(label)} → {esc(url)}</span>' for label, url in row)}</div>"
        for row in preview["buttons"]) or '<span class="muted">কোনো বাটন নেই</span>'
    return f"""{note}{warnbox}
    <h3>✍️ চ্যানেলে মেসেজ + ইনলাইন বাটন</h3>
    <form method=post action="/admin/channels/compose">
      <label>চ্যানেল</label>
      <select name=target>{''.join(channel_options) or '<option value="">— আগে চ্যানেল যোগ করুন —</option>'}</select>
      <label>স্টোর (বাটন/প্রিভিউর জন্য)</label>
      <select name=store_id><option value="">— কোনোটিই না —</option>{store_options}</select>
      <label>মেসেজ (HTML চলে: &lt;b&gt;, &lt;i&gt;, &lt;a href&gt;)</label>
      <textarea name=text rows=7>{esc(composer['text'])}</textarea>
      <label>ফাইল (একাধিক হলে Ctrl/Cmd চেপে বাছুন)</label>
      <select name=file_ids multiple size=6>{file_options}</select>
      <label>লিমিটেড লিংক মোড — চ্যানেলে ভিডিও নয়, কতবার খোলা যাবে এমন বাটন যাবে
             (0 = লিংক মোড বন্ধ, -1 = বন্ধ; খালি রাখলে ডিফল্ট
             {settings.get_int('LINK_DEFAULT_LIMIT', 100)})</label>
      <input name=link_limit type=number value="-1">
      <label>ইনলাইন বাটন — প্রতি লাইনে: <code>লেবেল | লিংক</code> (একই লাইনে <code>&amp;&amp;</code> দিলে পাশাপাশি)</label>
      <textarea name=buttons rows=4>{esc(composer['buttons'])}</textarea>
      <div class="chk"><input type=checkbox name=send_test value=1 id=st>
        <label for=st>প্রথমে আমাকে টেস্ট পাঠান (বটের DM-এ), চ্যানেলে নয়</label></div>
      <button class="ok">📤 চ্যানেলে পাঠান</button>
    </form>
    <div class="card"><h4>প্রিভিউ</h4>
      <div>{composer['text']}</div>
      <div class="muted">বাটন:</div>{preview_buttons}
    </div>"""


# ------------------------------------------------------------------ analytics
def analytics_body(days: int = 14) -> str:
    counts = db.event_counts()
    by_day = db.events_by_day(days)
    users = db.user_count()
    stores = db.all_stores()
    links = db.link_count()
    mirrored = db.count_mirrors()
    files = db.count_files()

    day_rows = "".join(
        f"<tr><td>{esc(str(r['day']))}</td><td>{r['users']}</td><td>{r['joined']}</td>"
        f"<td>{r['delivered']}</td><td>{r['paid']}</td></tr>" for r in by_day)
    store_rows = []
    for store in stores:
        funnel = db.store_funnel(store["id"])
        store_rows.append(
            f"<tr><td><b>{esc(store['name'])}</b><div class='muted'>#{store['id']}</div></td>"
            f"<td>{funnel['came']}</td><td>{funnel['joined']}</td><td>{funnel['watched']}</td>"
            f"<td>{funnel['paid']}</td>"
            f"<td><a class='btn grey' href='/admin/analytics/store/{store['id']}'>বিস্তারিত</a></td></tr>")
    top_files = db.top_files(10)
    file_rows = "".join(
        f"<tr><td>#{f['id']} · {esc(f['name'])}</td><td>{esc(f.get('store_name') or '')}</td>"
        f"<td>{db.unique_viewers(f['id'])}</td><td>{f['views']}</td>"
        f"<td><a class='btn grey' href='/admin/analytics/file/{f['id']}'>কে দেখেছে</a></td></tr>"
        for f in top_files)
    event_rows = "".join(
        f"<tr><td>{esc(fmt_ts(e['ts'], '%d %b %H:%M'))}</td>"
        f"<td>{_user_link(e.get('user_id'), e.get('user_name'))}</td>"
        f"<td>{_pill(e['name'])}</td><td>{esc(e.get('store_name') or '—')}</td>"
        f"<td>{esc(e.get('file_name') or '—')}</td><td>{esc((e.get('detail') or '')[:60])}</td></tr>"
        for e in db.events(limit=60))

    return f"""<h3>📊 অ্যানালিটিক্স</h3>
    <div class="grid">
      {_card("ইউজার", users)}{_card("ফাইল", files)}{_card("ক্যাশড", mirrored)}
      {_card("লিংক", links)}{_card("স্টোর", len(stores))}
      {_card("ভিডিও পাঠানো", counts.get("deliver", 0))}
      {_card("স্টোর ওপেন", counts.get("open_store", 0))}
      {_card("পেমেন্ট শুরু", counts.get("pay_start", 0))}
    </div>
    <h4>📈 শেষ {days} দিন</h4>
    <table><tr><th>দিন</th><th>ইউজার</th><th>নতুন</th><th>ডেলিভারি</th><th>পেমেন্ট</th></tr>
    {day_rows or "<tr><td colspan=5 class='muted'>কোনো ডেটা নেই</td></tr>"}</table>
    <h4>🏪 স্টোর ফানেল (এসেছে → জয়েন → দেখেছে → কিনেছে)</h4>
    <table><tr><th>স্টোর</th><th>এসেছে</th><th>জয়েন</th><th>দেখেছে</th><th>কিনেছে</th><th></th></tr>
    {''.join(store_rows) or "<tr><td colspan=6 class='muted'>স্টোর নেই</td></tr>"}</table>
    <h4>🔥 জনপ্রিয় ফাইল</h4>
    <table><tr><th>ফাইল</th><th>স্টোর</th><th>ইউনিক</th><th>মোট</th><th></th></tr>
    {file_rows or "<tr><td colspan=5 class='muted'>এখনো কেউ দেখেনি</td></tr>"}</table>
    <h4>🕒 সর্বশেষ ইভেন্ট</h4>
    <table><tr><th>সময়</th><th>ইউজার</th><th>কাজ</th><th>স্টোর</th><th>ফাইল</th><th>বিস্তারিত</th></tr>
    {event_rows or "<tr><td colspan=6 class='muted'>ইভেন্ট নেই</td></tr>"}</table>"""


def _card(label: str, value) -> str:
    return f'<div class="card"><div class="l">{esc(label)}</div><div class="n">{value}</div></div>'


def _user_link(user_id, name=None) -> str:
    if not user_id:
        return '<span class="muted">—</span>'
    label = esc(name or str(user_id))
    return f"<a href='/admin/users/{user_id}'>{label}</a>"


def user_detail_body(user_id: int) -> str:
    data = db.user_analytics(user_id, limit=150)
    user = data["user"] or {}
    counts = data["counts"]
    grants = db.user_grants(user_id)
    orders = db.orders(user_id=user_id)
    tickets = db.tickets(status=None, user_id=user_id, limit=20)

    grant_rows = "".join(
        f"<tr><td>{esc(g['store_name'])}</td><td>{_start(g['created_at'])}</td>"
        f"<td>{'♾ লাইফটাইম' if g['expires_at'] is None else _start(g['expires_at'])}</td>"
        f"<td>{esc(g.get('source') or '')}</td></tr>" for g in grants)
    order_rows = "".join(
        f"<tr><td>#{o['id']}</td><td>{esc(str(o.get('plan_name') or ''))}</td>"
        f"<td>{esc(str(o.get('status') or ''))}</td><td>{_start(o.get('created_at'))}</td></tr>"
        for o in orders)
    store_rows = "".join(
        f"<tr><td>{esc(r['name'])}</td><td>{r['hits']}</td>"
        f"<td>{_start(r['last_ts'])}</td></tr>" for r in data["stores"])
    video_rows = "".join(
        f"<tr><td>#{r['id']} · {esc(r['name'])}</td><td>{r['hits']}</td>"
        f"<td>{_start(r['last_ts'])}</td></tr>" for r in data["videos"])
    search_text = ", ".join(f"“{esc(r['ref'])}”×{r['c']}" for r in data["searches"]) or "—"
    event_rows = "".join(
        f"<tr><td>{esc(fmt_ts(e['ts'], '%d %b %H:%M:%S'))}</td><td>{_pill(e['name'])}</td>"
        f"<td>{esc(e.get('store_name') or '—')}</td><td>{esc(e.get('file_name') or '—')}</td>"
        f"<td>{esc((e.get('detail') or '')[:80])}</td></tr>" for e in data["events"])

    return f"""<h3>👤 ইউজার #{user_id}</h3>
    <div class="grid">
      {_card("স্টোর ওপেন", counts.get("open_store", 0))}
      {_card("ভিডিও দেখেছে", counts.get("deliver", 0))}
      {_card("লিংক ক্লিক", counts.get("view_file", 0))}
      {_card("জয়েন ব্লক", counts.get("join_block", 0))}
      {_card("পেমেন্ট শুরু", counts.get("pay_start", 0))}
      {_card("কিনেছে", counts.get("paid", 0))}
    </div>
    <table>
      <tr><th>নাম</th><td>{esc(user.get("name") or "—")}</td>
          <th>ইউজারনেম</th><td>{esc('@' + (user.get('username') or '') if user.get('username') else '—')}</td></tr>
      <tr><th>জয়েন</th><td>{_start(user.get('joined_at'))}</td>
          <th>শেষ দেখা</th><td>{_start(user.get('last_seen'))}</td></tr>
      <tr><th>সার্চ</th><td colspan=3>{search_text}</td></tr>
    </table>
    <p class="row">
      <a class="btn" href="tg://user?id={user_id}">💬 টেলিগ্রামে মেসেজ</a>
      <form method=post action="/admin/users/{user_id}/message" style="display:inline">
        <input name=text placeholder="ড্যাশবোর্ড থেকে মেসেজ পাঠান" style="width:320px">
        <button class="ok">📨 পাঠান</button>
      </form>
      <a class="btn warn" href="/admin/users/{user_id}/ban">🚫 ব্যান</a>
      <a class="btn grey" href="/admin/users/{user_id}/unban">✅ আনব্যান</a>
    </p>
    <h4>🏪 কোন স্টোরে কতবার</h4>
    <table><tr><th>স্টোর</th><th>হিট</th><th>শেষ</th></tr>{store_rows or "<tr><td colspan=3 class='muted'>—</td></tr>"}</table>
    <h4>🎬 কোন ভিডিও দেখেছে</h4>
    <table><tr><th>ভিডিও</th><th>বার</th><th>শেষ</th></tr>{video_rows or "<tr><td colspan=3 class='muted'>—</td></tr>"}</table>
    <h4>💎 অ্যাক্সেস</h4>
    <table><tr><th>স্টোর</th><th>দেওয়া</th><th>শেষ</th><th>সোর্স</th></tr>{grant_rows or "<tr><td colspan=4 class='muted'>কোনো অ্যাক্সেস নেই</td></tr>"}</table>
    <h4>🧾 অর্ডার</h4>
    <table><tr><th>#</th><th>প্ল্যান</th><th>স্টেটাস</th><th>সময়</th></tr>{order_rows or "<tr><td colspan=4 class='muted'>—</td></tr>"}</table>
    <h4>📩 সাপোর্ট টিকিট</h4>
    <table><tr><th>#</th><th>স্টেটাস</th><th>মেসেজ</th><th>সময়</th></tr>
    {''.join(f"<tr><td>{t['id']}</td><td>{esc(str(t.get('status') or ''))}</td>"
             f"<td>{esc((t.get('message') or '')[:80])}</td><td>{_start(t.get('created_at'))}</td></tr>"
             for t in tickets) or "<tr><td colspan=4 class='muted'>—</td></tr>"}</table>
    <h4>🕒 পুরো ইতিহাস</h4>
    <table><tr><th>সময়</th><th>কাজ</th><th>স্টোর</th><th>ফাইল</th><th>বিস্তারিত</th></tr>
    {event_rows or "<tr><td colspan=5 class='muted'>—</td></tr>"}</table>"""


def store_analytics_body(store_id: int) -> str:
    data = db.store_analytics(store_id)
    store = data["store"]
    funnel = data["funnel"]
    if not store:
        return '<div class="warnbox">স্টোর পাওয়া যায়নি</div>'
    file_rows = "".join(
        f"<tr><td>#{f['id']} · {esc(f['name'])}</td><td>{esc(f['kind'])}</td>"
        f"<td>{f['views']}</td><td>{f['uniq']}</td><td>{f['sends']}</td>"
        f"<td>{_start(f['last_ts'])}</td>"
        f"<td><a class='btn grey' href='/admin/analytics/file/{f['id']}'>কে দেখেছে</a></td></tr>"
        for f in data["files"])
    watch_rows = "".join(
        f"<tr><td>{_user_link(w['user_id'], w['name'])}</td><td>{esc('@' + (w['username'] or '') if w['username'] else '')}</td>"
        f"<td>{w['videos']}</td><td>{w['sends']}</td><td>{_start(w['last_ts'])}</td></tr>"
        for w in data["watchers"])
    event_rows = "".join(
        f"<tr><td>{esc(fmt_ts(e['ts'], '%d %b %H:%M'))}</td>"
        f"<td>{_user_link(e.get('user_id'), e.get('user_name'))}</td>"
        f"<td>{_pill(e['name'])}</td><td>{esc(e.get('file_name') or '—')}</td></tr>"
        for e in data["events"][:80])
    return f"""<h3>🏪 {esc(store['name'])} — অ্যানালিটিক্স</h3>
    <div class="grid">
      {_card("এসেছে", funnel['came'])}{_card("জয়েন করেছে", funnel['joined'])}
      {_card("ভিডিও দেখেছে", funnel['watched'])}{_card("কিনেছে", funnel['paid'])}
      {_card("মোট ভিউ", store.get('views') or 0)}
    </div>
    <h4>🎬 কোন ভিডিও কতজন দেখেছে</h4>
    <table><tr><th>ফাইল</th><th>ধরন</th><th>ভিউ</th><th>ইউনিক</th><th>মোট পাঠানো</th><th>শেষ</th><th></th></tr>
    {file_rows or "<tr><td colspan=7 class='muted'>ফাইল নেই</td></tr>"}</table>
    <h4>👥 কে কী দেখেছে</h4>
    <table><tr><th>ইউজার</th><th>ইউজারনেম</th><th>ভিন্ন ভিডিও</th><th>মোট পাঠানো</th><th>শেষ</th></tr>
    {watch_rows or "<tr><td colspan=5 class='muted'>এখনো কেউ দেখেনি</td></tr>"}</table>
    <h4>🕒 ইভেন্ট</h4>
    <table><tr><th>সময়</th><th>ইউজার</th><th>কাজ</th><th>ফাইল</th></tr>
    {event_rows or "<tr><td colspan=4 class='muted'>—</td></tr>"}</table>"""


def file_watchers_body(file_id: int) -> str:
    row = db.file(file_id)
    if row is None:
        return '<div class="warnbox">ফাইল পাওয়া যায়নি</div>'
    store = db.store(row["store_id"]) or {}
    rows = "".join(
        f"<tr><td>{_user_link(w['user_id'], w['name'])}</td>"
        f"<td>{esc('@' + (w['username'] or '') if w['username'] else '')}</td>"
        f"<td>{w['hits']}</td><td>{_start(w['last_ts'])}</td></tr>"
        for w in db.file_watchers(file_id))
    prompt_rows = "".join(
        f"<tr><td>{_user_link(p['user_id'], p.get('user_name'))}</td>"
        f"<td>{p['attempts']}</td><td>{_start(p['created'])}</td></tr>"
        for p in db.join_prompts(200) if p["file_id"] == file_id)
    mirror = "✅ ক্যাশড" if row.get("mirror_msg") else "⏳ ক্যাশ হয়নি"
    return f"""<h3>🎬 {esc(row['name'])} <span class="muted">#{file_id}</span></h3>
    <p class="row">{_pill(store.get('name') or '?')}{_pill(row['kind'])}
       {_pill('ভিউ ' + str(row['views']))}{_pill(mirror)}
       <a class="btn grey" href="/admin/files">← ফাইল লিস্ট</a></p>
    <h4>👥 কারা পেয়েছে ({len(db.file_watchers(file_id))})</h4>
    <table><tr><th>ইউজার</th><th>ইউজারনেম</th><th>বার</th><th>শেষ</th></tr>
    {rows or "<tr><td colspan=4 class='muted'>এখনো কেউ পায়নি</td></tr>"}</table>
    <h4>🔐 জয়েন করে নেয়নি (কতবার চেষ্টা)</h4>
    <table><tr><th>ইউজার</th><th>চেষ্টা</th><th>শুরু</th></tr>
    {prompt_rows or "<tr><td colspan=3 class='muted'>—</td></tr>"}</table>"""


# ------------------------------------------------------------------ link tools
def links_body(flash: str = "", warn: str = "") -> str:
    stores = "".join(
        f"<option value='{s['id']}'>{esc(s['name'])}</option>" for s in db.all_stores())
    files = db.newest_files(300)
    file_options = "".join(
        f"<option value='{f['id']}'>#{f['id']} · {esc(f['name'])} · {esc(f['kind'])}</option>"
        for f in files)
    rows = []
    for link in db.links(limit=100):
        limit = int(link.get("max_clicks") or 0)
        clicks = int(link.get("clicks") or 0)
        left = "♾" if not limit else max(0, limit - clicks)
        url = ""
        if runtime.bot_username:
            url = f"https://t.me/{runtime.bot_username}?start=t{link['token']}"
        note = link.get("kind2") or ""
        open_link_html = (f"<a class='btn grey' href='{esc(url)}' target='_blank'>🔗</a> "
                          if url else "")
        delete_html = f"<a class='btn bad' href='/admin/links/delete/{link['token']}'>🗑</a>"
        rows.append(
            f"<tr><td><code>{esc(link['token'])}</code><div class='muted'>{esc(note)}</div></td>"
            f"<td>{esc(link.get('kind') or '')}</td>"
            f"<td>{clicks}</td><td>{limit or '♾'}</td><td>{left}</td>"
            f"<td>{_start(link.get('created_at'))}</td>"
            f"<td>{'⌛ ' + _start(link['expires_at']) if link.get('expires_at') else '♾'}</td>"
            f"<td>{open_link_html}{delete_html}</td></tr>")
    note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
    return f"""{note}{warnbox}
    <h3>🔗 লিংক (লিমিটেড / আনলিমিটেড)</h3>
    <p class="muted">লিমিট দিলে ঠিক ততবারই খোলা যাবে; তারপর ইউজারকে
       “লিমিট শেষ — অ্যাক্সেস দেওয়া হবে না” বলা হবে।</p>
    <div class="card">
      <form method=post action="/admin/links/new">
        <label>ফাইল বাছুন (একাধিক হলে Ctrl/Cmd)</label>
        <select name=file_ids multiple size=8 required>{file_options}</select>
        <label>স্টোর (খালি রাখলে সরাসরি ফাইল)</label>
        <select name=store_id><option value="">— সরাসরি —</option>{stores}</select>
        <label>কতবার খোলা যাবে (0 = আনলিমিটেড)</label>
        <input name=max_clicks type=number min=0 value="{settings.get_int('LINK_DEFAULT_LIMIT', 100)}">
        <label>নোট (নিজের জন্য)</label>
        <input name=note placeholder="যেমন: অমুক গ্রাহকের জন্য ১০০ ক্লিক">
        <button class="ok">🔗 লিংক বানান</button>
      </form>
    </div>
    <table><tr><th>টোকেন</th><th>ধরন</th><th>ব্যবহৃত</th><th>লিমিট</th><th>বাকি</th>
    <th>তৈরি</th><th>মেয়াদ</th><th></th></tr>
    {''.join(rows) or "<tr><td colspan=8 class='muted'>কোনো লিংক নেই</td></tr>"}</table>"""


def store_forcejoin_body(store: dict) -> str:
    """Per-store force-join editor (inside the store page)."""
    rows = "".join(
        f"<tr><td>{esc(ch.get('title') or ch.get('ref'))}</td>"
        f"<td>{esc(ch.get('ref') or '')}</td>"
        f"<td>{'✅' if ch.get('chat_id') else '⏳'}</td>"
        f"<td><a class='btn bad' href='/admin/channels/delete/{ch['id']}'>🗑</a></td></tr>"
        for ch in db.join_channels(store["id"]))
    return f"""<h4>📢 এই স্টোরের জন্য আলাদা চ্যানেল</h4>
    <div class="card">
      <form method=post action="/admin/stores/{store['id']}/forcejoin">
        <label>বাধ্যতামূলক চ্যানেল (খালি = বন্ধ)</label>
        <input name=ref value="{esc(store.get('forcejoin') or '')}"
               placeholder="@store_channel / https://t.me/+AbCdEfGhIjK / -100…">
        <button class="ok">💾 সেভ</button>
      </form>
      <table><tr><th>চ্যানেল</th><th>রেফারেন্স</th><th>ID</th><th></th></tr>
      {rows or "<tr><td colspan=4 class='muted'>এই স্টোরে আলাদা কোনো চ্যানেল নেই — ডিফল্ট চ্যানেল প্রযোজ্য</td></tr>"}</table>
    </div>"""


def store_admin_body(store: dict, flash: str = "", warn: str = "") -> str:
    """Everything about one store in one page (the owner kept asking for this)."""
    stats = db.store_stats(store["id"])
    funnel = db.store_funnel(store["id"])
    plans = db.plans(store["id"])
    drip = db.drip(store["id"])
    link = ""
    if runtime.bot_username:
        link = f"https://t.me/{runtime.bot_username}?start=s{store['id']}"
    files = db.files_of(store["id"], newest_first=True)
    file_rows = "".join(
        f"<tr><td>#{f['id']}</td><td>{esc(f['name'])}</td><td>{esc(f['kind'])}</td>"
        f"<td>{f['views']}</td><td>{'✅' if f.get('mirror_msg') else '⏳'}</td>"
        f"<td><a class='btn' href='/admin/files/broadcast/{f['id']}'>📢</a> "
        f"<a class='btn grey' href='/admin/analytics/file/{f['id']}'>👥</a></td></tr>"
        for f in files[:100])
    grants = db.store_grants(store["id"])
    grant_rows = "".join(
        f"<tr><td>{_user_link(g['user_id'], g.get('user_name'))}</td>"
        f"<td>{_start(g['created_at'])}</td>"
        f"<td>{'♾' if g['expires_at'] is None else _start(g['expires_at'])}</td>"
        f"<td>{esc(g.get('source') or '')}</td></tr>" for g in grants[:100])
    note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    warnbox = f'<div class="warnbox">{esc(warn)}</div>' if warn else ""
    return f"""{note}{warnbox}
    <h3>🏪 {esc(store['name'])} <span class="muted">#{store['id']}</span></h3>
    <div class="grid">
      {_card("ফাইল", stats['files'])}{_card("ভিউ", stats['views'])}
      {_card("ক্যাশড", sum(1 for f in files if f.get('mirror_msg')))}
      {_card("অ্যাক্সেস", stats['grants'])}{_card("সাবস্ক্রাইবার", stats['subs'])}
      {_card("এসেছে", funnel['came'])}{_card("দেখেছে", funnel['watched'])}
      {_card("কিনেছে", funnel['paid'])}
    </div>
    <p class="row">
      {f'<a class="btn" href="{esc(link)}" target="_blank">▶️ বট লিংক</a>' if link else ''}
      <a class="btn grey" href="/s/{esc(store['slug'] or store['id'])}">🌐 সাইটে দেখুন</a>
      <a class="btn grey" href="/admin/files?store={store['id']}">🗂 ফাইল</a>
      <a class="btn grey" href="/admin/analytics/store/{store['id']}">📊 অ্যানালিটিক্স</a>
      <a class="btn" href="/admin/files/broadcast/{(files[0]['id'] if files else 0)}">📢 ব্রডকাস্ট</a>
      <a class="btn" href="/admin/channels/compose?store={store['id']}">📡 চ্যানেলে পোস্ট</a>
    </p>
    {store_forcejoin_body(store)}
    <h4>💎 প্ল্যান ({len(plans)}) · 📅 ড্রিপ
      ({'চালু ' + str(drip['count']) + '/দিন ' + drip['send_time'] if drip and drip['enabled'] else 'বন্ধ'})</h4>
    <h4>📂 ফাইল (সর্বশেষ {min(len(files), 100)})</h4>
    <table><tr><th>#</th><th>নাম</th><th>ধরন</th><th>ভিউ</th><th>ক্যাশ</th><th></th></tr>
    {file_rows or "<tr><td colspan=6 class='muted'>ফাইল নেই</td></tr>"}</table>
    <h4>👥 অ্যাক্সেস দেওয়া ইউজার</h4>
    <table><tr><th>ইউজার</th><th>দেওয়া</th><th>শেষ</th><th>সোর্স</th></tr>
    {grant_rows or "<tr><td colspan=4 class='muted'>—</td></tr>"}</table>"""


def now_label() -> str:
    return fmt_ts(time.time(), "%d %b %Y %H:%M")

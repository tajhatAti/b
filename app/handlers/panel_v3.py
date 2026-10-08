"""Bot-side screens for the v3 features: channels, analytics, limited links.

The website panel is the “advanced” place, but the owner should never be stuck
without a computer — everything here mirrors what the web panel can do:

    ch:home      📡 channel management (list / resolve / toggle / delete / add)
    ch:add       add a channel by typing any link, or from a *forwarded* post
    ch:post      publish the active store's newest file to a channel with buttons
    an:home      📊 analytics (totals + per-store funnel)
    an:store:N   🏪 one store: who watched which video, came/joined/watched/paid
    an:file:N    🎬 one file: who received it + who is stuck at the join gate
    lk:home      🔗 links with a click limit (create / list / delete)
"""
from __future__ import annotations

from telethon.tl.custom import Button

from app import runtime, ui
from app.handlers.router import route
from app.handlers.state import ask
from app.services import access, channels, forcejoin
from app.storage import db
from app.utils import esc, fmt_ts, safe_int


def _is_admin(event) -> bool:
    return access.is_admin(event.sender_id)


def _short(text: str, size: int = 26) -> str:
    text = text or ""
    return text if len(text) <= size else text[: size - 1] + "…"


async def _render(event, text: str, buttons) -> None:
    await ui.render(event, text, buttons, edit=True)


# =================================================================== channels
@route("ch:home")
async def channels_home(event, rest: str) -> None:
    if not _is_admin(event):
        await event.answer("Admins only.", alert=True)
        return
    rows = db.join_channels()
    lines = [
        "📡 <b>চ্যানেল ম্যানেজমেন্ট</b>",
        "",
        "বট যেসব চ্যানেলে পোস্ট করতে পারবে — ফোর্স-জয়েন, ক্যাশ আর “চ্যানেলে পাঠান” "
        "সবখানে এগুলো ব্যবহার হয়।",
        "",
    ]
    if not rows:
        lines.append("<i>এখনো কোনো চ্যানেল যোগ করা হয়নি।</i>")
    for row in rows[:20]:
        state = "✅" if row.get("chat_id") else "⏳"
        scope = "সব স্টোরে" if row.get("store_id") is None else f"স্টোর #{row['store_id']}"
        lines.append(f"{state} <b>{esc(_short(row.get('title') or row.get('ref'), 32))}</b> "
                     f"· {esc(scope)}")
        if row.get("error"):
            lines.append(f"   <i>⚠️ {esc(row['error'][:70])}</i>")
    buttons = [
        [Button.inline("➕ চ্যানেল যোগ করুন", "ch:add")],
        [Button.inline("🔄 সব চেক করুন", "ch:checkall"),
         Button.inline("✍️ চ্যানেলে পোস্ট", "ch:post")],
    ]
    for row in rows[:8]:
        buttons.append([Button.inline(f"✅ {_short(row.get('title') or row.get('ref'), 22)}",
                                      f"ch:check:{row['id']}"),
                        Button.inline("🚫" if row.get("enabled") else "✅",
                                      f"ch:toggle:{row['id']}"),
                        Button.inline("🗑", f"ch:del:{row['id']}")])
    buttons.append([Button.inline("🔙 প্যানেল", "adm:back")])
    await _render(event, "\n".join(lines), buttons)
    await event.answer()


@route("ch:add")
async def channels_add(event, rest: str) -> None:
    if not _is_admin(event):
        return
    ask(event.sender_id, "add_channel")
    await event.respond(
        "📡 <b>চ্যানেলের লিংক পাঠান</b>\n\n"
        "যেকোনো একটা চলবে:\n"
        "  • <code>@mychannel</code>\n"
        "  • <code>https://t.me/mychannel</code>\n"
        "  • <code>https://t.me/+AbCdEfGhIjK</code> (প্রাইভেট ইনভাইট)\n"
        "  • চ্যানেল আইডি <code>-100…</code>\n\n"
        "অথবা <b>চ্যানেলের যেকোনো মেসেজ forward করুন</b> — সেটাও ধরতে পারব।\n"
        "<i>/cancel দিলে বাতিল।</i>")
    await event.answer()


@route("ch:check:")
async def channels_check(event, rest: str) -> None:
    if not _is_admin(event):
        return
    row = db.join_channel(safe_int(rest))
    if row is None:
        await event.answer("চ্যানেল নেই", alert=True)
        return
    info = await channels.channel_info(row.get("chat_id") or row["ref"])
    if info.get("ok"):
        db.update_join_channel(row["id"], chat_id=info["chat_id"],
                               title=info.get("title") or row.get("title"),
                               last_check=0, error="")
        if info.get("chat_id"):
            db.set_meta(f"chat_title:{info['chat_id']}", info.get("title") or "")
        await event.answer(f"✅ {info.get('title')} · {info.get('members')} সদস্য", alert=True)
    else:
        db.update_join_channel(row["id"], error=str(info.get("error") or "")[:180])
        await event.answer(f"⚠️ {str(info.get('error'))[:120]}", alert=True)
    await channels_home(event, "")


@route("ch:toggle:")
async def channels_toggle(event, rest: str) -> None:
    if not _is_admin(event):
        return
    row = db.join_channel(safe_int(rest))
    if row is None:
        return
    db.update_join_channel(row["id"], enabled=0 if row.get("enabled") else 1)
    forcejoin.clear_cache()
    await event.answer("✅ আপডেট হয়েছে", alert=True)
    await channels_home(event, "")


@route("ch:del:")
async def channels_delete(event, rest: str) -> None:
    if not _is_admin(event):
        return
    db.delete_join_channel(safe_int(rest))
    forcejoin.clear_cache()
    await event.answer("🗑 মুছে ফেলা হলো", alert=True)
    await channels_home(event, "")


@route("ch:checkall")
async def channels_check_all(event, rest: str) -> None:
    if not _is_admin(event):
        return
    await event.answer("🔄 চেক করছি…")
    fixed = 0
    for row in db.join_channels():
        if row.get("chat_id"):
            continue
        result = await channels.add_channel(row["ref"], row.get("store_id"))
        if result.get("chat_id"):
            db.update_join_channel(row["id"], chat_id=result["chat_id"],
                                   title=result.get("title") or row.get("title"))
            fixed += 1
    await event.answer(f"✅ {fixed} টি চ্যানেল পাওয়া গেল", alert=True)
    await channels_home(event, "")


@route("ch:post")
async def channels_post(event, rest: str) -> None:
    """Publish the newest file of the active store + a tap-to-open button."""
    if not _is_admin(event):
        return
    store_id = db.active_store_id(event.sender_id)
    store = db.store(store_id) if store_id else None
    files = db.files_of(store["id"], newest_first=True) if store else []
    rows = db.join_channels()
    if not rows:
        await event.answer("আগে একটা চ্যানেল যোগ করুন", alert=True)
        return
    if not files:
        await event.answer("অ্যাকটিভ স্টোরে কোনো ফাইল নেই", alert=True)
        return
    target = rows[0]
    peer = target.get("chat_id") or target["ref"]
    from app.services import settings
    username = runtime.bot_username or ""
    buttons = []
    if store and username:
        buttons.append(f"🟢 {_short(store['name'], 30)} খুলুন | "
                       f"https://t.me/{username}?start={store['slug']}")
    default = settings.get_str("BROADCAST_DEFAULT_BUTTONS")
    if default:
        buttons.append(default)
    result = await channels.publish(peer, text=f"🎬 <b>{esc(files[0]['name'])}</b>\n"
                                              f"🏪 {esc(store['name'])}",
                                    file_ids=[files[0]["id"]], buttons="\n".join(buttons),
                                    store_id=store["id"])
    await event.answer("✅ চ্যানেলে পাঠানো হয়েছে" if result.get("ok")
                       else f"⚠️ {str(result.get('error'))[:120]}", alert=True)
    await channels_home(event, "")


# ================================================================== analytics
@route("an:home")
async def analytics_home(event, rest: str) -> None:
    if not _is_admin(event):
        await event.answer("Admins only.", alert=True)
        return
    counts = db.event_counts()
    users = db.user_count()
    lines = [
        "📊 <b>অ্যানালিটিক্স</b>",
        "",
        f"👥 ইউজার: <b>{users}</b> · 🗂 ফাইল: <b>{db.count_files()}</b> · "
        f"✅ ক্যাশড: <b>{db.count_mirrors()}</b> · 🔗 লিংক: <b>{db.link_count()}</b>",
        f"🎬 ভিডিও পাঠানো: <b>{counts.get('deliver', 0)}</b> · "
        f"🏪 স্টোর ওপেন: <b>{counts.get('open_store', 0)}</b>",
        f"🔐 জয়েনে আটকেছে: <b>{counts.get('join_block', 0)}</b> · "
        f"💳 পেমেন্ট শুরু: <b>{counts.get('pay_start', 0)}</b> · "
        f"💰 কিনেছে: <b>{counts.get('paid', 0)}</b>",
        "",
        "<b>স্টোর অনুযায়ী (এসেছে → জয়েন → দেখেছে → কিনেছে)</b>",
    ]
    buttons = []
    for store in db.all_stores()[:8]:
        f = db.store_funnel(store["id"])
        lines.append(f"🏪 <b>{esc(_short(store['name'], 28))}</b> — "
                     f"{f['came']} → {f['joined']} → {f['watched']} → {f['paid']}")
        buttons.append([Button.inline(f"📊 {_short(store['name'], 22)}", f"an:store:{store['id']}")])
    top = db.top_files(5)
    if top:
        lines.append("")
        lines.append("<b>🔥 জনপ্রিয় ফাইল</b>")
        for row in top:
            uniq = db.unique_viewers(row["id"])
            lines.append(f"🎬 {esc(_short(row['name'], 30))} — 👁 {row['views']} "
                         f"(ইউনিক {uniq})")
            buttons.append([Button.inline(f"👥 {_short(row['name'], 22)}",
                                          f"an:file:{row['id']}")])
    buttons.append([Button.inline("🆕 ডাইজেস্ট এখনই পাঠান", "dg:run")])
    buttons.append([Button.inline("🌐 ওয়েবে বিস্তারিত", "an:web"),
                    Button.inline("🔙 প্যানেল", "adm:back")])
    await _render(event, "\n".join(lines), buttons)
    await event.answer()


@route("dg:run")
async def digest_now(event, rest: str) -> None:
    if not _is_admin(event):
        return
    from app.services.scheduler import run_digest
    result = await run_digest(force=True)
    queued = result.get("queued") or []
    if not queued:
        await event.answer("এই সপ্তাহে নতুন ফাইল নেই", alert=True)
        return
    total = sum(item["targets"] for item in queued)
    await event.answer(f"✅ {len(queued)} টি স্টোরের ডাইজেস্ট কিউ হয়েছে ({total} ইউজার)",
                       alert=True)


@route("an:web")
async def analytics_web(event, rest: str) -> None:
    if not _is_admin(event):
        return
    import os
    host = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
    tip = (f"{host}/admin/analytics" if host else
           "হোস্টিং প্যানেলের পাবলিক URL + <code>/admin/analytics</code>")
    await event.answer("🌐 ওয়েব প্যানেল খুলছে…")
    await event.respond(f"🌐 <b>বিস্তারিত অ্যানালিটিক্স ওয়েব প্যানেলে</b>\n\n{tip}\n"
                        "<i>(WEB_PASS দিয়ে লগইন করুন — প্রতি ইউজারের ক্লিক, প্রতিটি ভিডিও "
                        "কারা দেখল, কার কোন স্টোরে কত কনভার্সন — সব টেবিল আকারে।)</i>")


@route("an:store:")
async def analytics_store(event, rest: str) -> None:
    if not _is_admin(event):
        return
    store_id = safe_int(rest.split(":")[0])
    data = db.store_analytics(store_id)
    store = data["store"]
    if not store:
        await event.answer("স্টোর নেই", alert=True)
        return
    funnel = data["funnel"]
    lines = [
        f"🏪 <b>{esc(store['name'])}</b> — অ্যানালিটিক্স",
        "",
        f"👣 এসেছে: <b>{funnel['came']}</b> · 🔐 জয়েন করেছে: <b>{funnel['joined']}</b> · "
        f"🎬 ভিডিও দেখেছে: <b>{funnel['watched']}</b> · 💰 কিনেছে: <b>{funnel['paid']}</b>",
        "",
        "<b>কোন ভিডিও কতজন দেখেছে</b>",
    ]
    buttons = []
    for row in data["files"][:10]:
        lines.append(f"🎬 {esc(_short(row['name'], 28))} — 👁 {row['views']} · "
                     f"ইউনিক {row['uniq']}")
        buttons.append([Button.inline(f"👥 {_short(row['name'], 24)}", f"an:file:{row['id']}")])
    if data["watchers"]:
        lines.append("")
        lines.append("<b>কে দেখেছে</b>")
        for row in data["watchers"][:10]:
            lines.append(f"👤 {esc(_short(row['name'], 22))} — {row['videos']} টি ভিডিও, "
                         f"{row['sends']} বার")
    buttons.append([Button.inline("🔙 অ্যানালিটিক্স", "an:home")])
    await _render(event, "\n".join(lines), buttons)
    await event.answer()


@route("an:file:")
async def analytics_file(event, rest: str) -> None:
    if not _is_admin(event):
        return
    file_id = safe_int(rest.split(":")[0])
    row = db.file(file_id)
    if row is None:
        await event.answer("ফাইল নেই", alert=True)
        return
    store = db.store(row["store_id"]) or {}
    watchers = db.file_watchers(file_id)
    stuck = [p for p in db.join_prompts(200) if p["file_id"] == file_id]
    lines = [
        f"🎬 <b>{esc(_short(row['name'], 34))}</b> <code>#{file_id}</code>",
        f"🏪 {esc(store.get('name') or '?')} · {esc(row['kind'])} · 👁 {row['views']}",
        f"✅ ক্যাশ: {'হয়েছে' if row.get('mirror_msg') else 'হয়নি (প্রথম ক্লিকে হবে)'}",
        "",
        f"<b>যারা পেয়েছে ({len(watchers)})</b>",
    ]
    for w in watchers[:12]:
        lines.append(f"👤 {esc(_short(w['name'] or str(w['user_id']), 24))} — {w['hits']} বার · "
                     f"{esc(fmt_ts(w['last_ts'], '%d %b %H:%M'))}")
    if not watchers:
        lines.append("<i>এখনো কেউ পায়নি।</i>")
    if stuck:
        lines.append("")
        lines.append(f"<b>🔐 জয়েন করে নেয়নি ({len(stuck)})</b>")
        for p in stuck[:10]:
            lines.append(f"👤 {esc(_short(p.get('user_name') or str(p['user_id']), 24))} — "
                         f"{p['attempts']} বার চেষ্টা")
    buttons = [
        [Button.inline("📢 এই ভিডিও ব্রডকাস্ট", f"fbr:{file_id}"),
         Button.inline("🔙 অ্যানালিটিক্স", "an:home")],
    ]
    await _render(event, "\n".join(lines), buttons)
    await event.answer()


# ============================================================== limited links
@route("lk:home")
async def links_home(event, rest: str) -> None:
    if not _is_admin(event):
        await event.answer("Admins only.", alert=True)
        return
    rows = db.links(limit=10)
    from app.services import settings
    default_limit = settings.get_int("LINK_DEFAULT_LIMIT", 100)
    lines = [
        "🔗 <b>লিংক (লিমিটেড / আনলিমিটেড)</b>",
        "",
        "লিমিট দিলে ঠিক ততবারই খোলা যাবে — তারপর ইউজারকে "
        "“লিমিট শেষ, অ্যাক্সেস দেওয়া হবে না” বলা হবে।",
        "",
    ]
    for link in rows:
        limit = int(link.get("max_clicks") or 0)
        clicks = int(link.get("clicks") or 0)
        left = "♾" if not limit else max(0, limit - clicks)
        lines.append(f"<code>{esc(link['token'])}</code> — {clicks}"
                     f"{'/' + str(limit) if limit else ''} ব্যবহৃত · বাকি {left} · "
                     f"{esc((link.get('kind2') or '')[:24])}")
    if not rows:
        lines.append("<i>এখনো কোনো লিংক নেই।</i>")
    buttons = [
        [Button.inline("➕ লিমিটেড লিংক বানান", "lk:new")],
        [Button.inline("🔙 প্যানেল", "adm:back")],
    ]
    for link in rows[:6]:
        buttons.append([Button.inline(f"🗑 {link['token']}", f"lk:del:{link['token']}")])
    await _render(event, "\n".join(lines), buttons)
    await event.answer()


@route("lk:new")
async def links_new(event, rest: str) -> None:
    if not _is_admin(event):
        return
    store_id = db.active_store_id(event.sender_id)
    files = db.files_of(store_id, newest_first=True) if store_id else []
    if not files:
        await event.answer("অ্যাকটিভ স্টোরে ফাইল নেই", alert=True)
        return
    buttons = [[Button.inline(f"🎬 {_short(f['name'], 30)}", f"lk:pick:{f['id']}")]
               for f in files[:10]]
    buttons.append([Button.inline("🔙 লিংক লিস্ট", "lk:home")])
    await _render(event, "🔗 <b>কোন ফাইলের লিংক বানাবেন?</b>", buttons)
    await event.answer()


@route("lk:pick:")
async def links_pick(event, rest: str) -> None:
    if not _is_admin(event):
        return
    file_id = safe_int(rest.split(":")[0])
    from app.services import settings
    default_limit = settings.get_int("LINK_DEFAULT_LIMIT", 100)
    ask(event.sender_id, "link_limit", file_id=file_id)
    await event.respond(
        f"🔢 <b>এই লিংক কতবার খোলা যাবে?</b>\n\n"
        f"শুধু সংখ্যাটি লিখুন (যেমন <code>100</code>)।\n"
        f"ডিফল্ট <b>{default_limit}</b> — <code>0</code> দিলে আনলিমিটেড।\n"
        f"<i>/cancel = বাতিল</i>")
    await event.answer()


@route("lk:del:")
async def links_delete(event, rest: str) -> None:
    if not _is_admin(event):
        return
    token = rest.split(":")[0]
    db.delete_link(token)
    await event.answer("🗑 মুছে ফেলা হলো", alert=True)
    await links_home(event, "")


# ==================================================== per-store force channel
@route("sfj:")
async def store_forcejoin_screen(event, rest: str) -> None:
    if not _is_admin(event):
        return
    store_id = safe_int(rest)
    store = db.store(store_id)
    if store is None:
        await event.answer("স্টোর নেই", alert=True)
        return
    extra = db.join_channels(store_id)
    lines = [
        f"📢 <b>{esc(store['name'])} — আলাদা ফোর্স-জয়েন</b>",
        "",
        "এই স্টোরে ঢোকার আগে ইউজারকে এই চ্যানেলেও জয়েন করতে হবে (ডিফল্ট চ্যানেলের "
        "সাথে যোগ হয়ে)।",
        "",
        f"এখন সেট করা: <b>{esc(store.get('forcejoin') or 'কিছু নেই')}</b>",
    ]
    for row in extra:
        lines.append(f"• {esc(row.get('title') or row.get('ref'))} "
                     f"({'✅' if row.get('chat_id') else '⏳'})")
    buttons = [
        [Button.inline("✏️ চ্যানেল দিন", f"sfje:{store_id}"),
         Button.inline("🚫 বন্ধ করুন", f"sfjo:{store_id}")],
        [Button.inline("🔙 স্টোর সেটিংস", f"sss:{store_id}")],
    ]
    await _render(event, "\n".join(lines), buttons)
    await event.answer()


@route("sfje:")
async def store_forcejoin_edit(event, rest: str) -> None:
    if not _is_admin(event):
        return
    store_id = safe_int(rest)
    ask(event.sender_id, "store_forcejoin", store_id=store_id)
    await event.respond(
        "📢 <b>চ্যানেলের লিংক / ইউজারনেম / আইডি পাঠান</b>\n\n"
        "  • <code>@storechannel</code>\n"
        "  • <code>https://t.me/+AbCdEfGhIjK</code>\n"
        "  • <code>-100…</code>\n\n"
        "<i>/cancel = বাতিল · “off” লিখলে বন্ধ</i>")
    await event.answer()


@route("sfjo:")
async def store_forcejoin_off(event, rest: str) -> None:
    if not _is_admin(event):
        return
    store_id = safe_int(rest)
    db.set_store_forcejoin(store_id, "")
    forcejoin.clear_cache()
    await event.answer("✅ বন্ধ করা হলো", alert=True)
    await store_forcejoin_screen(event, str(store_id))

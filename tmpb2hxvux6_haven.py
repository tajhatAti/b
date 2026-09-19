# requirements: telethon
import asyncio
import time
import json
import os
import re
import uuid
from collections import defaultdict
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.custom import Button
from telethon.errors import SessionPasswordNeededError, UserNotParticipantError, FloodWaitError
from telethon.tl.functions.channels import GetParticipantRequest

# ====== CONFIG ======
API_ID = 37109385
API_HASH = "b50a9ccaf4a0352b895a9fb2998c7f0d"
BOT_TOKEN = "8786524423:AAEAJfQVvqM8YraOrkb_N8g-qfrSvR08Wp0"
ADMIN_IDS = [8768764605, 7813556801]
GROUPS_PAGE_SIZE = 8
CONTENT_PAGE_SIZE = 16
DB_FILE = "bot_db.json"

# Pre-filled userbot session — attached automatically on startup to ADMIN_IDS[0],
# so you never have to run /session manually. Leave "" to go back to manual connect.
STRING_SESSION = "1BVtsOIUBu3qrAGEAEDNz3DpOTbtEU_yF9r1NwHjQmckEms1a-MASr0BTDGhQK0qxYamCxn8KIV9M9gg61dJNxW1zWwM2gZZv5awk2jdR8kmEWxxhYMzIaqPEiq4gXlUvr6XUyqLbS_Fb8e9riz3fWNAQvnI-ouBMQGdWAxtFbSKzW0IGOB-P3oSzrPGY08zAuSDy-su9NEq4GhykuoMqVnN6XjD3cI0DGBrpgpi-MLGSuxKaVuDwyyc3gSPx_Nbn4MVD7GFHAqvi3O-wkLozXLj8mA1dY12TZTm6P1UEqCww60qVHxPCYjEh6iNLbb5WnjY8YS3npbEdUJxvB-GVImtHLImNLGM="
# ====================

bot = TelegramClient('bot_master_session', API_ID, API_HASH)

user_clients = {}         
pending_login = {}        
user_dialogs = {}        
all_bot_users = set()    

# --- PERSISTENT DATABASE ---
stores = defaultdict(lambda: defaultdict(dict))
active_store = {}        
admin_batch_mode = {}    
channel_progress = defaultdict(dict)   
scan_state = {}          
admin_input_state = {}   
jit_locks = defaultdict(asyncio.Lock) # Prevents race conditions during JIT forwarding
pending_forwards = {}   # sender_id -> asyncio.Future, used to catch a JIT-forwarded file the moment it arrives
store_slugs = {}        # slug -> (admin_uid, store_name), gives each store a short shareable link
link_gen_selection = defaultdict(set)  # uid -> set of fids currently picked for the multi-file link
link_gen_context = {}   # uid -> (admin_uid, store_name, page) currently being browsed for link generation
search_pending = {}     # uid -> (admin_uid, store_name), waiting for a search keyword
temp_links = {}         # token -> {"admin_uid", "fids", "expires_at"} for expiring multi-file links
bot_admin_chats = set() # chat_ids where the BOT itself is a member/admin — served with zero session use

FORCE_CHANNEL = None     
CUSTOM_CAPTION = ""      
BOT_USERNAME = None
admin_sessions = {}     # uid -> string session, so connections survive a restart
session_owner_ids = {}  # uid -> the real Telegram user ID logged into that session
bot_entity_cache = {}   # uid -> resolved bot entity (resolve ONCE, never per-click — avoids FloodWait)

# --- PREMIUM ACCESS ---
# premium_access[admin_uid][store_name][target_uid] = expires_at (unix ts) or None for lifetime
premium_access = defaultdict(lambda: defaultdict(dict))
user_details = {}       # uid -> {"joined": ts, "name": str}
grant_premium_ctx = {}  # admin_uid -> {"target": int, "store": str}

# --- REFERRAL SYSTEM ---
referred_by = {}         # referred_uid -> referrer_uid
referral_rewarded = set()  # referred_uid's for whom the referrer has already been paid
referral_config = {}     # admin_uid -> {"store": str, "days": int} — reward for a successful referral

# --- DAILY DRIP (auto-send N files/day per store) ---
drip_config = {}         # "admin_uid:store" -> {"admin_uid":, "store":, "count": n, "time": "HH:MM", "last_sent_date": "YYYY-MM-DD", "enabled": bool}
drip_sent_ids = defaultdict(set)   # "admin_uid:store" -> set of fids already sent via drip
drip_subscribers = defaultdict(set)  # "admin_uid:store" -> set of uids who opted into daily updates

# --- ADVANCED BROADCAST ---
broadcast_filter_ctx = {}  # admin_uid -> filter string ("all" / "premium" / "free" / "store:<name>")
referral_ctx = {}          # admin_uid -> {"store": str} while setting up referral reward
drip_setup_ctx = {}        # admin_uid -> {"store": str, "count": int} while setting up a drip schedule

def make_slug(name):
    base = re.sub(r'[^a-zA-Z0-9]+', '_', name).strip('_').lower() or "store"
    slug = base[:20]
    i = 1
    while slug in store_slugs:
        i += 1
        slug = f"{base[:17]}_{i}"
    return slug

def load_db():
    global FORCE_CHANNEL, CUSTOM_CAPTION
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                data = json.load(f)
                for u, s in data.get('stores', {}).items():
                    stores[int(u)].update(s)
                for u, st in data.get('active_store', {}).items():
                    active_store[int(u)] = st
                FORCE_CHANNEL = data.get('force_channel', None)
                CUSTOM_CAPTION = data.get('custom_caption', "")
                for u, s in data.get('admin_sessions', {}).items():
                    admin_sessions[int(u)] = s
                for slug, pair in data.get('store_slugs', {}).items():
                    store_slugs[slug] = (int(pair[0]), pair[1])
                for token, info in data.get('temp_links', {}).items():
                    temp_links[token] = info
                for cid in data.get('bot_admin_chats', []):
                    bot_admin_chats.add(int(cid))
                for au, s in data.get('premium_access', {}).items():
                    for sname, users in s.items():
                        for u, exp in users.items():
                            premium_access[int(au)][sname][int(u)] = exp
                for u, d in data.get('user_details', {}).items():
                    user_details[int(u)] = d
                for u, r in data.get('referred_by', {}).items():
                    referred_by[int(u)] = int(r)
                for u in data.get('referral_rewarded', []):
                    referral_rewarded.add(int(u))
                for au, cfg in data.get('referral_config', {}).items():
                    referral_config[int(au)] = cfg
                for key, cfg in data.get('drip_config', {}).items():
                    drip_config[key] = cfg
                for key, ids in data.get('drip_sent_ids', {}).items():
                    drip_sent_ids[key] = set(ids)
                for key, subs in data.get('drip_subscribers', {}).items():
                    drip_subscribers[key] = set(int(s) for s in subs)
        except Exception as e:
            print("DB Load Error:", e)

def save_db():
    try:
        with open(DB_FILE, 'w') as f:
            json.dump({
                "stores": stores,
                "active_store": active_store,
                "force_channel": FORCE_CHANNEL,
                "custom_caption": CUSTOM_CAPTION,
                "admin_sessions": admin_sessions,
                "store_slugs": store_slugs,
                "temp_links": temp_links,
                "bot_admin_chats": list(bot_admin_chats),
                "premium_access": premium_access,
                "user_details": user_details,
                "referred_by": referred_by,
                "referral_rewarded": list(referral_rewarded),
                "referral_config": referral_config,
                "drip_config": drip_config,
                "drip_sent_ids": {k: list(v) for k, v in drip_sent_ids.items()},
                "drip_subscribers": {k: list(v) for k, v in drip_subscribers.items()}
            }, f)
    except Exception as e:
        print("DB Save Error:", e)

# Load database on startup
load_db()


# --- KEYBOARD BUILDERS ---
def build_admin_panel_keyboard(uid):
    curr_store = active_store.get(uid, "Not Set")
    f_chan = FORCE_CHANNEL if FORCE_CHANNEL else "Off"
    cap_status = "Set" if CUSTOM_CAPTION else "Off"
    session_label = "📲 Session: ✅ Connected" if uid in user_clients else "📲 Session Connect"

    return [
        [Button.inline(f"📌 Active Store: {curr_store}", data=b"adm_set_active")],
        [Button.inline("➕ Create New Store", data=b"adm_create_store"), Button.inline("🗑 Delete Store", data=b"adm_del_store")],
        [Button.inline(f"📢 Force Join ({f_chan})", data=b"adm_force_join"), Button.inline(f"📝 Caption ({cap_status})", data=b"adm_caption")],
        [Button.inline(session_label, data=b"adm_connect"), Button.inline("🔍 Scan Channel", data=b"adm_scan")],
        [Button.inline("🤖 Bot-Admin Scan (No Session)", data=b"adm_botscan")],
        [Button.inline("🔗 Store Links", data=b"adm_store_links"), Button.inline("🎬 Multi-Link Generator", data=b"adm_linkgen")],
        [Button.inline("📦 Batch Link Generator", data=b"adm_batch"), Button.inline("📢 Broadcast Msg", data=b"adm_broadcast")],
        [Button.inline("👑 Grant Premium", data=b"adm_grant_premium"), Button.inline("👥 All Users", data=b"adm_all_users")],
        [Button.inline("🎁 Referral Reward", data=b"adm_referral"), Button.inline("📅 Daily Drip", data=b"adm_drip")],
        [Button.inline("👁 Preview User View", data=b"adm_user_preview")]
    ]

def build_user_stores_keyboard(admin_uid=None):
    if admin_uid is not None:
        pairs = [(admin_uid, name) for name in stores.get(admin_uid, {}).keys()]
    else:
        # No specific admin requested — show every store from every admin combined
        pairs = [(auid, name) for auid, s in stores.items() for name in s.keys()]

    buttons = []
    for i in range(0, len(pairs), 2):
        a1, n1 = pairs[i]
        row = [Button.inline(f"🏪 {n1}", data=f"ustore:{a1}:{n1}".encode())]
        if i + 1 < len(pairs):
            a2, n2 = pairs[i + 1]
            row.append(Button.inline(f"🏪 {n2}", data=f"ustore:{a2}:{n2}".encode()))
        buttons.append(row)
    return buttons

def build_store_content_keyboard(admin_uid, store_name, page=0):
    files = list(stores[admin_uid][store_name].values())
    total_pages = (len(files) + CONTENT_PAGE_SIZE - 1) // CONTENT_PAGE_SIZE or 1
    start = page * CONTENT_PAGE_SIZE
    page_files = files[start:start + CONTENT_PAGE_SIZE]

    def label(f):
        views = f.get('views', 0)
        icon = "🎬" if f.get('type') == "Video" else "🖼" if f.get('type') == "Photo" else "📁"
        return f"{icon} {f['name']} · 👁{views}"

    buttons = []
    for i in range(0, len(page_files), 2):
        row = [Button.inline(label(page_files[i]), data=f"get:{page_files[i]['id']}".encode())]
        if i + 1 < len(page_files):
            row.append(Button.inline(label(page_files[i+1]), data=f"get:{page_files[i+1]['id']}".encode()))
        buttons.append(row)

    nav = []
    if page > 0: nav.append(Button.inline("« Prev", data=f"spg:{admin_uid}:{store_name}:{page - 1}".encode()))
    nav.append(Button.inline(f"{page + 1}/{total_pages}", data=b"noop"))
    if start + CONTENT_PAGE_SIZE < len(files): nav.append(Button.inline("Next »", data=f"spg:{admin_uid}:{store_name}:{page + 1}".encode()))
    
    buttons.append(nav)
    buttons.append([Button.inline("🔍 Search This Store", data=f"ssearch:{admin_uid}:{store_name}".encode())])
    buttons.append([Button.inline("🔙 Back to Stores", data=f"back_stores:{admin_uid}".encode())])
    return buttons, page_files, len(files)


async def wait_for_forward(sender_id, timeout=10):
    """Wait for the next incoming message from sender_id, without using get_messages
    (bots can't call GetHistoryRequest — this uses live updates instead, which bots CAN use)."""
    fut = asyncio.get_event_loop().create_future()
    pending_forwards[sender_id] = fut
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        pending_forwards.pop(sender_id, None)

@bot.on(events.NewMessage(incoming=True))
async def catch_jit_forward(event):
    sid = event.sender_id
    fut = pending_forwards.get(sid)
    if fut and not fut.done() and event.media:
        fut.set_result(event.message)

@bot.on(events.CallbackQuery(pattern=b'ssearch:'))
async def search_trigger(event):
    _, admin_uid, store_name = event.data.decode().split(':', 2)
    admin_uid = int(admin_uid)
    if not has_store_access(event.sender_id, admin_uid, store_name):
        await event.answer("🔒 Premium store — you don't have access.", alert=True)
        return
    search_pending[event.sender_id] = (admin_uid, store_name)
    await event.respond(f"🔍 Send a keyword to search in **{store_name}**:")
    await event.answer()

@bot.on(events.NewMessage())
async def search_input_handler(event):
    uid = event.sender_id
    if uid not in search_pending: return
    if event.raw_text.strip().startswith('/'): return
    admin_uid, store_name = search_pending.pop(uid)
    keyword = event.raw_text.strip().lower()
    if not keyword:
        return
    files = list(stores.get(admin_uid, {}).get(store_name, {}).values())
    matches = [f for f in files if keyword in f['name'].lower()][:CONTENT_PAGE_SIZE]

    if not matches:
        await event.respond(f"❌ No files found matching **'{keyword}'**.")
        return

    buttons = []
    for i in range(0, len(matches), 2):
        icon1 = "🎬" if matches[i].get('type') == "Video" else "🖼" if matches[i].get('type') == "Photo" else "📁"
        row = [Button.inline(f"{icon1} {matches[i]['name']}", data=f"get:{matches[i]['id']}".encode())]
        if i + 1 < len(matches):
            icon2 = "🎬" if matches[i+1].get('type') == "Video" else "🖼" if matches[i+1].get('type') == "Photo" else "📁"
            row.append(Button.inline(f"{icon2} {matches[i+1]['name']}", data=f"get:{matches[i+1]['id']}".encode()))
        buttons.append(row)
    buttons.append([Button.inline("🔙 Back to Store", data=f"ustore:{admin_uid}:{store_name}".encode())])
    await event.respond(f"🔍 **Results for '{keyword}':** (`{len(matches)}` found)", buttons=buttons)


def has_store_access(uid, admin_uid, store_name):
    """Admins always pass. Everyone else needs a non-expired premium grant for that store."""
    if uid in ADMIN_IDS:
        return True
    exp = premium_access.get(admin_uid, {}).get(store_name, {}).get(uid, "NOPE")
    if exp == "NOPE":
        return False
    if exp is None:  # lifetime
        return True
    if time.time() < exp:
        return True
    return False


def is_premium_anywhere(uid, admin_uid):
    for sname in premium_access.get(admin_uid, {}):
        if has_store_access(uid, admin_uid, sname):
            return True
    return False

def get_broadcast_targets(admin_uid, filter_value):
    if filter_value == "all":
        return set(all_bot_users)
    if filter_value == "premium":
        return {u for u in all_bot_users if is_premium_anywhere(u, admin_uid)}
    if filter_value == "free":
        return {u for u in all_bot_users if not is_premium_anywhere(u, admin_uid)}
    if filter_value.startswith("store:"):
        sname = filter_value.split(":", 1)[1]
        return {u for u in all_bot_users if has_store_access(u, admin_uid, sname)}
    return set(all_bot_users)

async def grant_referral_reward(referrer_uid):
    """Called once a referred user first starts the bot. Gives the referrer their configured reward."""
    for admin_uid, cfg in referral_config.items():
        store_name, days = cfg.get("store"), cfg.get("days", 0)
        if not store_name or store_name not in stores.get(admin_uid, {}):
            continue
        current_exp = premium_access[admin_uid][store_name].get(referrer_uid)
        base = current_exp if (current_exp and current_exp > time.time()) else time.time()
        premium_access[admin_uid][store_name][referrer_uid] = base + days * 86400
        try:
            await bot.send_message(referrer_uid, f"🎁 Your referral joined! You've earned **{days} days** of access to **{store_name}**.")
        except Exception:
            pass


# --- MEDIA SENDER WITH JIT ALGORITHM ---
def bump_views(target):
    target['views'] = target.get('views', 0) + 1
    save_db()

async def send_stored_media(event, media_id, check_access=True):
    if not await check_force_join(event, event.sender_id): return False
    caption_text = CUSTOM_CAPTION if CUSTOM_CAPTION else ""

    for uid_key, user_stores in stores.items():
        for store_name, file_dict in user_stores.items():
            if media_id in file_dict:
                if check_access and not has_store_access(event.sender_id, uid_key, store_name):
                    await event.respond(f"🔒 **{store_name}** is a premium store. Contact the admin to get access.")
                    return False
                target = file_dict[media_id]
                client = user_clients.get(uid_key)

                # Step 1: Try direct bot access (Works if bot is channel admin)
                try:
                    msg = await bot.get_messages(target['chat_id'], ids=target['msg_id'])
                    if msg and msg.media:
                        await bot.send_file(event.chat_id, msg.media, caption=caption_text, noforwards=True)
                        bump_views(target)
                        return True
                except Exception:
                    pass 

                # Step 2: Ultimate Workable Fix (JIT Forwarding via Session)
                if client:
                    try:
                        async with jit_locks[uid_key]:
                            # Resolve the bot's entity ONCE per session and cache it — resolving on
                            # every click quickly trips Telegram's username-lookup rate limit.
                            if uid_key not in bot_entity_cache:
                                bot_entity_cache[uid_key] = await client.get_entity(BOT_USERNAME)
                            bot_entity = bot_entity_cache[uid_key]

                            # Bots can't "look back" with get_messages/GetHistoryRequest — that's
                            # restricted for bot accounts. So we start listening BEFORE forwarding,
                            # and catch the file the instant it lands as a live update.
                            fetch_peer = session_owner_ids.get(uid_key, uid_key)
                            wait_task = asyncio.create_task(wait_for_forward(fetch_peer, timeout=10))
                            await client.forward_messages(bot_entity, target['msg_id'], target['chat_id'])
                            r_msg = await wait_task

                            if r_msg and r_msg.media:
                                await bot.send_file(event.chat_id, r_msg.media, caption=caption_text, noforwards=True)
                                bump_views(target)
                                return True
                            else:
                                print(f"JIT: no forwarded media arrived in time for uid_key={uid_key}")
                                if event.sender_id in ADMIN_IDS:
                                    await event.respond("⚠️ **Debug (admin only):** Forwarded the file but nothing arrived in the bot's DM within 10s. Is the session account actually a member of that source channel?")
                    except FloodWaitError as e:
                        mins = e.seconds // 60
                        print(f"JIT Logic Flood Wait: {e.seconds}s")
                        if event.sender_id in ADMIN_IDS:
                            await event.respond(f"⏳ **Debug (admin only):** Telegram is rate-limiting this session's lookups.\nWait `~{mins} min` before it works again, or connect a different session in the meantime.")
                    except Exception as e:
                        print(f"JIT Logic Failed: {e}")
                        if event.sender_id in ADMIN_IDS:
                            await event.respond(f"⚠️ **Debug (admin only):** JIT forwarding failed:\n`{e}`")

    if event.sender_id in ADMIN_IDS:
        await event.respond("❌ **File not found or Admin Session is disconnected.**\n(Admin must connect /session to serve private channel files).\n\n_You're seeing this because no matching store entry was found, or every send attempt above failed — check the debug message(s) above for the real reason._")
    else:
        await event.respond("❌ **File not found or Admin Session is disconnected.**\n(Admin must connect /session to serve private channel files).")
    return False


async def check_force_join(event, user_id):
    if not FORCE_CHANNEL or user_id in ADMIN_IDS: return True
    try:
        await bot(GetParticipantRequest(channel=FORCE_CHANNEL, user_id=user_id))
        return True
    except UserNotParticipantError:
        btn = [[Button.url("📢 Join Channel First", f"https://t.me/{FORCE_CHANNEL.replace('@', '')}")]]
        await event.respond("⚠️ **You must join our official channel to access files!**", buttons=btn)
        return False
    except Exception: return True


# --- COMMAND HANDLERS ---
@bot.on(events.NewMessage(pattern=r'/(start|admin)'))
async def start_or_admin_handler(event):
    uid = event.sender_id
    is_new_user = uid not in all_bot_users
    if is_new_user:
        sender = await event.get_sender()
        user_details[uid] = {"joined": time.time(), "name": getattr(sender, "first_name", None) or "Unknown"}
        save_db()
    all_bot_users.add(uid)
    args = event.raw_text.split()
    cmd = args[0].lower()

    if len(args) > 1:
        payload = args[1]
        if payload.startswith("ref_"):
            try:
                referrer_uid = int(payload.replace("ref_", "", 1))
            except ValueError:
                referrer_uid = None
            if referrer_uid and referrer_uid != uid and is_new_user and uid not in referred_by:
                referred_by[uid] = referrer_uid
                if uid not in referral_rewarded:
                    referral_rewarded.add(uid)
                    save_db()
                    await grant_referral_reward(referrer_uid)
                else:
                    save_db()
            # Falls through to the normal welcome screen below — no special content for this payload.
        elif payload.startswith("LINK_"):
            token = payload.replace("LINK_", "", 1)
            info = temp_links.get(token)
            if not info:
                await event.respond("⚠️ This link is invalid or was already removed.")
                return
            if info["expires_at"] and time.time() > info["expires_at"]:
                temp_links.pop(token, None)
                save_db()
                await event.respond("⌛ **This link has expired.**")
                return
            if not await check_force_join(event, uid): return
            await event.respond(f"📦 Sending {len(info['fids'])} file(s)...")
            for mid in info["fids"]:
                await send_stored_media(event, mid, check_access=False)  # admin explicitly generated this link
                await asyncio.sleep(0.4)
            return
        elif payload.startswith("STORE_"):
            slug = payload.replace("STORE_", "", 1)
            pair = store_slugs.get(slug)
            if not pair:
                await event.respond("⚠️ This store link is invalid or no longer exists.")
                return
            if not await check_force_join(event, uid): return
            admin_uid, store_name = pair
            if store_name not in stores.get(admin_uid, {}):
                await event.respond("⚠️ This store no longer exists.")
                return
            if not has_store_access(uid, admin_uid, store_name):
                await event.respond(f"🔒 **{store_name}** is a premium store. Contact the admin to get access.")
                return
            buttons, _, count = build_store_content_keyboard(admin_uid, store_name, 0)
            await event.respond(f"🏪 **Store:** {store_name}\n📂 Items: `{count}`", buttons=buttons)
            return
        elif payload.startswith("BATCH_"):
            raw_ids = payload.replace("BATCH_", "").split("_AND_")
            await event.respond("📦 Sending batch media...")
            for mid in raw_ids:
                await send_stored_media(event, mid, check_access=False)  # admin explicitly generated this link
                await asyncio.sleep(0.4)
            return
        else:
            await send_stored_media(event, payload)
            return

    if uid not in ADMIN_IDS:
        if cmd == '/admin': return
        if not await check_force_join(event, uid): return
        buttons = build_user_stores_keyboard()
        if not buttons:
            await event.respond("👋 Welcome! No content stores available right now.")
            return
        buttons.append([Button.inline("🎁 Invite & Earn", data=b"my_referral")])
        await event.respond("👋 **Select a Category to View Files:**", buttons=buttons)
        return

    await event.respond("⚙️ **Admin GUI Control Panel**", buttons=build_admin_panel_keyboard(uid))


# === CONTENT VIEWER COMMAND ===
@bot.on(events.NewMessage(pattern='/content'))
async def view_content_cmd(event):
    uid = event.sender_id
    if uid not in ADMIN_IDS: return
    
    user_stores = stores.get(uid, {})
    if not user_stores:
        await event.respond("📂 No stores found! Create one first.")
        return

    me = await bot.get_me()
    text = "📁 **Your Database Content:**\n\n"
    
    for sname, files in user_stores.items():
        text += f"🏪 **Store: `{sname}`** (Total Files: `{len(files)}`)\n"
        
        # Preview top 5 files to avoid massive text blocks
        f_list = list(files.values())[-5:]
        for f in f_list:
            text += f"  ├ 🎬 {f['name']} - [Direct Link](https://t.me/{me.username}?start={f['id']})\n"
        
        if len(files) > 5:
            text += f"  └ *...and {len(files)-5} more files.*\n"
        text += "\n"
        
    await event.respond(text, link_preview=False)


@bot.on(events.NewMessage(pattern=r'/session(?:\s+(.+))?'))
async def session_cmd_handler(event):
    if event.sender_id not in ADMIN_IDS: return
    session_str = event.pattern_match.group(1)
    if not session_str or not session_str.strip():
        admin_input_state[event.sender_id] = "session_input"
        await event.respond("💬 Please send your **String Session** in the next message:")
        return
    await connect_with_string_session(event, session_str.strip())

async def connect_with_string_session(event, session_str):
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await event.respond("❌ **Invalid Session String!**")
            return
        user_clients[event.sender_id] = client
        admin_sessions[event.sender_id] = session_str
        bot_entity_cache.pop(event.sender_id, None)
        me = await client.get_me()
        session_owner_ids[event.sender_id] = me.id
        save_db()
        await event.respond(f"✅ **Successfully Connected!**\nLogged in as: `{me.first_name}`")
    except Exception as e:
        await event.respond(f"⚠️ **Session Connection Error:**\n`{str(e)}`")


def get_or_make_slug(admin_uid, store_name):
    for slug, pair in store_slugs.items():
        if pair == (admin_uid, store_name):
            return slug
    slug = make_slug(store_name)
    store_slugs[slug] = (admin_uid, store_name)
    save_db()
    return slug

def build_link_gen_keyboard(req_uid, admin_uid, store_name, page=0):
    files = list(stores[admin_uid][store_name].values())
    total_pages = (len(files) + CONTENT_PAGE_SIZE - 1) // CONTENT_PAGE_SIZE or 1
    start = page * CONTENT_PAGE_SIZE
    page_files = files[start:start + CONTENT_PAGE_SIZE]
    sel = link_gen_selection[req_uid]

    buttons = []
    for i in range(0, len(page_files), 2):
        f1 = page_files[i]
        mark1 = "✅" if f1['id'] in sel else ("🎬" if f1.get('type') == "Video" else "🖼" if f1.get('type') == "Photo" else "⬜")
        row = [Button.inline(f"{mark1} {f1['name']}", data=f"lgtog:{f1['id']}".encode())]
        if i + 1 < len(page_files):
            f2 = page_files[i + 1]
            mark2 = "✅" if f2['id'] in sel else ("🎬" if f2.get('type') == "Video" else "🖼" if f2.get('type') == "Photo" else "⬜")
            row.append(Button.inline(f"{mark2} {f2['name']}", data=f"lgtog:{f2['id']}".encode()))
        buttons.append(row)

    nav = []
    if page > 0: nav.append(Button.inline("« Prev", data=f"lgpg:{page - 1}".encode()))
    nav.append(Button.inline(f"{page + 1}/{total_pages}", data=b"noop"))
    if start + CONTENT_PAGE_SIZE < len(files): nav.append(Button.inline("Next »", data=f"lgpg:{page + 1}".encode()))
    buttons.append(nav)

    buttons.append([Button.inline(f"🔗 Generate Link ({len(sel)} selected)", data=b"lggen"), Button.inline("❌ Clear", data=b"lgclear")])
    buttons.append([Button.inline("🔙 Back to Panel", data=b"adm_back_main")])
    return buttons


USERS_PAGE_SIZE = 10

async def show_all_users_page(event, admin_uid, page):
    uids = sorted(all_bot_users)
    total_pages = (len(uids) + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE or 1
    page = max(0, min(page, total_pages - 1))
    start = page * USERS_PAGE_SIZE
    chunk = uids[start:start + USERS_PAGE_SIZE]

    lines = [f"👥 **All Users** (`{len(uids)}` total)\n"]
    for u in chunk:
        info = user_details.get(u, {})
        name = info.get("name", "Unknown")
        joined = info.get("joined")
        joined_str = time.strftime("%Y-%m-%d", time.localtime(joined)) if joined else "—"
        premium_stores = []
        for sname, users in premium_access.get(admin_uid, {}).items():
            if u in users:
                exp = users[u]
                premium_stores.append(sname if exp is None else f"{sname} (exp {time.strftime('%Y-%m-%d', time.localtime(exp))})")
        pstr = ", ".join(premium_stores) if premium_stores else "Free"
        lines.append(f"`{u}` — {name} | joined {joined_str} | {pstr}")

    nav = []
    if page > 0: nav.append(Button.inline("« Prev", data=f"av_pg:{page-1}".encode()))
    nav.append(Button.inline(f"{page+1}/{total_pages}", data=b"noop"))
    if start + USERS_PAGE_SIZE < len(uids): nav.append(Button.inline("Next »", data=f"av_pg:{page+1}".encode()))
    buttons = [nav, [Button.inline("🔙 Back to Panel", data=b"adm_back_main")]]

    text = "\n".join(lines)
    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        await event.respond(text, buttons=buttons)


# --- ADMIN PANEL CALLBACKS ---
@bot.on(events.CallbackQuery())
async def admin_gui_callbacks(event):
    uid = event.sender_id
    data = event.data

    if data.startswith(b'ustore:') or data.startswith(b'spg:') or data.startswith(b'back_stores:') or data.startswith(b'get:') or data.startswith(b'ssearch:') or data.startswith(b'my_referral') or data.startswith(b'notify_toggle:'):
        return

    if uid not in ADMIN_IDS: return

    if data == b"adm_user_preview":
        buttons = build_user_stores_keyboard(uid)
        buttons.append([Button.inline("🔙 Back to Admin Panel", data=b"adm_back_main")])
        await event.edit("👁 **User View Preview Mode:**\nThis is exactly what users see:", buttons=buttons)

    elif data == b"adm_back_main":
        await event.edit("⚙️ **Admin GUI Control Panel**", buttons=build_admin_panel_keyboard(uid))

    elif data == b"adm_create_store":
        admin_input_state[uid] = "create_store"
        await event.respond("💬 Send the **Name** for the new Store Category (e.g., `Movies 🍿`):")
        await event.answer()

    elif data == b"adm_set_active":
        user_stores = stores.get(uid, {})
        if not user_stores:
            await event.answer("No stores created yet!", alert=True)
            return
        btns = [[Button.inline(name, data=f"sel_act:{name}".encode())] for name in user_stores.keys()]
        await event.respond("📌 Select Active Store Target:", buttons=btns)
        await event.answer()

    elif data.startswith(b"sel_act:"):
        sname = data.decode().split(':', 1)[1]
        active_store[uid] = sname
        save_db()
        await event.respond(f"✅ Active store updated to **[{sname}]**.")
        await event.answer()

    elif data == b"adm_del_store":
        user_stores = stores.get(uid, {})
        if not user_stores:
            await event.answer("No stores to delete.", alert=True)
            return
        btns = [[Button.inline(f"🗑 {name}", data=f"del_act:{name}".encode())] for name in user_stores.keys()]
        await event.respond("Select store to delete:", buttons=btns)

    elif data.startswith(b"del_act:"):
        sname = data.decode().split(':', 1)[1]
        if sname in stores[uid]:
            del stores[uid][sname]
            if active_store.get(uid) == sname: active_store[uid] = None
            save_db()
            await event.respond(f"🗑 Store **[{sname}]** deleted.")

    elif data == b"adm_force_join":
        admin_input_state[uid] = "set_channel"
        await event.respond("💬 Send Channel Username (e.g., `@mychannel`) or send `off` to disable:")

    elif data == b"adm_caption":
        admin_input_state[uid] = "set_caption"
        await event.respond("💬 Send your Custom Caption text (or send `clear` to remove caption):")

    elif data == b"adm_store_links":
        user_stores = stores.get(uid, {})
        if not user_stores:
            await event.answer("No stores created yet!", alert=True)
            return
        me = await bot.get_me()
        text = "🔗 **Your Store Links:**\n\n"
        for sname, files in user_stores.items():
            slug = get_or_make_slug(uid, sname)
            text += f"🏪 **{sname}** (`{len(files)}` files)\nhttps://t.me/{me.username}?start=STORE_{slug}\n\n"
        await event.respond(text, link_preview=False)

    elif data == b"adm_linkgen":
        user_stores = stores.get(uid, {})
        if not user_stores:
            await event.answer("No stores yet — scan or add files first!", alert=True)
            return
        btns = [[Button.inline(f"🏪 {name}", data=f"lgstore:{name}".encode())] for name in user_stores.keys()]
        btns.append([Button.inline("🔙 Back to Panel", data=b"adm_back_main")])
        await event.respond("🎬 **Multi-Link Generator**\nPick a store to select files from (2-8 files per link):", buttons=btns)

    elif data.startswith(b"lgstore:"):
        store_name = data.decode().split(':', 1)[1]
        link_gen_selection[uid] = set()
        link_gen_context[uid] = (uid, store_name, 0)
        kb = build_link_gen_keyboard(uid, uid, store_name, 0)
        await event.respond(f"🎬 **Selecting from:** {store_name}\nTap files to select (2-8), then Generate.", buttons=kb)

    elif data.startswith(b"lgtog:"):
        fid = data.decode().split(':', 1)[1]
        sel = link_gen_selection[uid]
        if fid in sel:
            sel.discard(fid)
        else:
            if len(sel) >= 8:
                await event.answer("Max 8 files per link!", alert=True)
                return
            sel.add(fid)
        admin_uid, store_name, page = link_gen_context.get(uid, (uid, None, 0))
        kb = build_link_gen_keyboard(uid, admin_uid, store_name, page)
        try:
            await event.edit(f"🎬 **Selecting from:** {store_name}\nSelected: `{len(sel)}` (2-8 needed)", buttons=kb)
        except Exception:
            pass
        await event.answer()

    elif data.startswith(b"lgpg:"):
        page = int(data.decode().split(':', 1)[1])
        admin_uid, store_name, _ = link_gen_context.get(uid, (uid, None, 0))
        link_gen_context[uid] = (admin_uid, store_name, page)
        kb = build_link_gen_keyboard(uid, admin_uid, store_name, page)
        sel = link_gen_selection[uid]
        await event.edit(f"🎬 **Selecting from:** {store_name}\nSelected: `{len(sel)}` (2-8 needed)", buttons=kb)

    elif data == b"lggen":
        sel = list(link_gen_selection.get(uid, []))
        if len(sel) < 2:
            await event.answer("Select at least 2 files first!", alert=True)
            return
        btns = [
            [Button.inline("⏳ 1 Hour", data=b"lgexp:3600"), Button.inline("⏳ 6 Hours", data=b"lgexp:21600")],
            [Button.inline("⏳ 24 Hours", data=b"lgexp:86400"), Button.inline("⏳ 7 Days", data=b"lgexp:604800")],
            [Button.inline("♾ Never Expires", data=b"lgexp:0")],
            [Button.inline("✏️ Custom Time (minutes)", data=b"lgexp:custom")]
        ]
        await event.respond(f"⏳ **Set link expiry** ({len(sel)} files selected):", buttons=btns)

    elif data.startswith(b"lgexp:"):
        val = data.decode().split(':', 1)[1]
        sel = list(link_gen_selection.get(uid, []))
        if len(sel) < 2:
            await event.answer("Selection lost — please pick files again.", alert=True)
            return
        if val == "custom":
            admin_input_state[uid] = "custom_expiry"
            await event.respond("💬 Send custom expiry in **minutes** (e.g. `90` for 1.5 hours):")
            return
        seconds = int(val)
        expires_at = (time.time() + seconds) if seconds > 0 else None
        token = uuid.uuid4().hex[:10]
        temp_links[token] = {"admin_uid": uid, "fids": sel, "expires_at": expires_at}
        save_db()
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start=LINK_{token}"
        if expires_at is None:
            exp_text = "Never"
        elif seconds >= 86400:
            exp_text = f"{seconds // 86400}d"
        elif seconds >= 3600:
            exp_text = f"{seconds // 3600}h"
        else:
            exp_text = f"{seconds // 60}m"
        await event.respond(f"✅ **Link generated!** ({len(sel)} files, expires: {exp_text})\n{link}")
        link_gen_selection[uid] = set()

    elif data == b"lgclear":
        link_gen_selection[uid] = set()
        admin_uid, store_name, page = link_gen_context.get(uid, (uid, None, 0))
        kb = build_link_gen_keyboard(uid, admin_uid, store_name, page)
        await event.edit(f"🎬 **Selecting from:** {store_name}\nSelected: `0` (2-8 needed)", buttons=kb)

    elif data == b"adm_connect":
        if uid in user_clients:
            who_id = session_owner_ids.get(uid, "unknown")
            btns = [
                [Button.inline("🔄 Replace with New Session", data=b"adm_connect_replace")],
                [Button.inline("🔌 Disconnect Session", data=b"adm_disconnect")]
            ]
            await event.respond(f"📲 **Session already active** (account ID: `{who_id}`).\nWhat would you like to do?", buttons=btns)
        else:
            admin_input_state[uid] = "session_input"
            await event.respond("💬 **Send your String Session string directly below:**")

    elif data == b"adm_connect_replace":
        admin_input_state[uid] = "session_input"
        await event.respond("💬 **Send the new String Session string directly below** (this replaces the current one):")

    elif data == b"adm_disconnect":
        client = user_clients.pop(uid, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        admin_sessions.pop(uid, None)
        session_owner_ids.pop(uid, None)
        bot_entity_cache.pop(uid, None)
        save_db()
        await event.respond("🔌 **Session disconnected.** You can connect a different account's session now.")

    elif data == b"adm_botscan":
        curr_store = active_store.get(uid)
        if not curr_store:
            await event.respond("⚠️ Set an Active Store first (📌 Active Store)!")
            return
        admin_input_state[uid] = "botscan_channel"
        await event.respond(
            "🤖 **Bot-Admin Scan**\n"
            "First make sure this bot is added as **Admin** in your channel.\n\n"
            "Then **forward any one message** from that channel here, "
            "or send its `@username`."
        )

    elif data == b"adm_scan":
        if uid not in user_clients:
            await event.respond("⚠️ Connect session first using Session Connect!")
            return
        client = user_clients[uid]
        dialogs = []
        async for d in client.iter_dialogs():
            if d.is_group or d.is_channel: dialogs.append((d.id, d.name or "Unnamed"))
        user_dialogs[uid] = dialogs
        await event.respond("Select channel to scan:", buttons=build_groups_keyboard(uid, 0))

    elif data == b"adm_batch":
        admin_batch_mode[uid] = []
        await event.respond("📦 **Batch Mode Active!** Forward photos/videos and send `/done` when finished.")

    elif data == b"adm_broadcast":
        user_stores = stores.get(uid, {})
        btns = [
            [Button.inline("📣 All Users", data=b"bc_filter:all")],
            [Button.inline("💎 Premium Users", data=b"bc_filter:premium"), Button.inline("🆓 Free Users", data=b"bc_filter:free")],
        ]
        for sname in user_stores.keys():
            btns.append([Button.inline(f"🏪 {sname} subscribers", data=f"bc_filter:store:{sname}".encode())])
        btns.append([Button.inline("🔙 Back to Panel", data=b"adm_back_main")])
        await event.respond("📢 **Who should get this broadcast?**", buttons=btns)

    elif data.startswith(b"bc_filter:"):
        filter_value = data.decode().split(':', 1)[1]
        broadcast_filter_ctx[uid] = filter_value
        admin_input_state[uid] = "broadcast"
        target_count = len(get_broadcast_targets(uid, filter_value))
        await event.respond(f"📢 Send the message to broadcast to `{target_count}` user(s):")

    elif data == b"adm_referral":
        user_stores = stores.get(uid, {})
        if not user_stores:
            await event.answer("Create a store first!", alert=True)
            return
        cfg = referral_config.get(uid)
        cur = f"Currently: {cfg['days']} days of {cfg['store']}" if cfg else "Not set up yet."
        btns = [[Button.inline(f"🏪 {name}", data=f"ref_store:{name}".encode())] for name in user_stores.keys()]
        await event.respond(f"🎁 **Referral Reward Setup**\n{cur}\n\nPick which store new referrals unlock:", buttons=btns)

    elif data.startswith(b"ref_store:"):
        store_name = data.decode().split(':', 1)[1]
        referral_ctx[uid] = {"store": store_name}
        btns = [
            [Button.inline("7 Days", data=b"ref_days:7"), Button.inline("15 Days", data=b"ref_days:15")],
            [Button.inline("30 Days", data=b"ref_days:30"), Button.inline("✏️ Custom", data=b"ref_days:custom")]
        ]
        await event.respond(f"⏳ **Days of '{store_name}' access per successful referral:**", buttons=btns)

    elif data.startswith(b"ref_days:"):
        val = data.decode().split(':', 1)[1]
        ctx = referral_ctx.get(uid)
        if not ctx:
            await event.answer("Session expired — start over with 🎁 Referral Reward.", alert=True)
            return
        if val == "custom":
            admin_input_state[uid] = "referral_custom_days"
            await event.respond("💬 Send number of **days** to reward per referral:")
            return
        days = int(val)
        referral_config[uid] = {"store": ctx["store"], "days": days}
        save_db()
        referral_ctx.pop(uid, None)
        await event.respond(f"✅ Referral reward set: **{days} days** of **{ctx['store']}** per successful invite.")

    elif data == b"adm_drip":
        user_stores = stores.get(uid, {})
        if not user_stores:
            await event.answer("Create a store first!", alert=True)
            return
        lines = ["📅 **Daily Drip Schedules:**\n"]
        for sname in user_stores.keys():
            key = f"{uid}:{sname}"
            cfg = drip_config.get(key)
            if cfg and cfg.get("enabled"):
                lines.append(f"🏪 {sname}: {cfg['count']}/day at {cfg['time']} · {len(drip_subscribers.get(key, set()))} subscribers")
            else:
                lines.append(f"🏪 {sname}: off")
        btns = [[Button.inline(f"⚙️ Configure {name}", data=f"drip_store:{name}".encode())] for name in user_stores.keys()]
        btns.append([Button.inline("🔙 Back to Panel", data=b"adm_back_main")])
        await event.respond("\n".join(lines), buttons=btns)

    elif data.startswith(b"drip_store:"):
        store_name = data.decode().split(':', 1)[1]
        drip_setup_ctx[uid] = {"store": store_name}
        key = f"{uid}:{store_name}"
        btns = [
            [Button.inline("2 files/day", data=b"drip_cnt:2"), Button.inline("3 files/day", data=b"drip_cnt:3")],
            [Button.inline("✏️ Custom count", data=b"drip_cnt:custom")],
        ]
        if drip_config.get(key, {}).get("enabled"):
            btns.append([Button.inline("⏹ Turn Off Drip", data=f"drip_off:{store_name}".encode())])
        await event.respond(f"📅 **{store_name}** — how many new files per day?", buttons=btns)

    elif data.startswith(b"drip_off:"):
        store_name = data.decode().split(':', 1)[1]
        key = f"{uid}:{store_name}"
        if key in drip_config:
            drip_config[key]["enabled"] = False
            save_db()
        await event.respond(f"⏹ Daily drip turned off for **{store_name}**.")

    elif data.startswith(b"drip_cnt:"):
        val = data.decode().split(':', 1)[1]
        ctx = drip_setup_ctx.get(uid)
        if not ctx:
            await event.answer("Session expired — start over with 📅 Daily Drip.", alert=True)
            return
        if val == "custom":
            admin_input_state[uid] = "drip_custom_count"
            await event.respond("💬 Send how many files to send per day:")
            return
        ctx["count"] = int(val)
        admin_input_state[uid] = "drip_time_input"
        await event.respond("💬 Send the daily send time in **24h HH:MM** format (e.g. `19:30`):")

    elif data == b"adm_grant_premium":
        admin_input_state[uid] = "grant_premium_target"
        await event.respond("💬 Send the **Telegram user ID** to grant premium access to:")

    elif data.startswith(b"gp_store:"):
        store_name = data.decode().split(':', 1)[1]
        ctx = grant_premium_ctx.get(uid)
        if not ctx:
            await event.answer("Session expired — start over with 👑 Grant Premium.", alert=True)
            return
        ctx["store"] = store_name
        btns = [
            [Button.inline("⏳ 7 Days", data=b"gp_dur:604800"), Button.inline("⏳ 30 Days", data=b"gp_dur:2592000")],
            [Button.inline("⏳ 90 Days", data=b"gp_dur:7776000"), Button.inline("♾ Lifetime", data=b"gp_dur:0")],
            [Button.inline("✏️ Custom (days)", data=b"gp_dur:custom")]
        ]
        await event.respond(f"⏳ **Duration for '{store_name}':**", buttons=btns)

    elif data.startswith(b"gp_dur:"):
        val = data.decode().split(':', 1)[1]
        ctx = grant_premium_ctx.get(uid)
        if not ctx or "store" not in ctx:
            await event.answer("Session expired — start over with 👑 Grant Premium.", alert=True)
            return
        if val == "custom":
            admin_input_state[uid] = "grant_premium_custom_days"
            await event.respond("💬 Send number of **days** of access to grant:")
            return
        seconds = int(val)
        expires_at = (time.time() + seconds) if seconds > 0 else None
        target, store_name = ctx["target"], ctx["store"]
        premium_access[uid][store_name][target] = expires_at
        save_db()
        grant_premium_ctx.pop(uid, None)
        dur_text = "Lifetime" if expires_at is None else f"{seconds // 86400} days"
        await event.respond(f"✅ Granted `{target}` access to **{store_name}** ({dur_text}).")
        try:
            await bot.send_message(target, f"🎉 You've been granted premium access to **{store_name}**! Use /start to view it.")
        except Exception:
            pass

    elif data == b"adm_all_users":
        await show_all_users_page(event, uid, 0)

    elif data.startswith(b"av_pg:"):
        page = int(data.decode().split(':', 1)[1])
        await show_all_users_page(event, uid, page)


@bot.on(events.NewMessage())
async def input_interceptor(event):
    uid = event.sender_id
    if uid not in ADMIN_IDS: return
    text = event.raw_text.strip()

    if uid in admin_input_state:
        action = admin_input_state.pop(uid)

        if action == "session_input":
            await connect_with_string_session(event, text)
            return

        elif action == "create_store":
            stores[uid][text] = {}
            active_store[uid] = text
            slug = make_slug(text)
            store_slugs[slug] = (uid, text)
            save_db()
            me = await bot.get_me()
            await event.respond(
                f"✅ Store **'{text}'** created & set Active!\n"
                f"🔗 **Shareable Link:**\nhttps://t.me/{me.username}?start=STORE_{slug}"
            )
            return

        elif action == "set_channel":
            global FORCE_CHANNEL
            FORCE_CHANNEL = None if text.lower() == 'off' else text
            save_db()
            await event.respond(f"✅ Force join updated.")
            return

        elif action == "set_caption":
            global CUSTOM_CAPTION
            CUSTOM_CAPTION = "" if text.lower() == 'clear' else text
            save_db()
            await event.respond("✅ Caption updated.")
            return

        elif action == "grant_premium_target":
            try:
                target = int(text)
            except ValueError:
                await event.respond("⚠️ Please send a valid numeric Telegram user ID.")
                return
            user_stores = stores.get(uid, {})
            if not user_stores:
                await event.respond("⚠️ You have no stores yet — create one first.")
                return
            grant_premium_ctx[uid] = {"target": target}
            btns = [[Button.inline(f"🏪 {name}", data=f"gp_store:{name}".encode())] for name in user_stores.keys()]
            await event.respond(f"👑 Granting access to `{target}`.\nPick a store:", buttons=btns)
            return

        elif action == "grant_premium_custom_days":
            try:
                days = int(text)
                if days <= 0: raise ValueError
            except ValueError:
                await event.respond("⚠️ Please send a valid positive number of days.")
                return
            ctx = grant_premium_ctx.get(uid)
            if not ctx or "store" not in ctx:
                await event.respond("⚠️ Session expired — start over with 👑 Grant Premium.")
                return
            expires_at = time.time() + days * 86400
            target, store_name = ctx["target"], ctx["store"]
            premium_access[uid][store_name][target] = expires_at
            save_db()
            grant_premium_ctx.pop(uid, None)
            await event.respond(f"✅ Granted `{target}` access to **{store_name}** ({days} days).")
            try:
                await bot.send_message(target, f"🎉 You've been granted premium access to **{store_name}**! Use /start to view it.")
            except Exception:
                pass
            return

        elif action == "referral_custom_days":
            try:
                days = int(text)
                if days <= 0: raise ValueError
            except ValueError:
                await event.respond("⚠️ Please send a valid positive number of days.")
                return
            ctx = referral_ctx.get(uid)
            if not ctx:
                await event.respond("⚠️ Session expired — start over with 🎁 Referral Reward.")
                return
            referral_config[uid] = {"store": ctx["store"], "days": days}
            save_db()
            referral_ctx.pop(uid, None)
            await event.respond(f"✅ Referral reward set: **{days} days** of **{ctx['store']}** per successful invite.")
            return

        elif action == "drip_custom_count":
            try:
                cnt = int(text)
                if cnt <= 0: raise ValueError
            except ValueError:
                await event.respond("⚠️ Please send a valid positive number.")
                return
            ctx = drip_setup_ctx.get(uid)
            if not ctx:
                await event.respond("⚠️ Session expired — start over with 📅 Daily Drip.")
                return
            ctx["count"] = cnt
            admin_input_state[uid] = "drip_time_input"
            await event.respond("💬 Send the daily send time in **24h HH:MM** format (e.g. `19:30`):")
            return

        elif action == "drip_time_input":
            m = re.match(r'^([01]?\d|2[0-3]):([0-5]\d)$', text)
            if not m:
                await event.respond("⚠️ Please send time as `HH:MM` in 24h format (e.g. `07:00` or `19:30`).")
                return
            ctx = drip_setup_ctx.get(uid)
            if not ctx or "count" not in ctx:
                await event.respond("⚠️ Session expired — start over with 📅 Daily Drip.")
                return
            time_str = f"{int(m.group(1)):02d}:{m.group(2)}"
            store_name = ctx["store"]
            key = f"{uid}:{store_name}"
            drip_config[key] = {
                "admin_uid": uid, "store": store_name, "count": ctx["count"],
                "time": time_str, "last_sent_date": "", "enabled": True
            }
            save_db()
            drip_setup_ctx.pop(uid, None)
            await event.respond(
                f"✅ **Daily Drip active** for **{store_name}**: `{ctx['count']}` file(s) every day at `{time_str}` "
                f"to subscribed users.\n(Users subscribe via 🔔 Get Daily Updates in the store view.)"
            )
            return

        elif action == "broadcast":
            filter_value = broadcast_filter_ctx.pop(uid, "all")
            targets = get_broadcast_targets(uid, filter_value)
            count = 0
            for u in targets:
                try:
                    await bot.send_message(u, text)
                    count += 1
                except Exception: pass
            await event.respond(f"✅ Broadcast sent to `{count}` users.")
            return

        elif action == "botscan_channel":
            curr_store = active_store.get(uid)
            if not curr_store:
                await event.respond("⚠️ Active store not set. Please set one and try again.")
                return

            entity = None
            if event.message.forward:
                try:
                    entity = await event.message.forward.get_chat()
                except Exception:
                    entity = None
                if not entity:
                    await event.respond("⚠️ Couldn't detect the source channel from that forward. Try sending the `@username` instead.")
                    return
            else:
                try:
                    entity = await bot.get_entity(text)
                except Exception as e:
                    await event.respond(f"⚠️ Couldn't find that channel: `{e}`")
                    return

            try:
                await bot.get_messages(entity, limit=1)
            except Exception as e:
                await event.respond(f"❌ **Bot can't access that channel.**\nAdd this bot as **Admin** there first, then try again.\n`{e}`")
                return

            chat_id = entity.id
            bot_admin_chats.add(chat_id)
            save_db()

            scan_state[uid] = {"status": "Running"}
            title = getattr(entity, 'title', 'Channel')
            s_msg = await event.respond(f"🤖 **Bot-Admin Scanning:** {title} ➔ **[{curr_store}]**\nScanned: 0 (no session used)", buttons=[[Button.inline("⏹ Stop", b"stop_scan")]])

            async def bot_worker():
                scanned, found = 0, 0
                try:
                    async for msg in bot.iter_messages(entity, limit=None, reverse=True):
                        if scan_state[uid]["status"] == "Stopped": break
                        scanned += 1
                        if msg.photo or msg.video:
                            found += 1
                            fid = f"FILE_{abs(chat_id)}_{msg.id}"
                            stores[uid][curr_store][fid] = {
                                "id": fid, "name": f"{title[:20]} #{found}",
                                "type": "Video" if msg.video else "Photo", "msg_id": msg.id, "chat_id": chat_id
                            }
                        if scanned % 100 == 0:
                            save_db()
                            try: await s_msg.edit(f"🤖 Scanning (bot-admin, no session used)...\nScanned: {scanned} | Found: {found}", buttons=[[Button.inline("⏹ Stop", b"stop_scan")]])
                            except Exception: pass
                except Exception as e:
                    print(e)
                save_db()
                result_buttons = [
                    [Button.inline("📂 Open This Store", data=f"ustore:{uid}:{curr_store}".encode())],
                    [Button.inline("🤖 Scan Another (Bot)", data=b"adm_botscan"), Button.inline("🔙 Admin Panel", data=b"adm_back_main")]
                ]
                await s_msg.edit(
                    f"✅ **Bot-Admin Scan Completed!** (zero session use 🎉)\n\n"
                    f"🏪 Store: **{curr_store}**\n"
                    f"📄 Messages Scanned: `{scanned}`\n"
                    f"🎬 Media Extracted: `{found}`\n\n"
                    f"These files are served **directly by the bot** from now on.\n\n"
                    f"👇 What next?",
                    buttons=result_buttons
                )

            asyncio.create_task(bot_worker())
            return

        elif action == "custom_expiry":
            try:
                minutes = int(text)
                if minutes <= 0: raise ValueError
            except ValueError:
                await event.respond("⚠️ Please send a valid positive number of minutes.")
                return
            sel = list(link_gen_selection.get(uid, []))
            if len(sel) < 2:
                await event.respond("⚠️ Your selection expired — please pick files again from 🎬 Multi-Link Generator.")
                return
            token = uuid.uuid4().hex[:10]
            expires_at = time.time() + minutes * 60
            temp_links[token] = {"admin_uid": uid, "fids": sel, "expires_at": expires_at}
            save_db()
            me = await bot.get_me()
            link = f"https://t.me/{me.username}?start=LINK_{token}"
            await event.respond(f"✅ **Link generated!** ({len(sel)} files, expires in `{minutes}` min)\n{link}")
            link_gen_selection[uid] = set()
            return

    if event.media and not text.startswith('/'):
        if uid in pending_forwards:
            return  # this message is a JIT-forwarded file being caught elsewhere, not a new upload
        curr_store = active_store.get(uid)
        if not curr_store:
            await event.respond("⚠️ Create/Select an Active Store via `/admin` first!")
            return

        fid = f"FILE_DIR_{event.message.id}"
        stores[uid][curr_store][fid] = {
            "id": fid, "name": f"Item_{len(stores[uid][curr_store]) + 1}",
            "type": "Video" if event.message.video else "Photo", "msg_id": event.message.id, "chat_id": event.chat_id
        }
        save_db()

        if uid in admin_batch_mode:
            admin_batch_mode[uid].append(fid)
            await event.respond(f"➕ Added to batch! Total: {len(admin_batch_mode[uid])}. Send `/done`.")
            return

        me = await bot.get_me()
        await event.respond(f"✅ Saved!\n🔗 Link: https://t.me/{me.username}?start={fid}")


# --- USER VIEW CALLBACKS ---
@bot.on(events.CallbackQuery(pattern=b'my_referral'))
async def my_referral_cb(event):
    uid = event.sender_id
    me = await bot.get_me()
    cfg_lines = []
    for admin_uid, cfg in referral_config.items():
        if cfg.get("store"):
            cfg_lines.append(f"• Invite a friend → **{cfg['days']} days** of **{cfg['store']}**")
    reward_text = "\n".join(cfg_lines) if cfg_lines else "Ask the admin about current referral rewards."
    await event.respond(
        f"🎁 **Your referral link:**\nhttps://t.me/{me.username}?start=ref_{uid}\n\n"
        f"**Rewards:**\n{reward_text}",
        buttons=[[Button.inline("🔙 Back", data=b"back_stores:0")]]
    )
    await event.answer()

@bot.on(events.CallbackQuery(pattern=b'notify_toggle:'))
async def notify_toggle_cb(event):
    uid = event.sender_id
    _, admin_uid, store_name = event.data.decode().split(':', 2)
    key = f"{admin_uid}:{store_name}"
    if uid in drip_subscribers[key]:
        drip_subscribers[key].discard(uid)
        await event.answer("🔕 Unsubscribed from daily updates.", alert=True)
    else:
        drip_subscribers[key].add(uid)
        await event.answer("🔔 You'll get daily updates for this store!", alert=True)
    save_db()

@bot.on(events.CallbackQuery(pattern=b'ustore:'))
async def open_user_store(event):
    if not await check_force_join(event, event.sender_id): return
    _, admin_uid, store_name = event.data.decode().split(':', 2)
    admin_uid = int(admin_uid)
    if not has_store_access(event.sender_id, admin_uid, store_name):
        await event.edit(
            f"🔒 **{store_name}** is a premium store.\nYou don't have access yet — contact the admin to purchase access.",
            buttons=[[Button.inline("🔙 Back to Stores", data=f"back_stores:{admin_uid}".encode())]]
        )
        return
    buttons, _, count = build_store_content_keyboard(admin_uid, store_name, 0)
    key = f"{admin_uid}:{store_name}"
    notify_label = "🔕 Unsubscribe Daily Updates" if event.sender_id in drip_subscribers[key] else "🔔 Get Daily Updates"
    buttons.append([Button.inline(notify_label, data=f"notify_toggle:{admin_uid}:{store_name}".encode())])
    await event.edit(f"🏪 **Store:** {store_name}\n📂 Items: `{count}`", buttons=buttons)

@bot.on(events.CallbackQuery(pattern=b'spg:'))
async def store_pagination(event):
    if not await check_force_join(event, event.sender_id): return
    _, admin_uid, store_name, page = event.data.decode().split(':', 3)
    admin_uid = int(admin_uid)
    if not has_store_access(event.sender_id, admin_uid, store_name):
        await event.answer("🔒 Premium store — you don't have access.", alert=True)
        return
    buttons, _, count = build_store_content_keyboard(admin_uid, store_name, int(page))
    await event.edit(f"🏪 **Store:** {store_name}\n📂 Items: `{count}`", buttons=buttons)

@bot.on(events.CallbackQuery(pattern=b'back_stores:'))
async def back_to_stores(event):
    if not await check_force_join(event, event.sender_id): return
    admin_uid = int(event.data.decode().split(':')[1])
    await event.edit("👋 **Select a Category:**", buttons=build_user_stores_keyboard(admin_uid))

@bot.on(events.CallbackQuery(pattern=b'get:'))
async def get_inline_cb(event):
    mid = event.data.decode().split(':')[1]
    sent = await send_stored_media(event, mid)
    await event.answer("✅ Sent!" if sent else "❌ Not found")

    if sent:
        try:
            msg = await event.get_message()
            current_rows = msg.buttons or []
            new_rows = []
            for row in current_rows:
                new_row = [b for b in row if b.data != event.data]
                if new_row:
                    new_rows.append(new_row)
            if new_rows:
                await event.edit(buttons=new_rows)
            else:
                await event.edit("✅ **You've received everything in this list!**")
        except Exception as e:
            print(f"Button removal failed: {e}")


# --- SCANNING & BATCH HANDLERS ---
def build_groups_keyboard(uid, page=0):
    dialogs = user_dialogs.get(uid, [])
    t_pages = (len(dialogs) + GROUPS_PAGE_SIZE - 1) // GROUPS_PAGE_SIZE or 1
    start = page * GROUPS_PAGE_SIZE
    pd = dialogs[start:start + GROUPS_PAGE_SIZE]
    btns = []
    for i in range(0, len(pd), 2):
        row = [Button.inline(pd[i][1][:15], data=f"selchat:{start+i}".encode())]
        if i + 1 < len(pd): row.append(Button.inline(pd[i+1][1][:15], data=f"selchat:{start+i+1}".encode()))
        btns.append(row)
    nav = []
    if page > 0: nav.append(Button.inline("«", data=f"pg:{page-1}".encode()))
    nav.append(Button.inline(f"{page+1}/{t_pages}", data=b"noop"))
    if start + GROUPS_PAGE_SIZE < len(dialogs): nav.append(Button.inline("»", data=f"pg:{page+1}".encode()))
    btns.append(nav)
    return btns

@bot.on(events.CallbackQuery(pattern=b'pg:'))
async def page_cb(event):
    await event.edit("Select channel:", buttons=build_groups_keyboard(event.sender_id, int(event.data.decode().split(':')[1])))

@bot.on(events.CallbackQuery(pattern=b'selchat:'))
async def select_chat_cb(event):
    uid = event.sender_id
    idx = int(event.data.decode().split(':')[1])
    chat_id, title = user_dialogs[uid][idx]
    
    curr_store = active_store.get(uid)
    client = user_clients[uid]
    scan_state[uid] = {"status": "Running"}
    s_msg = await event.respond(f"🔍 **Scanning:** {title} ➔ **[{curr_store}]**\nScanned: 0", buttons=[[Button.inline("⏹ Stop", b"stop_scan")]])

    async def worker():
        scanned, found = 0, 0
        try:
            async for msg in client.iter_messages(chat_id, limit=None, reverse=True):
                if scan_state[uid]["status"] == "Stopped": break
                scanned += 1
                if msg.photo or msg.video:
                    found += 1
                    fid = f"FILE_{abs(chat_id)}_{msg.id}"
                    stores[uid][curr_store][fid] = {
                        "id": fid, "name": f"{title[:20]} #{found}",
                        "type": "Video" if msg.video else "Photo", "msg_id": msg.id, "chat_id": chat_id
                    }
                if scanned % 100 == 0:
                    save_db() # Persist every 100 files
                    try: await s_msg.edit(f"🔍 Scanning...\nScanned: {scanned} | Found: {found}", buttons=[[Button.inline("⏹ Stop", b"stop_scan")]])
                    except Exception: pass
        except Exception as e: print(e)
        save_db() # Final save
        result_buttons = [
            [Button.inline("📂 Open This Store", data=f"ustore:{uid}:{curr_store}".encode())],
            [Button.inline("🔍 Scan Another Channel", data=b"adm_scan"), Button.inline("🔙 Admin Panel", data=b"adm_back_main")]
        ]
        await s_msg.edit(
            f"✅ **Scan Completed!**\n\n"
            f"🏪 Store: **{curr_store}**\n"
            f"📄 Messages Scanned: `{scanned}`\n"
            f"🎬 Media Extracted: `{found}`\n\n"
            f"👇 What next?",
            buttons=result_buttons
        )

    asyncio.create_task(worker())

@bot.on(events.CallbackQuery(pattern=b'stop_scan'))
async def stop_scan_cb(event):
    scan_state[event.sender_id]["status"] = "Stopped"
    await event.answer("Scan stopped!")

@bot.on(events.NewMessage(pattern='/done'))
async def finish_batch(event):
    uid = event.sender_id
    if uid not in ADMIN_IDS or uid not in admin_batch_mode: return
    batch = admin_batch_mode[uid]
    me = await bot.get_me()
    await event.respond(f"✅ **Batch Link Generated ({len(batch)} files):**\nhttps://t.me/{me.username}?start=BATCH_{'_AND_'.join(batch)}")
    del admin_batch_mode[uid]


async def drip_scheduler():
    """Runs forever; once a day (per store's configured HH:MM) sends N fresh files to subscribers."""
    while True:
        try:
            now = time.localtime()
            now_str = time.strftime("%H:%M", now)
            today_str = time.strftime("%Y-%m-%d", now)
            for key, cfg in list(drip_config.items()):
                if not cfg.get("enabled"):
                    continue
                if cfg.get("last_sent_date") == today_str:
                    continue
                if now_str < cfg["time"]:
                    continue
                admin_uid, store_name = cfg["admin_uid"], cfg["store"]
                subs = drip_subscribers.get(key, set())
                if not subs:
                    cfg["last_sent_date"] = today_str  # nothing to do today, don't retry until tomorrow
                    save_db()
                    continue
                files = list(stores.get(admin_uid, {}).get(store_name, {}).values())
                sent_ids = drip_sent_ids[key]
                fresh = [f for f in files if f['id'] not in sent_ids][:cfg["count"]]
                if not fresh:
                    cfg["last_sent_date"] = today_str
                    save_db()
                    continue
                for target_uid in list(subs):
                    try:
                        await bot.send_message(target_uid, f"📅 **Today's new drop in {store_name}:**")
                        for f in fresh:
                            await send_stored_media_to_uid(target_uid, f['id'])
                            await asyncio.sleep(0.4)
                    except Exception as e:
                        print(f"Drip send failed for {target_uid}: {e}")
                for f in fresh:
                    sent_ids.add(f['id'])
                cfg["last_sent_date"] = today_str
                save_db()
        except Exception as e:
            print("Drip scheduler error:", e)
        await asyncio.sleep(60)


async def send_stored_media_to_uid(target_uid, media_id):
    """Like send_stored_media, but pushes to a uid directly (no incoming event) for scheduled drip sends."""
    caption_text = CUSTOM_CAPTION if CUSTOM_CAPTION else ""
    for uid_key, user_stores in stores.items():
        for store_name, file_dict in user_stores.items():
            if media_id in file_dict:
                target = file_dict[media_id]
                try:
                    msg = await bot.get_messages(target['chat_id'], ids=target['msg_id'])
                    if msg and msg.media:
                        await bot.send_file(target_uid, msg.media, caption=caption_text, noforwards=True)
                        bump_views(target)
                        return True
                except Exception:
                    pass
                client = user_clients.get(uid_key)
                if client:
                    try:
                        async with jit_locks[uid_key]:
                            if uid_key not in bot_entity_cache:
                                bot_entity_cache[uid_key] = await client.get_entity(BOT_USERNAME)
                            bot_entity = bot_entity_cache[uid_key]
                            fetch_peer = session_owner_ids.get(uid_key, uid_key)
                            wait_task = asyncio.create_task(wait_for_forward(fetch_peer, timeout=10))
                            await client.forward_messages(bot_entity, target['msg_id'], target['chat_id'])
                            r_msg = await wait_task
                            if r_msg and r_msg.media:
                                await bot.send_file(target_uid, r_msg.media, caption=caption_text, noforwards=True)
                                bump_views(target)
                                return True
                    except Exception as e:
                        print(f"Drip JIT send failed: {e}")
    return False


async def init_bot_username():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    print(f"Bot started as @{BOT_USERNAME}")

async def reconnect_saved_sessions():
    # Seed the hardcoded session in (only if that admin doesn't already have one saved)
    if STRING_SESSION and ADMIN_IDS and ADMIN_IDS[0] not in admin_sessions:
        admin_sessions[ADMIN_IDS[0]] = STRING_SESSION
        save_db()

    for uid, session_str in list(admin_sessions.items()):
        try:
            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                user_clients[uid] = client
                who = await client.get_me()
                session_owner_ids[uid] = who.id
                print(f"✅ Session connected for admin {uid} (logged in as {who.first_name}, id={who.id})")
            else:
                print(f"⚠️ Saved session for admin {uid} is no longer authorized")
        except Exception as e:
            print(f"❌ Failed to reconnect session for admin {uid}: {e}")

if __name__ == '__main__':
    print("Database & JIT Forwarding System Online...")
    bot.start(bot_token=BOT_TOKEN)
    bot.loop.run_until_complete(init_bot_username())
    bot.loop.run_until_complete(reconnect_saved_sessions())
    bot.loop.create_task(drip_scheduler())
    bot.run_until_disconnected()

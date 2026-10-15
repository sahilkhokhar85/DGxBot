import asyncio
import csv
import html
import io
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("premium-bot")

# ------------------------------------------------------------------ CONFIG
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(",", " ").split()}
DB_PATH = os.getenv("DB_PATH", "bot.db")
UPI_ID = os.getenv("UPI_ID", "luffytaroxzmanish@fam")
CONTACT_URL = "https://t.me/DGHelps_Bot"
IST = ZoneInfo("Asia/Kolkata")
HOUR = 3600
DAY = 86400
REMINDER_BEFORE_DAYS = 2

# Plans (price ₹ mein). Sab payable products par yehi plans lagenge.
# Kisi product ke liye alag chahiye toh uske dict mein "plans": [...] daal do.
PLANS = [
    {"id": "60d", "label": "🥇 60 Days", "secs": 60 * DAY, "price": 79},
    {"id": "30d", "label": "🥈 30 Days", "secs": 30 * DAY, "price": 39},
    {"id": "7d", "label": "🥉 7 Days", "secs": 7 * DAY, "price": 19},
    {"id": "3h", "label": "⚜️ 3 Hours", "secs": 3 * HOUR, "price": 9},
]

# channel_env: us product ke channel ki ID jis Railway variable mein hogi.
PRODUCTS = {
    "movies": {
        "name": "🎬📺 Movies + TV Shows Premium",
        "terms": "• Payment non-refundable\n• Sirf personal use\n• Link share karna mana hai",
        "channel_env": "CH_MOVIES",
    },
    "courses": {
        "name": "📚 DMT Courses (Adding Soon)",
        "terms": "• Courses jald add honge\n• Details ke liye admin se contact karein: @DGHelps_Bot",
        "channel_env": "CH_COURSES",
        "contact": True,
    },
    "music": {
        "name": "🎧 DMT Immersive Music",
        "terms": "• Payment non-refundable\n• Sirf personal use\n• Link share karna mana hai",
        "channel_env": "CH_MUSIC",
    },
}

# Admin "Change duration" ke options (seconds, label)
DURATION_CHOICES = [
    (3 * HOUR, "3h"),
    (7 * DAY, "7d"),
    (15 * DAY, "15d"),
    (30 * DAY, "30d"),
    (60 * DAY, "60d"),
    (90 * DAY, "90d"),
    (180 * DAY, "180d"),
    (365 * DAY, "365d"),
]


def plans_of(key):
    return PRODUCTS[key].get("plans", PLANS)


def get_plan(key, pid):
    for p in plans_of(key):
        if p["id"] == pid:
            return p
    return None


def pname(key):
    return PRODUCTS.get(key, {}).get("name", key)


def channel_of(key):
    v = os.getenv(PRODUCTS[key]["channel_env"], "").strip().strip("\"'").strip()
    try:
        return int(v)
    except ValueError:
        return None


# ---------------------------------------------------------------------- DB
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, username TEXT, full_name TEXT,
                product TEXT, status TEXT DEFAULT 'pending',
                file_id TEXT, created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS subs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, product TEXT,
                start_ts INTEGER, expiry_ts INTEGER,
                active INTEGER DEFAULT 1, reminded INTEGER DEFAULT 0,
                UNIQUE(user_id, product)
            );
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT
            );
            CREATE TABLE IF NOT EXISTS banned(user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        # naye columns (purane database par bhi safe)
        for tbl, col, typ in [
            ("requests", "plan_secs", "INTEGER"),
            ("requests", "plan_label", "TEXT"),
            ("requests", "plan_price", "INTEGER"),
            ("users", "first_seen", "INTEGER"),
            ("users", "last_seen", "INTEGER"),
        ]:
            try:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        # purane 'tv' plan ko 'movies' mein merge karo (ab dono ek hi channel)
        c.execute("UPDATE OR IGNORE subs SET product='movies' WHERE product='tv'")
        c.execute("DELETE FROM subs WHERE product='tv'")
        c.execute("UPDATE requests SET product='movies' WHERE product='tv'")


def get_setting(key):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def set_setting(key, value):
    with conn() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def touch_user(u):
    now = int(time.time())
    with conn() as c:
        c.execute(
            "INSERT INTO users(user_id,username,full_name,first_seen,last_seen) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
            "full_name=excluded.full_name, last_seen=excluded.last_seen",
            (u.id, u.username, u.full_name, now, now),
        )


def fmt(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %Y, %I:%M %p")


def fmt_dur(secs):
    if secs < DAY:
        return f"{max(1, secs // HOUR)} Hours"
    return f"{secs // DAY} Days"


def fmt_left(secs):
    if secs >= DAY:
        return f"{secs // DAY} din"
    if secs >= HOUR:
        return f"{secs // HOUR} ghante"
    return f"{max(1, secs // 60)} minute"


def esc(s):
    return html.escape(str(s) if s is not None else "")


def is_admin(uid):
    return uid in ADMIN_IDS


def is_banned(uid):
    if is_admin(uid):
        return False
    with conn() as c:
        return c.execute("SELECT 1 FROM banned WHERE user_id=?", (uid,)).fetchone() is not None


def all_channels():
    return sorted({channel_of(k) for k in PRODUCTS if channel_of(k)})


async def make_invite(context, key, ttl=DAY):
    ch = channel_of(key)
    if not ch:
        raise RuntimeError(
            f"{PRODUCTS[key]['channel_env']} Railway mein set nahi hai ya galat hai"
        )
    link = await context.bot.create_chat_invite_link(
        ch,
        member_limit=1,
        expire_date=datetime.now(timezone.utc) + timedelta(seconds=max(ttl, 60)),
    )
    return link.invite_link


def link_ttl_text(ttl):
    return f"{max(1, ttl // HOUR)} ghante" if ttl >= HOUR else "kuch minute"


# -------------------------------------------------------------------- MENUS
HOME_TEXT = (
    "👋 <b>Welcome!</b>\n\nNeeche se apna premium plan choose karein 👇\n\n"
    "🗄 <b>Database channel</b> ke liye admin ko contact karein."
)


def home_markup():
    rows = [
        [InlineKeyboardButton(p["name"], callback_data=f"p:{k}")]
        for k, p in PRODUCTS.items()
    ]
    rows.append([InlineKeyboardButton("💬 Contact Admin (Database)", url=CONTACT_URL)])
    rows.append([InlineKeyboardButton("📋 My Subscriptions", callback_data="my")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    touch_user(u)
    if is_banned(u.id):
        await update.effective_message.reply_text("🚫 Aap is bot ko use nahi kar sakte.")
        return
    context.user_data.pop("buying", None)
    await update.effective_message.reply_text(
        HOME_TEXT, reply_markup=home_markup(), parse_mode=ParseMode.HTML
    )


async def show_home(q):
    await q.edit_message_text(
        HOME_TEXT, reply_markup=home_markup(), parse_mode=ParseMode.HTML
    )


async def show_product(q, key):
    """Page 1: plans + prices"""
    p = PRODUCTS[key]
    if p.get("contact"):
        text = f"<b>{p['name']}</b>\n\n📜 <b>Terms:</b>\n{esc(p['terms'])}"
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💬 Contact Admin", url=CONTACT_URL)],
                [InlineKeyboardButton("⬅️ Back", callback_data="home")],
            ]
        )
        await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return
    lines = [
        f"🎗 <b>VIP ACCESS PLANS</b> 🎗\n<b>{p['name']}</b>",
        f"\n📜 <b>Terms:</b>\n{esc(p['terms'])}",
        "\n👇 Apna plan select karein",
    ]
    rows = [
        [
            InlineKeyboardButton(
                f"{pl['label']} — ₹{pl['price']}", callback_data=f"plan:{key}:{pl['id']}"
            )
        ]
        for pl in plans_of(key)
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    await q.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML
    )


async def show_payment(q, context, key, pid):
    """Page 2: payment method"""
    pl = get_plan(key, pid)
    if not pl:
        return
    context.user_data["buying"] = {"key": key, "plan": pid}
    has_qr = bool(get_setting("qr_file_id"))
    text = (
        f"<b>{PRODUCTS[key]['name']}</b>\n"
        f"📦 Plan: <b>{pl['label']} — ₹{pl['price']}</b>\n\n"
        "<b>Select Your Payout METHOD</b>\n\n"
        '1. UPI :- "Tap👇Karke Copy Karein"\n'
        f"→ <code>{esc(UPI_ID)}</code>\n\n"
    )
    n = 2
    rows = []
    if has_qr:
        text += f'{n}. Scan QR Code 👇\nView QR: => "QR-CODE"\n\n'
        rows.append([InlineKeyboardButton("QR-CODE", callback_data="qr")])
        n += 1
    text += f"{n}. Binance Pay :- (Adding Soon)\n\n"
    text += "(Payment ke baad yahin <b>screenshot (photo)</b> bhejein)"
    rows.append([InlineKeyboardButton("⬅️ Back / Cancel", callback_data=f"p:{key}")])
    await q.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML
    )


async def my_subs_text(user_id):
    now = int(time.time())
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM subs WHERE user_id=? AND active=1 AND expiry_ts>?",
            (user_id, now),
        ).fetchall()
    if not rows:
        return "Aapka koi active subscription nahi hai."
    lines = ["<b>📋 Aapke active plans:</b>\n"]
    for r in rows:
        lines.append(
            f"{pname(r['product'])}\n   Expiry: {fmt(r['expiry_ts'])} ({fmt_left(r['expiry_ts'] - now)} baaki)"
        )
    return "\n".join(lines)


async def mysubs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update.effective_user)
    await update.message.reply_text(
        await my_subs_text(update.effective_user.id), parse_mode=ParseMode.HTML
    )


# ----------------------------------------------------------- USER CALLBACKS
async def user_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if is_banned(q.from_user.id):
        await q.answer("🚫 Aap banned hain", show_alert=True)
        return
    if q.data == "qr":
        fid = get_setting("qr_file_id")
        if not fid:
            await q.answer("QR abhi available nahi hai. UPI se payment karein.", show_alert=True)
            return
        await q.answer()
        cap = "📷 QR scan karke payment karein"
        buying = context.user_data.get("buying")
        if buying:
            pl = get_plan(buying["key"], buying["plan"])
            if pl:
                cap += f" — {pl['label']} ₹{pl['price']}"
        cap += "\n\nPayment ke baad yahin screenshot (photo) bhejein."
        await context.bot.send_photo(q.from_user.id, fid, caption=cap)
        return
    await q.answer()
    touch_user(q.from_user)
    data = q.data

    if data == "home":
        context.user_data.pop("buying", None)
        await show_home(q)
    elif data == "my":
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="home")]]
        )
        await q.edit_message_text(
            await my_subs_text(q.from_user.id),
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    elif data.startswith("p:"):
        key = data[2:]
        if key in PRODUCTS:
            context.user_data.pop("buying", None)
            await show_product(q, key)
    elif data.startswith("plan:"):
        _, key, pid = data.split(":")
        if key in PRODUCTS and not PRODUCTS[key].get("contact"):
            await show_payment(q, context, key, pid)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if is_banned(u.id):
        return
    touch_user(u)
    buying = context.user_data.get("buying")
    if not buying:
        await update.message.reply_text("Pehle /start karke plan choose karein.")
        return
    key = buying["key"]
    pl = get_plan(key, buying["plan"])
    file_id = update.message.photo[-1].file_id
    with conn() as c:
        cur = c.execute(
            "INSERT INTO requests(user_id,username,full_name,product,file_id,created_at,"
            "plan_secs,plan_label,plan_price) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                u.id, u.username, u.full_name, key, file_id, int(time.time()),
                pl["secs"], pl["label"], pl["price"],
            ),
        )
        req_id = cur.lastrowid
    context.user_data.pop("buying", None)

    await update.message.reply_text(
        "✅ Screenshot mil gaya! Admin verify karke jaldi approve karenge."
    )
    caption = (
        f"🧾 <b>New Payment Request #{req_id}</b>\n\n"
        f"👤 {esc(u.full_name)} (@{esc(u.username) or '-'})\n"
        f"🆔 <code>{u.id}</code>\n"
        f"📦 {PRODUCTS[key]['name']}\n"
        f"⏳ Plan: {pl['label']} — ₹{pl['price']}"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"✅ Approve ({fmt_dur(pl['secs'])})", callback_data=f"ap:{req_id}")],
            [
                InlineKeyboardButton("⏱ Change duration", callback_data=f"chg:{req_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"rj:{req_id}"),
            ],
        ]
    )
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                admin, file_id, caption=caption, reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.warning("Admin %s ko send nahi hua: %s", admin, e)


# ---------------------------------------------------------- ADMIN CALLBACKS
def get_req(req_id):
    with conn() as c:
        return c.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()


def duration_keyboard(req_id):
    row, rows = [], []
    for secs, label in DURATION_CHOICES:
        row.append(InlineKeyboardButton(label, callback_data=f"dur:{req_id}:{secs}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Custom days", callback_data=f"cd:{req_id}")])
    rows.append([InlineKeyboardButton("❌ Reject", callback_data=f"rj:{req_id}")])
    return InlineKeyboardMarkup(rows)


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Sirf admin ke liye", show_alert=True)
        return
    await q.answer()
    parts = q.data.split(":")
    action, req_id = parts[0], int(parts[1])
    req = get_req(req_id)
    if not req or req["status"] != "pending":
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
        await q.message.reply_text(f"Request #{req_id} pehle hi handle ho chuki hai.")
        return

    if action == "ap":
        if req["plan_secs"]:
            await finalize(q, context, req_id, int(req["plan_secs"]))
        else:  # purani request jisme plan nahi tha
            await q.edit_message_reply_markup(duration_keyboard(req_id))

    elif action == "chg":
        await q.edit_message_reply_markup(duration_keyboard(req_id))

    elif action == "cd":
        context.user_data["await_days"] = req_id
        await q.message.reply_text(
            f"Request #{req_id}: din ki sankhya number mein bhejein (e.g. 45)"
        )

    elif action == "dur":
        await finalize(q, context, req_id, int(parts[2]))

    elif action == "rj":
        with conn() as c:
            c.execute("UPDATE requests SET status='rejected' WHERE id=?", (req_id,))
        await q.edit_message_caption(
            (q.message.caption_html or "") + "\n\n❌ <b>Rejected</b>",
            parse_mode=ParseMode.HTML,
        )
        try:
            await context.bot.send_message(
                req["user_id"],
                "❌ Aapka payment verify nahi ho paya. Sahi screenshot ke saath dobara try karein ya admin se contact karein.",
            )
        except Exception:
            pass


async def finalize(q, context, req_id, secs):
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "UPDATE requests SET status='approved' WHERE id=? AND status='pending'",
            (req_id,),
        )
        if cur.rowcount == 0:
            return "Request pehle hi handle ho chuki hai."
        req = c.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
        key, uid = req["product"], req["user_id"]
        sub = c.execute(
            "SELECT * FROM subs WHERE user_id=? AND product=?", (uid, key)
        ).fetchone()
        if sub and sub["active"] and sub["expiry_ts"] > now:
            start, base = sub["start_ts"], sub["expiry_ts"]  # renew: extend
        else:
            start, base = now, now
        expiry = base + secs
        reminded = 1 if expiry - now <= REMINDER_BEFORE_DAYS * DAY else 0
        if sub:
            c.execute(
                "UPDATE subs SET start_ts=?, expiry_ts=?, active=1, reminded=? WHERE id=?",
                (start, expiry, reminded, sub["id"]),
            )
        else:
            c.execute(
                "INSERT INTO subs(user_id,product,start_ts,expiry_ts,reminded) VALUES(?,?,?,?,?)",
                (uid, key, start, expiry, reminded),
            )

    text = (
        f"✅ <b>Payment approved!</b>\n\n📦 {PRODUCTS[key]['name']}\n"
        f"🗓 Joining: {fmt(start)}\n⏳ Expiry: <b>{fmt(expiry)}</b>\n\n"
    )
    link_note = ""
    if os.getenv(PRODUCTS[key]["channel_env"], "").strip():
        try:
            ttl = min(DAY, expiry - now)
            link = await make_invite(context, key, ttl)
            text += f"🔗 Join link (sirf 1 banda, {link_ttl_text(ttl)} valid):\n{link}"
        except Exception as e:
            log.error("Invite link fail: %s", e)
            link_note = f"\n⚠️ Invite link nahi bana: {e}\nManually link bhejna padega (/resend {uid} {key})"
            text += "Admin aapko jaldi channel link bhej denge."
    else:
        link_note = f"\n⚠️ {PRODUCTS[key]['channel_env']} set nahi hai, isliye link nahi bana (/resend {uid} {key})"
        text += "Admin aapko jaldi access de denge."
    try:
        await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        link_note += f"\n⚠️ User ko message nahi gaya: {e}"

    summary = f"✅ <b>Approved</b> — {fmt_dur(secs)}, expiry {fmt(expiry)}{esc(link_note)}"
    if q is not None:
        await q.edit_message_caption(
            (q.message.caption_html or "") + "\n\n" + summary,
            parse_mode=ParseMode.HTML,
        )
    return summary


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # admin custom-days input
    if is_admin(update.effective_user.id) and context.user_data.get("await_days"):
        req_id = context.user_data["await_days"]
        t = update.message.text.strip()
        if not t.isdigit() or not (0 < int(t) <= 3650):
            await update.message.reply_text("Sahi number bhejein (1-3650).")
            return
        context.user_data.pop("await_days")
        res = await finalize(None, context, req_id, int(t) * DAY)
        await update.message.reply_text(res, parse_mode=ParseMode.HTML)
        return
    if not is_banned(update.effective_user.id):
        touch_user(update.effective_user)
        await update.message.reply_text("/start dabake menu kholein.")


# ------------------------------------------------------------ ADMIN COMMANDS
async def subs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    now = int(time.time())
    with conn() as c:
        rows = c.execute(
            "SELECT s.*, u.username, u.full_name FROM subs s LEFT JOIN users u USING(user_id)"
            " WHERE s.active=1 AND s.expiry_ts>? ORDER BY s.expiry_ts",
            (now,),
        ).fetchall()
    if not rows:
        await update.message.reply_text("Koi active subscriber nahi.")
        return
    lines = [f"<b>Active subscribers ({len(rows)})</b>\n"]
    for r in rows:
        lines.append(
            f"• {esc(r['full_name'])} (@{esc(r['username']) or '-'}) <code>{r['user_id']}</code>\n"
            f"  {pname(r['product'])} → {fmt(r['expiry_ts'])}"
        )
    text = "\n".join(lines)
    for i in range(0, len(text), 3800):
        await update.message.reply_text(text[i : i + 3800], parse_mode=ParseMode.HTML)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    now = int(time.time())
    with conn() as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        pending = c.execute(
            "SELECT COUNT(*) FROM requests WHERE status='pending'"
        ).fetchone()[0]
        per = c.execute(
            "SELECT product, COUNT(*) n FROM subs WHERE active=1 AND expiry_ts>? GROUP BY product",
            (now,),
        ).fetchall()
        paid_n, paid_sum = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(plan_price),0) FROM requests WHERE status='approved'"
        ).fetchone()
        banned = c.execute("SELECT COUNT(*) FROM banned").fetchone()[0]
    lines = [
        f"👥 Total users: {users}",
        f"🚫 Banned: {banned}",
        f"⏳ Pending requests: {pending}",
        f"💰 Approved payments: {paid_n} (₹{paid_sum})",
        "",
        "Active subs:",
    ]
    lines += [f"• {pname(r['product'])}: {r['n']}" for r in per] or ["• none"]
    await update.message.reply_text("\n".join(lines))


async def user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/user <user_id> -> user ki poori detail"""
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Use: /user <user_id>")
        return
    uid = int(context.args[0])
    now = int(time.time())
    with conn() as c:
        u = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        subs = c.execute("SELECT * FROM subs WHERE user_id=?", (uid,)).fetchall()
        n_req, paid = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(plan_price),0) FROM requests WHERE user_id=? AND status='approved'",
            (uid,),
        ).fetchone()
        banned = c.execute("SELECT 1 FROM banned WHERE user_id=?", (uid,)).fetchone()
    if not u and not subs:
        await update.message.reply_text("Ye user database mein nahi mila.")
        return
    lines = [f"🆔 {uid}"]
    if u:
        lines.append(f"👤 {u['full_name']} (@{u['username'] or '-'})")
        if u["first_seen"]:
            lines.append(f"📅 Pehli baar: {fmt(u['first_seen'])}")
        if u["last_seen"]:
            lines.append(f"🕒 Last active: {fmt(u['last_seen'])}")
    lines.append(f"🚫 Banned: {'Haan' if banned else 'Nahi'}")
    lines.append(f"💰 Approved payments: {n_req} (₹{paid})")
    lines.append("")
    if subs:
        lines.append("Subscriptions:")
        for s in subs:
            live = s["active"] and s["expiry_ts"] > now
            lines.append(
                f"• {pname(s['product'])}: {'ACTIVE' if live else 'expired'} — {fmt(s['expiry_ts'])}"
            )
    else:
        lines.append("Koi subscription nahi.")
    await update.message.reply_text("\n".join(lines))


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saare users ka CSV (database backup jaisa)"""
    if not is_admin(update.effective_user.id):
        return
    now = int(time.time())
    with conn() as c:
        users = c.execute("SELECT * FROM users").fetchall()
        subs = c.execute(
            "SELECT * FROM subs WHERE active=1 AND expiry_ts>?", (now,)
        ).fetchall()
        paid = {
            r["user_id"]: r["t"]
            for r in c.execute(
                "SELECT user_id, COALESCE(SUM(plan_price),0) t FROM requests "
                "WHERE status='approved' GROUP BY user_id"
            )
        }
        banned = {r[0] for r in c.execute("SELECT user_id FROM banned")}
    active = {}
    for s in subs:
        active.setdefault(s["user_id"], []).append(
            f"{pname(s['product'])} till {fmt(s['expiry_ts'])}"
        )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["user_id", "username", "name", "first_seen", "last_seen", "banned", "total_paid", "active_plans"]
    )
    for u in users:
        w.writerow(
            [
                u["user_id"],
                u["username"] or "",
                u["full_name"] or "",
                fmt(u["first_seen"]) if u["first_seen"] else "",
                fmt(u["last_seen"]) if u["last_seen"] else "",
                "yes" if u["user_id"] in banned else "no",
                paid.get(u["user_id"], 0),
                " | ".join(active.get(u["user_id"], [])),
            ]
        )
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    await update.message.reply_document(
        data, filename="users.csv", caption=f"👥 {len(users)} users"
    )


async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/revoke <user_id> <product_key>  -> turant kick + subscription band"""
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 2 or context.args[1] not in PRODUCTS or not context.args[0].isdigit():
        await update.message.reply_text(
            "Use: /revoke <user_id> <product>\nProducts: " + ", ".join(PRODUCTS)
        )
        return
    uid, key = int(context.args[0]), context.args[1]
    await kick(context, uid, key)
    with conn() as c:
        c.execute(
            "UPDATE subs SET active=0 WHERE user_id=? AND product=?", (uid, key)
        )
    await update.message.reply_text("Done ✅")


async def resend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resend <user_id> <product> -> active subscriber ko naya join link"""
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 2 or context.args[1] not in PRODUCTS or not context.args[0].isdigit():
        await update.message.reply_text(
            "Use: /resend <user_id> <product>\nProducts: " + ", ".join(PRODUCTS)
        )
        return
    uid, key = int(context.args[0]), context.args[1]
    now = int(time.time())
    with conn() as c:
        sub = c.execute(
            "SELECT * FROM subs WHERE user_id=? AND product=? AND active=1 AND expiry_ts>?",
            (uid, key, now),
        ).fetchone()
    if not sub:
        await update.message.reply_text("Is user ka is plan mein active subscription nahi hai.")
        return
    try:
        ttl = min(DAY, sub["expiry_ts"] - now)
        link = await make_invite(context, key, ttl)
        await context.bot.send_message(
            uid,
            f"🔗 {pname(key)} join link (sirf 1 banda, {link_ttl_text(ttl)} valid):\n{link}",
        )
        await update.message.reply_text("Link bhej diya ✅")
    except Exception as e:
        await update.message.reply_text(f"❌ Fail: {e}")


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ban <user_id> -> sab channels se ban + bot use band + subscriptions band"""
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Use: /ban <user_id>")
        return
    uid = int(context.args[0])
    if is_admin(uid):
        await update.message.reply_text("Admin ko ban nahi kar sakte.")
        return
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO banned VALUES(?)", (uid,))
        c.execute("UPDATE subs SET active=0 WHERE user_id=?", (uid,))
    lines = [f"🚫 {uid} banned (bot + subscriptions band)."]
    for ch in all_channels():
        try:
            await context.bot.ban_chat_member(ch, uid)
            lines.append(f"✅ channel {ch}: banned")
        except Exception as e:
            lines.append(f"❌ channel {ch}: {e}")
    await update.message.reply_text("\n".join(lines))


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unban <user_id> -> channels se unban + bot use wapas"""
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Use: /unban <user_id>")
        return
    uid = int(context.args[0])
    with conn() as c:
        c.execute("DELETE FROM banned WHERE user_id=?", (uid,))
    lines = [f"✅ {uid} unbanned."]
    for ch in all_channels():
        try:
            await context.bot.unban_chat_member(ch, uid, only_if_banned=True)
            lines.append(f"✅ channel {ch}: unbanned")
        except Exception as e:
            lines.append(f"❌ channel {ch}: {e}")
    await update.message.reply_text("\n".join(lines))


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/broadcast <text>  ya  kisi message ko reply karke /broadcast (photo/video bhi chalega)"""
    if not is_admin(update.effective_user.id):
        return
    msg = update.message
    reply = msg.reply_to_message
    parts = msg.text.split(maxsplit=1)
    text = parts[1] if len(parts) > 1 else ""
    if not reply and not text:
        await msg.reply_text(
            "Use:\n/broadcast <message>\nYa kisi message ko reply karke /broadcast likho."
        )
        return
    with conn() as c:
        ids = [
            r[0]
            for r in c.execute(
                "SELECT user_id FROM users WHERE user_id NOT IN (SELECT user_id FROM banned)"
            )
        ]
    await msg.reply_text(f"📢 Broadcast shuru: {len(ids)} users...")

    async def run():
        sent = failed = 0
        for uid in ids:
            for _ in range(2):
                try:
                    if reply:
                        await context.bot.copy_message(uid, reply.chat_id, reply.message_id)
                    else:
                        await context.bot.send_message(uid, text)
                    sent += 1
                    break
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception:
                    failed += 1
                    break
            await asyncio.sleep(0.05)
        await context.bot.send_message(
            update.effective_user.id, f"📢 Broadcast done ✅\nSent: {sent}\nFailed: {failed}"
        )

    context.application.create_task(run())


async def checkchannels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Har channel ka status: ID set hai? bot admin hai? invite/ban permission hai?"""
    if not is_admin(update.effective_user.id):
        return
    me = await context.bot.get_me()
    lines = []
    for key, p in PRODUCTS.items():
        env = p["channel_env"]
        ch = channel_of(key)
        if not ch:
            lines.append(f"• {p['name']}: {env} set nahi hai")
            continue
        try:
            chat = await context.bot.get_chat(ch)
            m = await context.bot.get_chat_member(ch, me.id)
            inv = getattr(m, "can_invite_users", None)
            ban = getattr(m, "can_restrict_members", None)
            lines.append(
                f"• {p['name']}: {chat.title} ({ch})\n   bot={m.status}, invite={inv}, ban={ban}"
            )
        except Exception as e:
            lines.append(f"• {p['name']}: ❌ {ch} -> {e}")
    await update.message.reply_text("\n".join(lines))


async def setqr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """QR photo par reply karke /setqr likho"""
    if not is_admin(update.effective_user.id):
        return
    r = update.message.reply_to_message
    if not r or not r.photo:
        await update.message.reply_text(
            "QR ki photo bhejo aur caption mein /setqr likho, ya photo par reply karke /setqr likho."
        )
        return
    set_setting("qr_file_id", r.photo[-1].file_id)
    await update.message.reply_text("✅ QR set ho gaya. Ab payment page par QR-CODE button dikhega.")


async def setqr_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photo bhejo caption /setqr ke saath (sirf admin)"""
    set_setting("qr_file_id", update.message.photo[-1].file_id)
    await update.message.reply_text("✅ QR set ho gaya. Ab payment page par QR-CODE button dikhega.")


async def removeqr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with conn() as c:
        c.execute("DELETE FROM settings WHERE key='qr_file_id'")
    await update.message.reply_text("QR hata diya.")


async def forwarded_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin kisi channel ka message bot ko forward kare -> channel ID mil jayegi"""
    if not is_admin(update.effective_user.id):
        return
    origin = getattr(update.message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    if not chat:
        await update.message.reply_text(
            "Ye message kisi channel se forward nahi hua. Channel ka message forward karo."
        )
        return
    await update.message.reply_text(f"📡 {chat.title}\nID: {chat.id}")


async def channel_id_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Channel mein /id likho (bot admin ho) -> admins ko DM mein channel ID aa jayegi"""
    post = update.channel_post
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(admin, f"📡 {post.chat.title}\nID: {post.chat.id}")
        except Exception:
            pass
    try:
        await post.delete()
    except Exception:
        pass


# --------------------------------------------------------------- EXPIRY JOB
async def kick(context, uid, key):
    ch = channel_of(key)
    if not ch:
        return
    try:
        await context.bot.ban_chat_member(ch, uid)
        # unban taaki renew karne par wapas join kar sake
        await context.bot.unban_chat_member(ch, uid, only_if_banned=True)
    except Exception as e:
        log.warning("Kick fail %s/%s: %s", uid, key, e)


async def expiry_job(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    with conn() as c:
        expired = c.execute(
            "SELECT * FROM subs WHERE active=1 AND expiry_ts<=?", (now,)
        ).fetchall()
        remind = c.execute(
            "SELECT * FROM subs WHERE active=1 AND reminded=0 AND expiry_ts>? AND expiry_ts<=?",
            (now, now + REMINDER_BEFORE_DAYS * DAY),
        ).fetchall()

    for r in remind:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 Renew", callback_data=f"p:{r['product']}")]]
        )
        try:
            await context.bot.send_message(
                r["user_id"],
                f"⚠️ Aapka <b>{pname(r['product'])}</b> subscription "
                f"<b>{fmt(r['expiry_ts'])}</b> ko expire hoga. Renew kar lein!",
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        with conn() as c:
            c.execute("UPDATE subs SET reminded=1 WHERE id=?", (r["id"],))

    for r in expired:
        key, uid = r["product"], r["user_id"]
        await kick(context, uid, key)
        with conn() as c:
            c.execute("UPDATE subs SET active=0 WHERE id=?", (r["id"],))
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 Re-new Subscription", callback_data=f"p:{key}")]]
        )
        try:
            await context.bot.send_message(
                uid,
                f"⏰ Aapka <b>{pname(key)}</b> subscription expire ho gaya hai.\n"
                "Dobara access ke liye please <b>re-new your subscription</b> 👇",
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        for admin in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin,
                    f"⏰ Expired & removed: <code>{uid}</code> — {pname(key)}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass


# --------------------------------------------------------------------- MAIN
def main():
    if not ADMIN_IDS:
        raise SystemExit("ADMIN_IDS env variable set karo")
    init_db()
    for k, p in PRODUCTS.items():
        log.info("Channel %s (%s) = %s", p["channel_env"], k, channel_of(k))
    app = Application.builder().token(BOT_TOKEN).build()

    for name, fn in [
        ("start", start),
        ("mysubs", mysubs_cmd),
        ("subs", subs_cmd),
        ("stats", stats_cmd),
        ("user", user_cmd),
        ("export", export_cmd),
        ("revoke", revoke_cmd),
        ("resend", resend_cmd),
        ("ban", ban_cmd),
        ("unban", unban_cmd),
        ("broadcast", broadcast_cmd),
        ("checkchannels", checkchannels_cmd),
        ("setqr", setqr_cmd),
        ("removeqr", removeqr_cmd),
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^(ap|rj|cd|dur|chg):"))
    app.add_handler(CallbackQueryHandler(user_cb))
    app.add_handler(
        MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, forwarded_id)
    )
    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POSTS & filters.Regex(r"^/id(@\w+)?$"),
            channel_id_post,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.PHOTO
            & filters.CaptionRegex(r"^/setqr")
            & filters.User(user_id=list(ADMIN_IDS)),
            setqr_photo,
        )
    )
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.job_queue.run_repeating(expiry_job, interval=60, first=10)
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

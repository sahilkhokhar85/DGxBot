import html
import logging
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
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
PAYMENT_INFO = os.getenv(
    "PAYMENT_INFO", "UPI ID: yourupi@bank\n(Payment ke baad screenshot yahin bhejein)"
)
CONTACT_URL = "https://t.me/DGHelps_Bot"
IST = ZoneInfo("Asia/Kolkata")
DAY = 86400

# Yahan apne products edit karo (naam, price, terms, duration options).
# channel_env: us product ke channel ki ID jis env variable mein hogi (e.g. CH_MOVIES).
# Agar env set nahi hai toh approve ke baad bot sirf user ko notify karega (manual delivery).
PRODUCTS = {
    "movies": {
        "name": "🎬 INSTANT Movies",
        "price": "₹99 / month",
        "terms": "• Payment non-refundable\n• Sirf personal use\n• Link share karna mana hai",
        "channel_env": "CH_MOVIES",
        "default_days": 30,
    },
    "tv": {
        "name": "📺 TV SHOW + Serial",
        "price": "₹99 / month",
        "terms": "• Payment non-refundable\n• Sirf personal use\n• Link share karna mana hai",
        "channel_env": "CH_TV",
        "default_days": 30,
    },
    "courses": {
        "name": "📚 DMT Courses",
        "price": "Adding Soon",
        "terms": "• Courses jald add honge\n• Details ke liye admin se contact karein: @DGHelps_Bot",
        "channel_env": "CH_COURSES",
        "default_days": 30,
        "contact": True,
    },
    "music": {
        "name": "🎧 DMT Immersive Music",
        "price": "₹149 / month",
        "terms": "• Payment non-refundable\n• Sirf personal use",
        "channel_env": "CH_MUSIC",
        "default_days": 30,
    },
}
DURATION_OPTIONS = [7, 15, 30, 60, 90, 180, 365]
REMINDER_BEFORE_DAYS = 2


def channel_of(key):
    v = os.getenv(PRODUCTS[key]["channel_env"], "").strip()
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
            """
        )


def fmt(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %Y, %I:%M %p")


def esc(s):
    return html.escape(s or "")


def is_admin(uid):
    return uid in ADMIN_IDS


# -------------------------------------------------------------------- MENUS
def home_markup():
    rows = []
    for k, p in PRODUCTS.items():
        if p.get("url"):
            rows.append([InlineKeyboardButton(p["name"], url=p["url"])])
        else:
            rows.append([InlineKeyboardButton(p["name"], callback_data=f"p:{k}")])
    rows.append([InlineKeyboardButton("💬 Contact Admin", url=CONTACT_URL)])
    rows.append([InlineKeyboardButton("📋 My Subscriptions", callback_data="my")])
    return InlineKeyboardMarkup(rows)


HOME_TEXT = "👋 <b>Welcome!</b>\n\nNeeche se apna premium plan choose karein 👇"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO users VALUES(?,?,?)",
            (u.id, u.username, u.full_name),
        )
    context.user_data.pop("buying", None)
    await update.effective_message.reply_text(
        HOME_TEXT, reply_markup=home_markup(), parse_mode=ParseMode.HTML
    )


async def show_home(q):
    await q.edit_message_text(
        HOME_TEXT, reply_markup=home_markup(), parse_mode=ParseMode.HTML
    )


async def show_product(q, key):
    p = PRODUCTS[key]
    text = (
        f"<b>{p['name']}</b>\n\n"
        f"💰 <b>Price:</b> {esc(p['price'])}\n\n"
        f"📜 <b>Terms:</b>\n{esc(p['terms'])}"
    )
    if p.get("contact"):
        action = InlineKeyboardButton("💬 Contact Admin", url=CONTACT_URL)
    else:
        action = InlineKeyboardButton("💳 Buy Now", callback_data=f"buy:{key}")
    kb = InlineKeyboardMarkup(
        [[action], [InlineKeyboardButton("⬅️ Back", callback_data="home")]]
    )
    await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


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
        left = max(0, (r["expiry_ts"] - now) // DAY)
        lines.append(
            f"{PRODUCTS[r['product']]['name']}\n   Expiry: {fmt(r['expiry_ts'])} ({left} din baaki)"
        )
    return "\n".join(lines)


async def mysubs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        await my_subs_text(update.effective_user.id), parse_mode=ParseMode.HTML
    )


# ----------------------------------------------------------- USER CALLBACKS
async def user_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
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
        await show_product(q, data[2:])
    elif data.startswith("buy:"):
        key = data[4:]
        if PRODUCTS[key].get("contact") or PRODUCTS[key].get("url"):
            return
        context.user_data["buying"] = key
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=f"p:{key}")]]
        )
        await q.edit_message_text(
            f"<b>{PRODUCTS[key]['name']}</b>\n💰 {esc(PRODUCTS[key]['price'])}\n\n"
            f"{esc(PAYMENT_INFO)}\n\n"
            "📸 Payment karne ke baad <b>screenshot (photo)</b> yahin bhejein.",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.get("buying")
    if not key:
        await update.message.reply_text("Pehle /start karke plan choose karein.")
        return
    u = update.effective_user
    file_id = update.message.photo[-1].file_id
    with conn() as c:
        cur = c.execute(
            "INSERT INTO requests(user_id,username,full_name,product,file_id,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (u.id, u.username, u.full_name, key, file_id, int(time.time())),
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
        f"📦 {PRODUCTS[key]['name']}\n💰 {esc(PRODUCTS[key]['price'])}"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"ap:{req_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"rj:{req_id}"),
            ]
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
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"Request #{req_id} pehle hi handle ho chuki hai.")
        return

    if action == "ap":
        default = PRODUCTS[req["product"]]["default_days"]
        row, rows = [], []
        for d in DURATION_OPTIONS:
            label = f"{d}d" + (" ⭐" if d == default else "")
            row.append(InlineKeyboardButton(label, callback_data=f"dur:{req_id}:{d}"))
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("✏️ Custom days", callback_data=f"cd:{req_id}")])
        rows.append([InlineKeyboardButton("❌ Reject", callback_data=f"rj:{req_id}")])
        await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
        await q.message.reply_text("⏳ Subscription kitne din ki set karni hai?")

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


async def finalize(q, context, req_id, days, from_text_msg=None):
    req = get_req(req_id)
    if not req or req["status"] != "pending":
        return "Request pehle hi handle ho chuki hai."
    key, uid = req["product"], req["user_id"]
    now = int(time.time())

    with conn() as c:
        sub = c.execute(
            "SELECT * FROM subs WHERE user_id=? AND product=?", (uid, key)
        ).fetchone()
        if sub and sub["active"] and sub["expiry_ts"] > now:
            start, base = sub["start_ts"], sub["expiry_ts"]  # renew: extend
        else:
            start, base = now, now
        expiry = base + days * DAY
        if sub:
            c.execute(
                "UPDATE subs SET start_ts=?, expiry_ts=?, active=1, reminded=0 WHERE id=?",
                (start, expiry, sub["id"]),
            )
        else:
            c.execute(
                "INSERT INTO subs(user_id,product,start_ts,expiry_ts) VALUES(?,?,?,?)",
                (uid, key, start, expiry),
            )
        c.execute("UPDATE requests SET status='approved' WHERE id=?", (req_id,))

    text = (
        f"✅ <b>Payment approved!</b>\n\n📦 {PRODUCTS[key]['name']}\n"
        f"🗓 Joining: {fmt(start)}\n⏳ Expiry: <b>{fmt(expiry)}</b>\n\n"
    )
    ch = channel_of(key)
    link_note = ""
    if ch:
        try:
            link = await context.bot.create_chat_invite_link(
                ch, member_limit=1, expire_date=now + DAY
            )
            text += f"🔗 Join link (1 baar, 24 ghante valid):\n{link.invite_link}"
        except Exception as e:
            log.error("Invite link fail: %s", e)
            link_note = "\n⚠️ Invite link nahi ban paya (bot channel mein admin hai?). Manually bhejna padega."
            text += "Admin aapko jaldi channel link bhej denge."
    else:
        text += "Admin aapko jaldi access de denge."
    try:
        await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        link_note += f"\n⚠️ User ko message nahi gaya: {e}"

    summary = f"✅ <b>Approved</b> — {days} din, expiry {fmt(expiry)}{esc(link_note)}"
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
        res = await finalize(None, context, req_id, int(t))
        await update.message.reply_text(res, parse_mode=ParseMode.HTML)
        return
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
            f"  {PRODUCTS[r['product']]['name']} → {fmt(r['expiry_ts'])}"
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
    lines = [f"👥 Total users: {users}", f"⏳ Pending requests: {pending}", "", "Active subs:"]
    lines += [f"• {PRODUCTS[r['product']]['name']}: {r['n']}" for r in per] or ["• none"]
    await update.message.reply_text("\n".join(lines))


async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/revoke <user_id> <product_key>  -> turant kick + subscription band"""
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 2 or context.args[1] not in PRODUCTS:
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
                f"⚠️ Aapka <b>{PRODUCTS[r['product']]['name']}</b> subscription "
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
                f"⏰ Aapka <b>{PRODUCTS[key]['name']}</b> subscription expire ho gaya hai.\n"
                "Dobara access ke liye please <b>re-new your subscription</b> 👇",
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        for admin in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin, f"⏰ Expired & removed: <code>{uid}</code> — {PRODUCTS[key]['name']}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass


# --------------------------------------------------------------------- MAIN
def main():
    if not ADMIN_IDS:
        raise SystemExit("ADMIN_IDS env variable set karo")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mysubs", mysubs_cmd))
    app.add_handler(CommandHandler("subs", subs_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("revoke", revoke_cmd))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^(ap|rj|cd|dur):"))
    app.add_handler(CallbackQueryHandler(user_cb))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.job_queue.run_repeating(expiry_job, interval=60, first=10)
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

"""
Smart Earning Telegram Bot
Python 3.10+ | python-telegram-bot 22.x

Setup:
1. Install: pip install -U python-telegram-bot
2. Put your BotFather token in BOT_TOKEN below.
3. Keep this file in a writable folder and run: python bot.py
4. Add the bot as an administrator in both required channels so membership checks work.

Note: This bot tracks balances and withdrawal requests in SQLite. It does not send real
payments automatically; an admin must pay approved withdrawals manually.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from html import escape

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -------------------- CONFIGURATION --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")

ADMIN_IDS = {8268732754}

REQUIRED_CHANNELS = [
    ("@SmartEarningAF", "Smart Earning Channel"),
    ("@smartearningafg", "Smart Earning Updates"),
]

REFERRAL_REWARD = 0.5
MIN_WITHDRAWAL = 30.0
DATABASE = os.getenv("DATABASE_PATH", "smart_earning.db")
WITHDRAWAL_CHANNEL = "@smartearningafg"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# -------------------- DATABASE --------------------
def db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                balance REAL NOT NULL DEFAULT 0,
                referred_by INTEGER,
                referral_paid INTEGER NOT NULL DEFAULT 0,
                joined_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                account TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                processed_at TEXT DEFAULT '',
                processed_by INTEGER
            )
        """)


def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def get_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def ensure_user(tg_user, referrer_id=None):
    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM users WHERE user_id=?", (tg_user.id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET username=?, full_name=? WHERE user_id=?",
                (tg_user.username or "", tg_user.full_name or "", tg_user.id),
            )
            return False

        if referrer_id == tg_user.id:
            referrer_id = None
        if referrer_id and not conn.execute(
            "SELECT 1 FROM users WHERE user_id=?", (referrer_id,)
        ).fetchone():
            referrer_id = None

        conn.execute("""
            INSERT INTO users(user_id, username, full_name, referred_by, joined_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            tg_user.id,
            tg_user.username or "",
            tg_user.full_name or "",
            referrer_id,
            now_text(),
        ))
        return True


def is_admin(user_id):
    return user_id in ADMIN_IDS


def get_referral_count(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE referred_by=? AND referral_paid=1",
            (user_id,),
        ).fetchone()
        return int(row["n"])


def get_pending_withdrawals():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM withdrawals WHERE status='pending' ORDER BY id ASC"
        ).fetchall()


def get_stats():
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        balance = conn.execute("SELECT COALESCE(SUM(balance),0) n FROM users").fetchone()["n"]
        pending = conn.execute(
            "SELECT COUNT(*) n FROM withdrawals WHERE status='pending'"
        ).fetchone()["n"]
        approved = conn.execute(
            "SELECT COUNT(*) n FROM withdrawals WHERE status='approved'"
        ).fetchone()["n"]
        return int(users), float(balance), int(pending), int(approved)


def get_users_page(page=0, page_size=20):
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        rows = conn.execute(
            "SELECT user_id, username, full_name, balance, joined_at FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",
            (page_size, page * page_size),
        ).fetchall()
        return int(total), rows


# -------------------- KEYBOARDS --------------------
def main_keyboard(user_id):
    rows = [
        ["👤 My Profile", "💰 My Balance"],
        ["👥 Referral Link", "💸 Withdraw"],
        ["📊 Statistics", "ℹ️ Help"],
    ]
    if is_admin(user_id):
        rows.append(["🛠 Admin Panel"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def channel_keyboard(user_id):
    rows = []
    for channel, title in REQUIRED_CHANNELS:
        clean = channel.lstrip("@")
        rows.append([InlineKeyboardButton(f"Join {title}", url=f"https://t.me/{clean}")])
    rows.append([InlineKeyboardButton("✅ I Joined — Verify", callback_data="verify_membership")])
    return InlineKeyboardMarkup(rows)


def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Pending Withdrawals", callback_data="admin_pending"),
            InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("➕ Add Balance", callback_data="admin_add"),
            InlineKeyboardButton("➖ Remove Balance", callback_data="admin_remove"),
        ],
        [InlineKeyboardButton("👥 All Users", callback_data="admin_users_0")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="admin_broadcast")],
    ])


# -------------------- MEMBERSHIP / REFERRALS --------------------
async def check_membership(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    for channel, _ in REQUIRED_CHANNELS:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ("left", "kicked", "banned"):
                return False
        except TelegramError as exc:
            logger.warning("Could not check membership for %s: %s", channel, exc)
            return False
    return True


async def pay_referral_if_eligible(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        row = conn.execute(
            "SELECT referred_by, referral_paid FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row or not row["referred_by"] or row["referral_paid"]:
            return
        referrer_id = int(row["referred_by"])
        conn.execute(
            "UPDATE users SET referral_paid=1 WHERE user_id=? AND referral_paid=0",
            (user_id,),
        )
        conn.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (REFERRAL_REWARD, referrer_id),
        )
    try:
        await context.bot.send_message(
            referrer_id,
            f"🎉 Referral reward received!\nYou earned {REFERRAL_REWARD:.2f} AFG.",
        )
    except TelegramError:
        pass


async def show_membership_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Welcome to Smart Earning!\n\n"
        "To use the bot, please join both required channels, then tap "
        "“I Joined — Verify”."
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=channel_keyboard(update.effective_user.id))
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=channel_keyboard(update.effective_user.id))


# -------------------- USER COMMANDS --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referrer_id = None
    if context.args:
        try:
            candidate = int(context.args[0])
            if candidate != user.id:
                referrer_id = candidate
        except ValueError:
            pass
    ensure_user(user, referrer_id)

    if not await check_membership(context, user.id):
        await show_membership_prompt(update, context)
        return

    await pay_referral_if_eligible(user.id, context)
    await update.effective_message.reply_text(
        f"Welcome, {escape(user.first_name or 'User')}! 👋\n\n"
        "Your Smart Earning account is ready.",
        reply_markup=main_keyboard(user.id),
        parse_mode=ParseMode.HTML,
    )


async def require_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await check_membership(context, update.effective_user.id):
        return True
    await show_membership_prompt(update, context)
    return False


async def verify_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await check_membership(context, query.from_user.id):
        await query.message.reply_text(
            "❌ Membership not detected yet. Join both channels and try again.",
            reply_markup=channel_keyboard(query.from_user.id),
        )
        return
    await pay_referral_if_eligible(query.from_user.id, context)
    await query.message.reply_text(
        "✅ Membership verified! You can now use Smart Earning.",
        reply_markup=main_keyboard(query.from_user.id),
    )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, context):
        return
    user = get_user(update.effective_user.id)
    referrals = get_referral_count(update.effective_user.id)
    username = f"@{user['username']}" if user and user["username"] else "Not set"
    await update.effective_message.reply_text(
        "👤 MY PROFILE\n\n"
        f"Name: {user['full_name'] if user else update.effective_user.full_name}\n"
        f"Username: {username}\n"
        f"User ID: {update.effective_user.id}\n"
        f"Successful referrals: {referrals}"
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, context):
        return
    user = get_user(update.effective_user.id)
    await update.effective_message.reply_text(
        f"💰 Your current balance is: {float(user['balance'] if user else 0):.2f} AFG"
    )


async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, context):
        return
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={update.effective_user.id}"
    await update.effective_message.reply_text(
        "👥 YOUR REFERRAL LINK\n\n"
        f"{link}\n\n"
        f"Reward per eligible referral: {REFERRAL_REWARD:.2f} AFG\n"
        "A referral is counted after the new user joins both required channels."
    )


async def statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, context):
        return
    users, _, pending, approved = get_stats()
    await update.effective_message.reply_text(
        "📊 BOT STATISTICS\n\n"
        f"Registered users: {users}\n"
        f"Your successful referrals: {get_referral_count(update.effective_user.id)}\n"
        f"Minimum withdrawal: {MIN_WITHDRAWAL:.2f} AFG\n"
        f"Pending withdrawal requests: {pending}\n"
        f"Approved withdrawals: {approved}"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, context):
        return
    await update.effective_message.reply_text(
        "ℹ️ HELP\n\n"
        "• Use My Profile to view your account details.\n"
        "• Use My Balance to check your available balance.\n"
        "• Share your referral link to invite new users.\n"
        f"• Minimum withdrawal: {MIN_WITHDRAWAL:.2f} AFG.\n"
        "• Withdrawal requests are reviewed by an administrator.\n"
        "• Never share your Telegram login code or password."
    )


# -------------------- WITHDRAWAL FLOW --------------------
async def begin_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, context):
        return
    user = get_user(update.effective_user.id)
    available = float(user["balance"] if user else 0)
    if available < MIN_WITHDRAWAL:
        await update.effective_message.reply_text(
            f"❌ Your balance is {available:.2f} AFG.\n"
            f"You need at least {MIN_WITHDRAWAL:.2f} AFG to request a withdrawal."
        )
        return
    context.user_data["state"] = "withdraw_amount"
    await update.effective_message.reply_text(
        f"💸 WITHDRAWAL REQUEST\n\n"
        f"Available balance: {available:.2f} AFG\n"
        f"Minimum withdrawal: {MIN_WITHDRAWAL:.2f} AFG\n\n"
        "Enter the amount you want to withdraw:"
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Access denied.")
        return
    await update.effective_message.reply_text("🛠 ADMIN PANEL", reply_markup=admin_keyboard())


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user_id = update.effective_user.id
    text = (message.text or "").strip()
    state = context.user_data.get("state")

    # Admin multi-step actions
    if is_admin(user_id) and state in {"admin_add_id", "admin_add_amount", "admin_remove_id",
                                      "admin_remove_amount", "admin_broadcast"}:
        if state == "admin_broadcast":
            context.user_data.pop("state", None)
            sent = 0
            with db() as conn:
                ids = [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]
            for uid in ids:
                try:
                    await context.bot.send_message(uid, f"📣 ANNOUNCEMENT\n\n{text}")
                    sent += 1
                except TelegramError:
                    continue
            await message.reply_text(f"Broadcast completed. Sent to {sent} users.")
            return
        if state in {"admin_add_id", "admin_remove_id"}:
            try:
                target_id = int(text)
                if not get_user(target_id):
                    await message.reply_text("User not found. Enter a registered Telegram user ID:")
                    return
            except ValueError:
                await message.reply_text("Please enter a valid numeric Telegram user ID:")
                return
            context.user_data["target_id"] = target_id
            context.user_data["state"] = "admin_add_amount" if state == "admin_add_id" else "admin_remove_amount"
            await message.reply_text("Enter the amount in AFG:")
            return
        if state in {"admin_add_amount", "admin_remove_amount"}:
            try:
                amount = float(text)
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await message.reply_text("Enter a valid amount greater than zero:")
                return
            target_id = int(context.user_data["target_id"])
            adding = state == "admin_add_amount"
            with db() as conn:
                if adding:
                    conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, target_id))
                else:
                    row = conn.execute("SELECT balance FROM users WHERE user_id=?", (target_id,)).fetchone()
                    if not row or float(row["balance"]) < amount:
                        await message.reply_text("The user does not have enough balance.")
                        return
                    conn.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, target_id))
            context.user_data.pop("state", None)
            context.user_data.pop("target_id", None)
            await message.reply_text(
                f"✅ {'Added' if adding else 'Removed'} {amount:.2f} AFG for user {target_id}."
            )
            try:
                await context.bot.send_message(
                    target_id,
                    f"ℹ️ Admin {'added' if adding else 'removed'} {amount:.2f} AFG "
                    "to/from your account balance.",
                )
            except TelegramError:
                pass
            return

    # Withdrawal multi-step flow
    if state == "withdraw_amount":
        try:
            amount = float(text)
            if amount < MIN_WITHDRAWAL:
                await message.reply_text(f"Minimum withdrawal is {MIN_WITHDRAWAL:.2f} AFG. Enter another amount:")
                return
            user = get_user(user_id)
            if not user or amount > float(user["balance"]):
                await message.reply_text("Insufficient balance. Enter an amount within your available balance:")
                return
        except ValueError:
            await message.reply_text("Please enter a valid number, for example: 30")
            return
        context.user_data["withdraw_amount"] = amount
        context.user_data["state"] = "withdraw_method"
        await message.reply_text(
            "Choose a withdrawal method by typing one:\n"
            "1. USDT (TRC20)\n"
            "2. Bank / Mobile Wallet\n"
            "3. Other"
        )
        return

    if state == "withdraw_method":
        context.user_data["withdraw_method"] = text
        context.user_data["state"] = "withdraw_account"
        await message.reply_text("Enter your wallet address or payment account details:")
        return

    if state == "withdraw_account":
        amount = float(context.user_data.get("withdraw_amount", 0))
        method = context.user_data.get("withdraw_method", "Other")
        account = text
        # Reserve/deduct balance now to prevent double spending.
        with db() as conn:
            row = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not row or float(row["balance"]) < amount:
                context.user_data.clear()
                await message.reply_text("❌ Your balance changed and is now insufficient. Please try again.")
                return
            conn.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, user_id))
            conn.execute("""
                INSERT INTO withdrawals(user_id, amount, method, account, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
            """, (user_id, amount, method, account, now_text()))
            withdrawal_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        context.user_data.pop("state", None)
        context.user_data.pop("withdraw_amount", None)
        context.user_data.pop("withdraw_method", None)

        await message.reply_text(
            f"✅ Withdrawal request #{withdrawal_id} submitted.\n"
            f"Amount: {amount:.2f} AFG\nMethod: {method}\n"
            "Your request is pending admin review."
        )
        admin_text = (
            f"💸 NEW WITHDRAWAL REQUEST #{withdrawal_id}\n\n"
            f"User: {user_id}\nName: {update.effective_user.full_name}\n"
            f"Amount: {amount:.2f} AFG\nMethod: {method}\n"
            f"Account: {account}\nTime: {now_text()}"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{withdrawal_id}"),
            InlineKeyboardButton("❌ Reject & Refund", callback_data=f"wd_reject_{withdrawal_id}"),
        ]])
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(admin_id, admin_text, reply_markup=keyboard)
            except TelegramError as exc:
                logger.warning("Could not notify admin %s: %s", admin_id, exc)
        return

    # Main menu buttons
    if text == "👤 My Profile":
        await profile(update, context)
    elif text == "💰 My Balance":
        await balance(update, context)
    elif text == "👥 Referral Link":
        await referral(update, context)
    elif text == "💸 Withdraw":
        await begin_withdraw(update, context)
    elif text == "📊 Statistics":
        await statistics(update, context)
    elif text == "ℹ️ Help":
        await help_command(update, context)
    elif text == "🛠 Admin Panel":
        await admin_panel(update, context)
    else:
        if not await require_membership(update, context):
            return
        await message.reply_text("Please choose an option from the menu below.", reply_markup=main_keyboard(user_id))


# -------------------- CALLBACKS --------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user_id = query.from_user.id

    if data == "verify_membership":
        await verify_membership(update, context)
        return

    if data.startswith("wd_"):
        if not is_admin(user_id):
            await query.answer("Access denied.", show_alert=True)
            return
        parts = data.split("_")
        action = parts[1]
        try:
            withdrawal_id = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("Invalid request.", show_alert=True)
            return
        with db() as conn:
            row = conn.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
            if not row:
                await query.answer("Withdrawal not found.", show_alert=True)
                return
            if row["status"] != "pending":
                await query.answer("This request has already been processed.", show_alert=True)
                return
            status = "approved" if action == "approve" else "rejected"
            conn.execute("""
                UPDATE withdrawals SET status=?, processed_at=?, processed_by=? WHERE id=?
            """, (status, now_text(), user_id, withdrawal_id))
            if action == "reject":
                conn.execute(
                    "UPDATE users SET balance=balance+? WHERE user_id=?",
                    (float(row["amount"]), int(row["user_id"])),
                )
        await query.answer("Request processed.")
        await query.edit_message_text(
            (query.message.text or "") + f"\n\nStatus: {'APPROVED' if action == 'approve' else 'REJECTED — REFUNDED'}"
        )
        if action == "approve":
            try:
                target = get_user(int(row["user_id"]))
                username = f"@{target['username']}" if target and target["username"] else "No username"
                await context.bot.send_message(
                    chat_id=WITHDRAWAL_CHANNEL,
                    text=(
                        f"✅ APPROVED WITHDRAWAL #{withdrawal_id}\n\n"
                        f"User ID: {row['user_id']}\n"
                        f"Name: {target['full_name'] if target else 'Unknown'}\n"
                        f"Username: {username}\n"
                        f"Amount: {float(row['amount']):.2f} AFG\n"
                        f"Method: {row['method']}\n"
                        f"Account: {row['account']}\n"
                        f"Approved at: {now_text()}\n"
                        "Status: Approved by admin"
                    ),
                )
            except TelegramError as exc:
                logger.warning("Could not post approved withdrawal to channel %s: %s", WITHDRAWAL_CHANNEL, exc)
                await query.message.reply_text(
                    "⚠️ Withdrawal approved, but posting to the withdrawal channel failed. "
                    "Make sure the bot is an administrator in @smartearningafg."
                )
        try:
            await context.bot.send_message(
                int(row["user_id"]),
                f"💸 Withdrawal request #{withdrawal_id} has been "
                f"{'approved. Please allow time for payment processing.' if action == 'approve' else 'rejected. The amount has been refunded to your balance.'}",
            )
        except TelegramError:
            pass
        return

    if data.startswith("admin_"):
        if not is_admin(user_id):
            await query.answer("Access denied.", show_alert=True)
            return
        await query.answer()
        if data.startswith("admin_users_"):
            try:
                page = max(0, int(data.rsplit("_", 1)[1]))
            except ValueError:
                page = 0
            total, rows = get_users_page(page)
            if not rows and page > 0:
                await query.message.reply_text("No more users on this page.")
                return
            lines = [f"👥 ALL REGISTERED USERS\nTotal users: {total}\nPage: {page + 1}\n"]
            for row in rows:
                username = f"@{row['username']}" if row['username'] else "No username"
                lines.append(
                    f"\n👤 {row['full_name'] or 'User'}\n"
                    f"ID: {row['user_id']} | {username}\n"
                    f"Balance: {float(row['balance']):.2f} AFG\n"
                    f"Joined: {row['joined_at']}"
                )
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"admin_users_{page-1}"))
            if (page + 1) * 20 < total:
                nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_users_{page+1}"))
            markup = InlineKeyboardMarkup([nav]) if nav else None
            await query.message.reply_text("\n".join(lines), reply_markup=markup)
        elif data == "admin_stats":
            users, balance_total, pending, approved = get_stats()
            await query.message.reply_text(
                "📊 ADMIN STATISTICS\n\n"
                f"Registered users: {users}\n"
                f"Total user balances: {balance_total:.2f} AFG\n"
                f"Pending withdrawals: {pending}\n"
                f"Approved withdrawals: {approved}"
            )
        elif data == "admin_pending":
            rows = get_pending_withdrawals()
            if not rows:
                await query.message.reply_text("There are no pending withdrawal requests.")
            else:
                for row in rows[:30]:
                    text = (
                        f"💸 REQUEST #{row['id']}\n"
                        f"User ID: {row['user_id']}\n"
                        f"Amount: {float(row['amount']):.2f} AFG\n"
                        f"Method: {row['method']}\n"
                        f"Account: {row['account']}\n"
                        f"Time: {row['created_at']}"
                    )
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{row['id']}"),
                        InlineKeyboardButton("❌ Reject & Refund", callback_data=f"wd_reject_{row['id']}"),
                    ]])
                    await query.message.reply_text(text, reply_markup=keyboard)
        elif data == "admin_add":
            context.user_data["state"] = "admin_add_id"
            await query.message.reply_text("Enter the registered Telegram user ID to add balance:")
        elif data == "admin_remove":
            context.user_data["state"] = "admin_remove_id"
            await query.message.reply_text("Enter the registered Telegram user ID to remove balance:")
        elif data == "admin_broadcast":
            context.user_data["state"] = "admin_broadcast"
            await query.message.reply_text("Send the announcement text to broadcast to all registered users:")
        return

    await query.answer()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled bot error: %s", context.error)


def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit(
            "BOT_TOKEN is not set. Open bot.py and replace PASTE_YOUR_BOT_TOKEN_HERE "
            "with your token from @BotFather."
        )

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    logger.info("Smart Earning bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

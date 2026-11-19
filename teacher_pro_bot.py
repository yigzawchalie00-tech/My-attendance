"""
Teacher Pro Bot
===============
A Telegram bot for Ethiopian teachers nationwide.

FEATURES:
  FREE (3-day trial):
    - Register profile (name, school, subject, grade, phone)
    - Try all features during trial

  PAID (20 birr/month):
    - Attendance: take & view class attendance
    - Salary tracker: log salary, advances, deductions
    - Payment history

  SUPER ADMIN:
    - View all teachers
    - Approve/reject payment screenshots
    - Suspend/activate accounts
    - Income summary

PAYMENT:
  Manual via Telebirr: 0900170922
  Teacher sends screenshot → admin approves → unlocked

ENV VARS:
  TG_BOT_TOKEN=        (from @BotFather)
  SUPER_ADMIN_ID=      (your Telegram numeric ID)
  DATABASE_URL=        (Postgres from Neon.tech, recommended)
  DB_PATH=./teacher_pro.db  (SQLite fallback for local testing)
  PORT=10000

DEPLOY:
  Build:  pip install -r requirements.txt
  Start:  python teacher_pro_bot.py
"""

import os
import io
import re
import sqlite3
import threading
from datetime import datetime, date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", "0"))
DB_PATH = os.environ.get("DB_PATH", "./teacher_pro.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

PAYMENT_NUMBER = "0900170922"
MONTHLY_FEE = 20
TRIAL_DAYS = 3

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras


# ── Database ──────────────────────────────────────────────────────────────────

class _PGConn:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def db():
    if USE_POSTGRES:
        return _PGConn(psycopg2.connect(DATABASE_URL))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    serial = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"

    conn.execute("""
        CREATE TABLE IF NOT EXISTS teachers (
            telegram_id   BIGINT PRIMARY KEY,
            name          TEXT,
            school        TEXT,
            subject       TEXT,
            grade         TEXT,
            phone         TEXT,
            registered_at TEXT,
            trial_ends    TEXT,
            status        TEXT DEFAULT 'trial',
            suspended     INTEGER DEFAULT 0
        )
    """)
    # status: 'trial' | 'active' | 'expired'

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS payments (
            id            {serial},
            telegram_id   BIGINT,
            amount        INTEGER,
            submitted_at  TEXT,
            approved_at   TEXT,
            approved_by   BIGINT,
            status        TEXT DEFAULT 'pending',
            months        INTEGER DEFAULT 1,
            file_id       TEXT
        )
    """)
    # status: 'pending' | 'approved' | 'rejected'

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS attendance (
            id            {serial},
            telegram_id   BIGINT,
            class_name    TEXT,
            att_date      TEXT,
            present       INTEGER DEFAULT 0,
            absent        INTEGER DEFAULT 0,
            late          INTEGER DEFAULT 0,
            notes         TEXT,
            created_at    TEXT
        )
    """)

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS salary_records (
            id            {serial},
            telegram_id   BIGINT,
            month         TEXT,
            base_salary   REAL,
            advance       REAL DEFAULT 0,
            deduction     REAL DEFAULT 0,
            bonus         REAL DEFAULT 0,
            notes         TEXT,
            created_at    TEXT
        )
    """)

    conn.commit()
    conn.close()
    print(f"[db] {'Postgres' if USE_POSTGRES else 'SQLite (' + DB_PATH + ')'}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_teacher(telegram_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM teachers WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return row


def is_access_ok(teacher) -> bool:
    """Returns True if teacher can use paid features."""
    if not teacher:
        return False
    if teacher["suspended"]:
        return False
    if teacher["status"] == "active":
        return True
    if teacher["status"] == "trial":
        trial_ends = teacher["trial_ends"]
        if trial_ends and date.today().isoformat() <= trial_ends:
            return True
    return False


def days_left_trial(teacher) -> int:
    if not teacher or teacher["status"] != "trial":
        return 0
    trial_ends = teacher["trial_ends"]
    if not trial_ends:
        return 0
    delta = date.fromisoformat(trial_ends) - date.today()
    return max(0, delta.days)


def trial_banner(teacher) -> str:
    if teacher["status"] == "trial":
        d = days_left_trial(teacher)
        if d > 0:
            return f"\n\n⏳ *Free trial: {d} day(s) left.*"
        else:
            return "\n\n⚠️ *Your trial has ended. Subscribe to continue.*"
    return ""


# ── Keyboards ─────────────────────────────────────────────────────────────────

def main_menu(teacher=None):
    if teacher and is_access_ok(teacher):
        return ReplyKeyboardMarkup([
            ["📋 Attendance", "💰 Salary Tracker"],
            ["💳 Subscription", "👤 My Profile"],
            ["ℹ️ Help"],
        ], resize_keyboard=True)
    else:
        return ReplyKeyboardMarkup([
            ["💳 Subscribe", "👤 My Profile"],
            ["ℹ️ Help"],
        ], resize_keyboard=True)


def admin_menu():
    return ReplyKeyboardMarkup([
        ["👥 All Teachers", "⏳ Pending Payments"],
        ["📊 Income Summary", "🔍 Find Teacher"],
        ["👤 My Profile", "ℹ️ Help"],
    ], resize_keyboard=True)


# ── HTTP keep-alive ───────────────────────────────────────────────────────────

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - Teacher Pro Bot running")

    def log_message(self, format, *args):
        pass


def start_ping_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), PingHandler).serve_forever()


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    first = update.effective_user.first_name or "Teacher"

    # Super admin shortcut
    if tid == SUPER_ADMIN_ID:
        await update.message.reply_text(
            f"👋 Welcome back, Admin!\nUse the menu below.",
            reply_markup=admin_menu(),
        )
        return

    teacher = get_teacher(tid)

    if not teacher:
        # New user — start registration
        context.user_data["reg"] = {"step": "name"}
        await update.message.reply_text(
            f"👋 Welcome to *Teacher Pro Bot*!\n\n"
            f"This bot helps Ethiopian teachers track attendance and salary.\n\n"
            f"✅ *{TRIAL_DAYS}-day FREE trial* — no payment needed to start!\n\n"
            f"Let's set up your profile. What is your *full name*?",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # Already registered
    banner = trial_banner(teacher)
    await update.message.reply_text(
        f"👋 Welcome back, *{teacher['name']}*!{banner}\n\n"
        f"Use the menu below.",
        parse_mode="Markdown",
        reply_markup=main_menu(teacher),
    )


# ── Registration flow ─────────────────────────────────────────────────────────

async def handle_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    text = update.message.text.strip()
    reg = context.user_data.get("reg", {})
    step = reg.get("step")

    if step == "name":
        reg["name"] = text
        reg["step"] = "school"
        context.user_data["reg"] = reg
        await update.message.reply_text("🏫 What is your *school name*?", parse_mode="Markdown")

    elif step == "school":
        reg["school"] = text
        reg["step"] = "subject"
        context.user_data["reg"] = reg
        await update.message.reply_text("📚 What *subject* do you teach?", parse_mode="Markdown")

    elif step == "subject":
        reg["subject"] = text
        reg["step"] = "grade"
        context.user_data["reg"] = reg
        await update.message.reply_text("🎓 Which *grade(s)* do you teach? (e.g. Grade 9, 10)", parse_mode="Markdown")

    elif step == "grade":
        reg["grade"] = text
        reg["step"] = "phone"
        context.user_data["reg"] = reg
        await update.message.reply_text("📱 Your *phone number*? (e.g. 0912345678)", parse_mode="Markdown")

    elif step == "phone":
        reg["phone"] = text
        # Save to DB
        now = datetime.now().isoformat(timespec="seconds")
        trial_ends = (date.today() + timedelta(days=TRIAL_DAYS)).isoformat()
        conn = db()
        conn.execute("""
            INSERT INTO teachers (telegram_id, name, school, subject, grade, phone, registered_at, trial_ends, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'trial')
            ON CONFLICT (telegram_id) DO NOTHING
        """, (tid, reg["name"], reg["school"], reg["subject"], reg["grade"], reg["phone"], now, trial_ends))
        conn.commit()
        conn.close()
        context.user_data.pop("reg", None)

        teacher = get_teacher(tid)
        await update.message.reply_text(
            f"✅ *Profile created!*\n\n"
            f"👤 Name: {reg['name']}\n"
            f"🏫 School: {reg['school']}\n"
            f"📚 Subject: {reg['subject']}\n"
            f"🎓 Grade: {reg['grade']}\n"
            f"📱 Phone: {reg['phone']}\n\n"
            f"⏳ Your *{TRIAL_DAYS}-day free trial* starts now!\n"
            f"After trial, subscribe for only *{MONTHLY_FEE} birr/month*.",
            parse_mode="Markdown",
            reply_markup=main_menu(teacher),
        )

        # Notify admin
        if SUPER_ADMIN_ID:
            try:
                await context.bot.send_message(
                    SUPER_ADMIN_ID,
                    f"🆕 New teacher registered!\n\n"
                    f"👤 {reg['name']}\n🏫 {reg['school']}\n📚 {reg['subject']}\n"
                    f"🎓 {reg['grade']}\n📱 {reg['phone']}\nID: {tid}"
                )
            except Exception:
                pass


# ── My Profile ────────────────────────────────────────────────────────────────

async def btn_my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    teacher = get_teacher(tid)
    if not teacher:
        await update.message.reply_text("Please send /start to register first.")
        return

    status = teacher["status"]
    if status == "trial":
        d = days_left_trial(teacher)
        status_line = f"⏳ Free trial — {d} day(s) left"
    elif status == "active":
        status_line = "✅ Active subscriber"
    else:
        status_line = "❌ Expired — please subscribe"

    await update.message.reply_text(
        f"👤 *Your Profile*\n\n"
        f"Name: {teacher['name']}\n"
        f"School: {teacher['school']}\n"
        f"Subject: {teacher['subject']}\n"
        f"Grade: {teacher['grade']}\n"
        f"Phone: {teacher['phone']}\n\n"
        f"Status: {status_line}",
        parse_mode="Markdown",
        reply_markup=main_menu(teacher),
    )


# ── Subscription ──────────────────────────────────────────────────────────────

async def btn_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    teacher = get_teacher(tid)
    if not teacher:
        await update.message.reply_text("Please send /start to register first.")
        return

    # Check pending payment
    conn = db()
    pending = conn.execute(
        "SELECT id FROM payments WHERE telegram_id = ? AND status = 'pending'", (tid,)
    ).fetchone()
    conn.close()

    if pending:
        await update.message.reply_text(
            "⏳ You already have a *pending payment* waiting for approval.\n\n"
            "Please wait for admin to approve it.",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"💳 *Subscribe to Teacher Pro*\n\n"
        f"Monthly fee: *{MONTHLY_FEE} birr*\n\n"
        f"📲 Send payment to:\n"
        f"*Telebirr: {PAYMENT_NUMBER}*\n\n"
        f"After paying:\n"
        f"1. Take a screenshot of the confirmation\n"
        f"2. Send it here as a photo\n\n"
        f"Your account will be activated within a few minutes! ✅",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True),
    )
    context.user_data["awaiting_payment_screenshot"] = True


async def handle_payment_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teacher sends payment screenshot photo."""
    tid = update.effective_user.id
    teacher = get_teacher(tid)
    if not teacher:
        return
    if not context.user_data.get("awaiting_payment_screenshot"):
        return

    photo = update.message.photo
    document = update.message.document

    if photo:
        file_id = photo[-1].file_id
    elif document:
        file_id = document.file_id
    else:
        await update.message.reply_text("Please send a *photo* of your payment receipt.", parse_mode="Markdown")
        return

    now = datetime.now().isoformat(timespec="seconds")
    conn = db()
    conn.execute(
        "INSERT INTO payments (telegram_id, amount, submitted_at, status, months, file_id) VALUES (?, ?, ?, 'pending', 1, ?)",
        (tid, MONTHLY_FEE, now, file_id),
    )
    conn.commit()
    pay_id = conn.execute(
        "SELECT id FROM payments WHERE telegram_id = ? ORDER BY id DESC LIMIT 1", (tid,)
    ).fetchone()["id"]
    conn.close()

    context.user_data.pop("awaiting_payment_screenshot", None)

    await update.message.reply_text(
        "✅ *Payment screenshot received!*\n\n"
        "Admin will approve it shortly. You'll get a notification when activated.",
        parse_mode="Markdown",
        reply_markup=main_menu(teacher),
    )

    # Notify admin with approve/reject buttons
    if SUPER_ADMIN_ID:
        try:
            buttons = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"pay_approve:{pay_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"pay_reject:{pay_id}"),
                ]
            ])
            await context.bot.send_photo(
                SUPER_ADMIN_ID,
                photo=file_id,
                caption=(
                    f"💳 *New Payment Request*\n\n"
                    f"👤 {teacher['name']}\n"
                    f"🏫 {teacher['school']}\n"
                    f"📚 {teacher['subject']}\n"
                    f"💰 {MONTHLY_FEE} birr\n"
                    f"ID: {tid}"
                ),
                parse_mode="Markdown",
                reply_markup=buttons,
            )
        except Exception as e:
            print(f"[notify] Admin notify failed: {e}")


async def on_payment_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin approves or rejects payment."""
    query = update.callback_query
    if update.effective_user.id != SUPER_ADMIN_ID:
        await query.answer("Admins only.", show_alert=True)
        return

    action, pay_id_str = query.data.split(":")
    pay_id = int(pay_id_str)
    await query.answer()

    conn = db()
    pay = conn.execute("SELECT * FROM payments WHERE id = ?", (pay_id,)).fetchone()
    if not pay:
        await query.message.reply_text("Payment not found.")
        conn.close()
        return

    teacher_id = pay["telegram_id"]
    teacher = conn.execute("SELECT * FROM teachers WHERE telegram_id = ?", (teacher_id,)).fetchone()
    now = datetime.now().isoformat(timespec="seconds")

    if action == "pay_approve":
        conn.execute(
            "UPDATE payments SET status = 'approved', approved_at = ?, approved_by = ? WHERE id = ?",
            (now, SUPER_ADMIN_ID, pay_id),
        )
        # Extend active period by 1 month from today
        conn.execute(
            "UPDATE teachers SET status = 'active', suspended = 0 WHERE telegram_id = ?",
            (teacher_id,),
        )
        conn.commit()
        conn.close()

        await query.edit_message_caption(
            caption=query.message.caption + "\n\n✅ *APPROVED*",
            parse_mode="Markdown",
        )

        # Notify teacher
        try:
            t = get_teacher(teacher_id)
            await context.bot.send_message(
                teacher_id,
                f"🎉 *Payment Approved!*\n\n"
                f"Your account is now *active*. Enjoy Teacher Pro Bot!\n\n"
                f"Use the menu to track attendance and salary. 📋💰",
                parse_mode="Markdown",
                reply_markup=main_menu(t),
            )
        except Exception as e:
            print(f"[notify] Teacher notify failed: {e}")

    elif action == "pay_reject":
        conn.execute(
            "UPDATE payments SET status = 'rejected', approved_at = ? WHERE id = ?",
            (now, pay_id),
        )
        conn.commit()
        conn.close()

        await query.edit_message_caption(
            caption=query.message.caption + "\n\n❌ *REJECTED*",
            parse_mode="Markdown",
        )

        try:
            await context.bot.send_message(
                teacher_id,
                f"❌ *Payment Not Approved*\n\n"
                f"Your payment screenshot was not approved.\n\n"
                f"Please make sure you:\n"
                f"• Sent the correct amount ({MONTHLY_FEE} birr)\n"
                f"• Sent to the right number ({PAYMENT_NUMBER})\n"
                f"• Sent a clear screenshot\n\n"
                f"Try again with /subscribe or tap 💳 Subscribe.",
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"[notify] Teacher notify failed: {e}")


async def btn_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show subscription status and history."""
    tid = update.effective_user.id
    teacher = get_teacher(tid)
    if not teacher:
        return

    conn = db()
    payments = conn.execute(
        "SELECT * FROM payments WHERE telegram_id = ? ORDER BY id DESC LIMIT 5", (tid,)
    ).fetchall()
    conn.close()

    status = teacher["status"]
    if status == "trial":
        d = days_left_trial(teacher)
        status_line = f"⏳ Free trial — {d} day(s) left"
    elif status == "active":
        status_line = "✅ Active"
    else:
        status_line = "❌ Expired"

    lines = [f"💳 *Subscription Status*\n\n{status_line}\n\n*Payment History:*"]
    if not payments:
        lines.append("No payments yet.")
    for p in payments:
        emoji = {"approved": "✅", "rejected": "❌", "pending": "⏳"}.get(p["status"], "?")
        lines.append(f"{emoji} {p['submitted_at'][:10]} — {p['amount']} birr ({p['status']})")

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Subscribe Now", callback_data="go_subscribe")]
    ]) if status != "active" else None

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=buttons,
    )


async def on_go_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await btn_subscribe(update, context)


# ── Attendance ─────────────────────────────────────────────────────────────────

async def btn_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    teacher = get_teacher(tid)
    if not teacher or not is_access_ok(teacher):
        await update.message.reply_text(
            "🔒 This feature requires a subscription.\n\nTap 💳 *Subscribe* to unlock.",
            parse_mode="Markdown",
        )
        return

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Record Attendance", callback_data="att_new")],
        [InlineKeyboardButton("📊 View History", callback_data="att_history")],
    ])
    banner = trial_banner(teacher)
    await update.message.reply_text(
        f"📋 *Attendance*{banner}",
        parse_mode="Markdown",
        reply_markup=buttons,
    )


async def on_att_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tid = update.effective_user.id
    teacher = get_teacher(tid)
    if not teacher or not is_access_ok(teacher):
        await query.answer("Subscribe to use this feature.", show_alert=True)
        return
    await query.answer()
    context.user_data["att"] = {"step": "class"}
    await query.message.reply_text(
        "📋 *Record Attendance*\n\nWhat is the class/section name? (e.g. Grade 9A)",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True),
    )


async def on_att_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tid = update.effective_user.id
    await query.answer()

    conn = db()
    rows = conn.execute(
        "SELECT * FROM attendance WHERE telegram_id = ? ORDER BY att_date DESC LIMIT 10",
        (tid,),
    ).fetchall()
    conn.close()

    if not rows:
        await query.message.reply_text("No attendance records yet.")
        return

    lines = ["📊 *Recent Attendance (last 10):*\n"]
    for r in rows:
        lines.append(
            f"📅 {r['att_date']} — {r['class_name']}\n"
            f"   ✅ {r['present']} present  ❌ {r['absent']} absent  🕒 {r['late']} late"
        )
    await query.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


async def handle_attendance_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    text = update.message.text.strip()
    att = context.user_data.get("att", {})
    step = att.get("step")
    teacher = get_teacher(tid)

    if step == "class":
        att["class_name"] = text
        att["step"] = "date"
        context.user_data["att"] = att
        today = date.today().isoformat()
        await update.message.reply_text(
            f"📅 Date? (press Enter for today: {today}, or type another date YYYY-MM-DD)",
            reply_markup=ReplyKeyboardMarkup([[today, "❌ Cancel"]], resize_keyboard=True),
        )

    elif step == "date":
        if re.match(r'^\d{4}-\d{2}-\d{2}$', text):
            att["att_date"] = text
        else:
            att["att_date"] = date.today().isoformat()
        att["step"] = "present"
        context.user_data["att"] = att
        await update.message.reply_text(
            f"✅ How many students were *PRESENT*?",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True),
        )

    elif step == "present":
        if not text.isdigit():
            await update.message.reply_text("Please enter a number.")
            return
        att["present"] = int(text)
        att["step"] = "absent"
        context.user_data["att"] = att
        await update.message.reply_text(
            f"❌ How many were *ABSENT*?",
            parse_mode="Markdown",
        )

    elif step == "absent":
        if not text.isdigit():
            await update.message.reply_text("Please enter a number.")
            return
        att["absent"] = int(text)
        att["step"] = "late"
        context.user_data["att"] = att
        await update.message.reply_text("🕒 How many were *LATE*?", parse_mode="Markdown")

    elif step == "late":
        if not text.isdigit():
            await update.message.reply_text("Please enter a number.")
            return
        att["late"] = int(text)
        att["step"] = "notes"
        context.user_data["att"] = att
        await update.message.reply_text(
            "📝 Any notes? (or send 'skip')",
            reply_markup=ReplyKeyboardMarkup([["skip", "❌ Cancel"]], resize_keyboard=True),
        )

    elif step == "notes":
        notes = "" if text.lower() == "skip" else text
        now = datetime.now().isoformat(timespec="seconds")
        conn = db()
        conn.execute("""
            INSERT INTO attendance (telegram_id, class_name, att_date, present, absent, late, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (tid, att["class_name"], att["att_date"], att["present"], att["absent"], att["late"], notes, now))
        conn.commit()
        conn.close()
        context.user_data.pop("att", None)

        total = att["present"] + att["absent"] + att["late"]
        await update.message.reply_text(
            f"✅ *Attendance Saved!*\n\n"
            f"📅 Date: {att['att_date']}\n"
            f"🏫 Class: {att['class_name']}\n"
            f"👥 Total: {total}\n"
            f"✅ Present: {att['present']}\n"
            f"❌ Absent: {att['absent']}\n"
            f"🕒 Late: {att['late']}\n"
            f"📝 Notes: {notes or '—'}",
            parse_mode="Markdown",
            reply_markup=main_menu(teacher),
        )


# ── Salary Tracker ────────────────────────────────────────────────────────────

async def btn_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    teacher = get_teacher(tid)
    if not teacher or not is_access_ok(teacher):
        await update.message.reply_text(
            "🔒 This feature requires a subscription.\n\nTap 💳 *Subscribe* to unlock.",
            parse_mode="Markdown",
        )
        return

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Salary Record", callback_data="sal_new")],
        [InlineKeyboardButton("📊 View Records", callback_data="sal_history")],
        [InlineKeyboardButton("📈 Summary", callback_data="sal_summary")],
    ])
    banner = trial_banner(teacher)
    await update.message.reply_text(
        f"💰 *Salary Tracker*{banner}",
        parse_mode="Markdown",
        reply_markup=buttons,
    )


async def on_sal_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tid = update.effective_user.id
    teacher = get_teacher(tid)
    if not teacher or not is_access_ok(teacher):
        await query.answer("Subscribe to use this feature.", show_alert=True)
        return
    await query.answer()
    context.user_data["sal"] = {"step": "month"}
    this_month = date.today().strftime("%Y-%m")
    await query.message.reply_text(
        f"💰 *Add Salary Record*\n\nWhich month? (e.g. {this_month})",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[this_month, "❌ Cancel"]], resize_keyboard=True),
    )


async def on_sal_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tid = update.effective_user.id
    await query.answer()

    conn = db()
    rows = conn.execute(
        "SELECT * FROM salary_records WHERE telegram_id = ? ORDER BY month DESC LIMIT 10",
        (tid,),
    ).fetchall()
    conn.close()

    if not rows:
        await query.message.reply_text("No salary records yet.")
        return

    lines = ["💰 *Salary Records (last 10):*\n"]
    for r in rows:
        net = r["base_salary"] + r["bonus"] - r["advance"] - r["deduction"]
        lines.append(
            f"📅 {r['month']}\n"
            f"   Base: {r['base_salary']:,.0f}  Bonus: +{r['bonus']:,.0f}\n"
            f"   Advance: -{r['advance']:,.0f}  Deduction: -{r['deduction']:,.0f}\n"
            f"   💵 Net: {net:,.0f} birr"
        )
    await query.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


async def on_sal_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tid = update.effective_user.id
    await query.answer()

    conn = db()
    rows = conn.execute(
        "SELECT * FROM salary_records WHERE telegram_id = ?", (tid,)
    ).fetchall()
    conn.close()

    if not rows:
        await query.message.reply_text("No salary records yet.")
        return

    total_base = sum(r["base_salary"] for r in rows)
    total_bonus = sum(r["bonus"] for r in rows)
    total_advance = sum(r["advance"] for r in rows)
    total_deduction = sum(r["deduction"] for r in rows)
    total_net = total_base + total_bonus - total_advance - total_deduction

    await query.message.reply_text(
        f"📈 *Salary Summary (all time)*\n\n"
        f"Total Base Salary: {total_base:,.0f} birr\n"
        f"Total Bonus: +{total_bonus:,.0f} birr\n"
        f"Total Advance: -{total_advance:,.0f} birr\n"
        f"Total Deductions: -{total_deduction:,.0f} birr\n\n"
        f"💵 *Total Net: {total_net:,.0f} birr*\n"
        f"📊 Months tracked: {len(rows)}",
        parse_mode="Markdown",
    )


async def handle_salary_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    text = update.message.text.strip()
    sal = context.user_data.get("sal", {})
    step = sal.get("step")
    teacher = get_teacher(tid)

    if step == "month":
        if not re.match(r'^\d{4}-\d{2}$', text):
            await update.message.reply_text("Please use format YYYY-MM (e.g. 2026-11)")
            return
        sal["month"] = text
        sal["step"] = "base"
        context.user_data["sal"] = sal
        await update.message.reply_text(
            "💵 What is your *base salary* (in birr)?",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True),
        )

    elif step == "base":
        try:
            sal["base"] = float(text.replace(",", ""))
        except ValueError:
            await update.message.reply_text("Please enter a number (e.g. 5000)")
            return
        sal["step"] = "advance"
        context.user_data["sal"] = sal
        await update.message.reply_text(
            "💸 Any *advance* taken? (enter 0 if none)",
            parse_mode="Markdown",
        )

    elif step == "advance":
        try:
            sal["advance"] = float(text.replace(",", ""))
        except ValueError:
            await update.message.reply_text("Please enter a number (e.g. 500 or 0)")
            return
        sal["step"] = "deduction"
        context.user_data["sal"] = sal
        await update.message.reply_text(
            "✂️ Any *deductions*? (pension, tax, etc. — enter 0 if none)",
            parse_mode="Markdown",
        )

    elif step == "deduction":
        try:
            sal["deduction"] = float(text.replace(",", ""))
        except ValueError:
            await update.message.reply_text("Please enter a number (e.g. 300 or 0)")
            return
        sal["step"] = "bonus"
        context.user_data["sal"] = sal
        await update.message.reply_text(
            "🎁 Any *bonus*? (enter 0 if none)",
            parse_mode="Markdown",
        )

    elif step == "bonus":
        try:
            sal["bonus"] = float(text.replace(",", ""))
        except ValueError:
            await update.message.reply_text("Please enter a number (e.g. 200 or 0)")
            return
        sal["step"] = "notes"
        context.user_data["sal"] = sal
        await update.message.reply_text(
            "📝 Any notes? (or send 'skip')",
            reply_markup=ReplyKeyboardMarkup([["skip", "❌ Cancel"]], resize_keyboard=True),
        )

    elif step == "notes":
        notes = "" if text.lower() == "skip" else text
        now = datetime.now().isoformat(timespec="seconds")
        net = sal["base"] + sal["bonus"] - sal["advance"] - sal["deduction"]
        conn = db()
        conn.execute("""
            INSERT INTO salary_records (telegram_id, month, base_salary, advance, deduction, bonus, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (tid, sal["month"], sal["base"], sal["advance"], sal["deduction"], sal["bonus"], notes, now))
        conn.commit()
        conn.close()
        context.user_data.pop("sal", None)

        await update.message.reply_text(
            f"✅ *Salary Record Saved!*\n\n"
            f"📅 Month: {sal['month']}\n"
            f"💵 Base: {sal['base']:,.0f} birr\n"
            f"🎁 Bonus: +{sal['bonus']:,.0f} birr\n"
            f"💸 Advance: -{sal['advance']:,.0f} birr\n"
            f"✂️ Deduction: -{sal['deduction']:,.0f} birr\n\n"
            f"💵 *Net: {net:,.0f} birr*",
            parse_mode="Markdown",
            reply_markup=main_menu(teacher),
        )


# ── Admin panel ───────────────────────────────────────────────────────────────

async def admin_all_teachers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPER_ADMIN_ID:
        return
    conn = db()
    rows = conn.execute("SELECT * FROM teachers ORDER BY registered_at DESC").fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No teachers registered yet.")
        return

    total = len(rows)
    active = sum(1 for r in rows if r["status"] == "active")
    trial = sum(1 for r in rows if r["status"] == "trial")
    expired = sum(1 for r in rows if r["status"] == "expired")

    lines = [
        f"👥 *All Teachers ({total})*\n",
        f"✅ Active: {active}  ⏳ Trial: {trial}  ❌ Expired: {expired}\n",
    ]
    for r in rows:
        s = {"active": "✅", "trial": "⏳", "expired": "❌"}.get(r["status"], "?")
        susp = " 🚫" if r["suspended"] else ""
        lines.append(f"{s} {r['name']} — {r['school']}{susp}\n   ID: {r['telegram_id']}")

    # Chunk if too long
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n... (more)"
    await update.message.reply_text(text, parse_mode="Markdown")


async def admin_pending_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPER_ADMIN_ID:
        return
    conn = db()
    rows = conn.execute(
        "SELECT payments.*, teachers.name, teachers.school FROM payments "
        "JOIN teachers ON payments.telegram_id = teachers.telegram_id "
        "WHERE payments.status = 'pending' ORDER BY submitted_at"
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No pending payments. 🎉")
        return

    await update.message.reply_text(f"⏳ *{len(rows)} pending payment(s):*", parse_mode="Markdown")
    for p in rows:
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"pay_approve:{p['id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"pay_reject:{p['id']}"),
            ]
        ])
        try:
            await context.bot.send_photo(
                update.effective_chat.id,
                photo=p["file_id"],
                caption=(
                    f"👤 {p['name']} — {p['school']}\n"
                    f"💰 {p['amount']} birr\n"
                    f"📅 {p['submitted_at'][:16]}\n"
                    f"ID: {p['telegram_id']}"
                ),
                reply_markup=buttons,
            )
        except Exception:
            await update.message.reply_text(
                f"👤 {p['name']} — {p['school']}\n💰 {p['amount']} birr\n📅 {p['submitted_at'][:16]}",
                reply_markup=buttons,
            )


async def admin_income_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPER_ADMIN_ID:
        return
    conn = db()
    rows = conn.execute("SELECT * FROM payments WHERE status = 'approved'").fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No approved payments yet.")
        return

    total = sum(r["amount"] for r in rows)
    this_month = date.today().strftime("%Y-%m")
    month_total = sum(r["amount"] for r in rows if r["approved_at"] and r["approved_at"][:7] == this_month)

    await update.message.reply_text(
        f"📊 *Income Summary*\n\n"
        f"This month: {month_total} birr\n"
        f"All time: {total} birr\n"
        f"Total payments: {len(rows)}",
        parse_mode="Markdown",
    )


async def admin_find_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPER_ADMIN_ID:
        return
    context.user_data["admin_find"] = True
    await update.message.reply_text(
        "🔍 Send the teacher's name, school, or Telegram ID to search:",
        reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True),
    )


async def handle_admin_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPER_ADMIN_ID:
        return
    if not context.user_data.get("admin_find"):
        return
    text = update.message.text.strip()
    context.user_data.pop("admin_find", None)

    conn = db()
    if text.isdigit():
        rows = conn.execute("SELECT * FROM teachers WHERE telegram_id = ?", (int(text),)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM teachers WHERE LOWER(name) LIKE LOWER(?) OR LOWER(school) LIKE LOWER(?)",
            (f"%{text}%", f"%{text}%"),
        ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No teacher found.", reply_markup=admin_menu())
        return

    for r in rows:
        s = {"active": "✅ Active", "trial": "⏳ Trial", "expired": "❌ Expired"}.get(r["status"], "?")
        susp = "🚫 SUSPENDED" if r["suspended"] else ""
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Activate", callback_data=f"admin_activate:{r['telegram_id']}"),
                InlineKeyboardButton("🚫 Suspend", callback_data=f"admin_suspend:{r['telegram_id']}"),
            ]
        ])
        await update.message.reply_text(
            f"👤 *{r['name']}* {susp}\n"
            f"🏫 {r['school']}\n📚 {r['subject']}  🎓 {r['grade']}\n"
            f"📱 {r['phone']}\nStatus: {s}\nID: {r['telegram_id']}",
            parse_mode="Markdown",
            reply_markup=buttons,
        )


async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != SUPER_ADMIN_ID:
        await query.answer("Admins only.", show_alert=True)
        return
    await query.answer()

    action, tid_str = query.data.split(":")
    tid = int(tid_str)
    conn = db()

    if action == "admin_activate":
        conn.execute("UPDATE teachers SET status = 'active', suspended = 0 WHERE telegram_id = ?", (tid,))
        conn.commit()
        conn.close()
        await query.message.reply_text(f"✅ Teacher {tid} activated.")
        try:
            t = get_teacher(tid)
            await context.bot.send_message(tid, "✅ Your account has been activated by admin!", reply_markup=main_menu(t))
        except Exception:
            pass

    elif action == "admin_suspend":
        conn.execute("UPDATE teachers SET suspended = 1 WHERE telegram_id = ?", (tid,))
        conn.commit()
        conn.close()
        await query.message.reply_text(f"🚫 Teacher {tid} suspended.")
        try:
            await context.bot.send_message(tid, "🚫 Your account has been suspended. Contact admin for help.")
        except Exception:
            pass


# ── Cancel ────────────────────────────────────────────────────────────────────

async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    context.user_data.pop("reg", None)
    context.user_data.pop("att", None)
    context.user_data.pop("sal", None)
    context.user_data.pop("awaiting_payment_screenshot", None)
    context.user_data.pop("admin_find", None)

    teacher = get_teacher(tid)
    if tid == SUPER_ADMIN_ID:
        await update.message.reply_text("Cancelled.", reply_markup=admin_menu())
    else:
        await update.message.reply_text("Cancelled.", reply_markup=main_menu(teacher))


# ── Help ──────────────────────────────────────────────────────────────────────

async def btn_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Teacher Pro Bot Help*\n\n"
        "📋 *Attendance* — Record daily class attendance\n"
        "💰 *Salary Tracker* — Track your monthly salary, advances & deductions\n"
        "💳 *Subscribe* — Subscribe for 20 birr/month\n"
        "👤 *My Profile* — View your profile & subscription status\n\n"
        f"⏳ New users get *{TRIAL_DAYS}-day free trial*\n\n"
        "Need help? Contact @YourUsername",
        parse_mode="Markdown",
    )


# ── Main message router ───────────────────────────────────────────────────────

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    text = update.message.text.strip()

    # Cancel always works
    if text == "❌ Cancel":
        await handle_cancel(update, context)
        return

    # Admin buttons
    if tid == SUPER_ADMIN_ID:
        if text == "👥 All Teachers":
            await admin_all_teachers(update, context)
            return
        if text == "⏳ Pending Payments":
            await admin_pending_payments(update, context)
            return
        if text == "📊 Income Summary":
            await admin_income_summary(update, context)
            return
        if text == "🔍 Find Teacher":
            await admin_find_teacher(update, context)
            return
        if text == "👤 My Profile":
            await btn_my_profile(update, context)
            return
        if text == "ℹ️ Help":
            await btn_help(update, context)
            return
        # Admin search flow
        if context.user_data.get("admin_find"):
            await handle_admin_find(update, context)
            return
        return

    # Registration flow
    if context.user_data.get("reg"):
        await handle_registration(update, context)
        return

    # Attendance flow
    if context.user_data.get("att"):
        await handle_attendance_flow(update, context)
        return

    # Salary flow
    if context.user_data.get("sal"):
        await handle_salary_flow(update, context)
        return

    # Menu buttons
    if text == "📋 Attendance":
        await btn_attendance(update, context)
    elif text == "💰 Salary Tracker":
        await btn_salary(update, context)
    elif text in ("💳 Subscribe", "💳 Subscription"):
        await btn_subscribe(update, context)
    elif text == "💳 Subscription":
        await btn_subscription(update, context)
    elif text == "👤 My Profile":
        await btn_my_profile(update, context)
    elif text == "ℹ️ Help":
        await btn_help(update, context)
    else:
        teacher = get_teacher(tid)
        if teacher:
            await update.message.reply_text(
                "Use the menu buttons below 👇",
                reply_markup=main_menu(teacher),
            )
        else:
            await update.message.reply_text("Send /start to begin.")


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo — could be payment screenshot."""
    tid = update.effective_user.id
    teacher = get_teacher(tid)
    if teacher and context.user_data.get("awaiting_payment_screenshot"):
        await handle_payment_screenshot(update, context)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    init_db()
    threading.Thread(target=start_ping_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(on_payment_decision, pattern="^pay_(approve|reject):"))
    app.add_handler(CallbackQueryHandler(on_att_new, pattern="^att_new$"))
    app.add_handler(CallbackQueryHandler(on_att_history, pattern="^att_history$"))
    app.add_handler(CallbackQueryHandler(on_sal_new, pattern="^sal_new$"))
    app.add_handler(CallbackQueryHandler(on_sal_history, pattern="^sal_history$"))
    app.add_handler(CallbackQueryHandler(on_sal_summary, pattern="^sal_summary$"))
    app.add_handler(CallbackQueryHandler(on_go_subscribe, pattern="^go_subscribe$"))
    app.add_handler(CallbackQueryHandler(on_admin_action, pattern="^admin_(activate|suspend):"))

    # Photo (payment screenshot)
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))

    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("Teacher Pro Bot running.")
    app.run_polling()


if __name__ == "__main__":
    main()

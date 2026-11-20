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
    - Student roster (add by typing, or import PDF / Word / Excel)
    - Exam results: enter marks, send to students/parents on Telegram
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
import asyncio
import sqlite3
import threading
from datetime import datetime, date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telegram.error import BadRequest
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
        CREATE TABLE IF NOT EXISTS exams (
            id            {serial},
            telegram_id   BIGINT,
            class_name    TEXT,
            exam_name     TEXT,
            max_score     REAL,
            exam_date     TEXT,
            created_at    TEXT
        )
    """)

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS exam_scores (
            id            {serial},
            exam_id       BIGINT,
            student_id    BIGINT,
            student_name  TEXT,
            score         REAL
        )
    """)

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS student_contacts (
            id            {serial},
            student_id    BIGINT,
            chat_id       BIGINT,
            linked_at     TEXT
        )
    """)

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS students (
            id            {serial},
            telegram_id   BIGINT,
            class_name    TEXT,
            student_name  TEXT,
            created_at    TEXT
        )
    """)

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS attendance_marks (
            id            {serial},
            telegram_id   BIGINT,
            class_name    TEXT,
            att_date      TEXT,
            student_id    BIGINT,
            student_name  TEXT,
            status        TEXT
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
            ["📋 Attendance", "📝 Exam Results"],
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

    # Parent/student joining via a teacher's link
    if context.args and context.args[0].startswith("j") and context.args[0][1:].isdigit():
        await handle_join(update, context, int(context.args[0][1:]))
        return

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
            f"This bot helps Ethiopian teachers take attendance and send exam results.\n\n"
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
        await update.effective_message.reply_text("Please send /start to register first.")
        return

    # Check pending payment
    conn = db()
    pending = conn.execute(
        "SELECT id FROM payments WHERE telegram_id = ? AND status = 'pending'", (tid,)
    ).fetchone()
    conn.close()

    if pending:
        await update.effective_message.reply_text(
            "⏳ You already have a *pending payment* waiting for approval.\n\n"
            "Please wait for admin to approve it.",
            parse_mode="Markdown",
        )
        return

    await update.effective_message.reply_text(
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
                f"Use the menu to take attendance and send exam results. 📋💰",
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


# ── Attendance (student roster + tap to mark) ─────────────────────────────────

STATUS_ICON = {"present": "✅", "absent": "❌", "late": "🕒"}


def get_classes(tid: int):
    conn = db()
    rows = conn.execute(
        "SELECT class_name, COUNT(*) AS n FROM students WHERE telegram_id = ? "
        "GROUP BY class_name ORDER BY class_name",
        (tid,),
    ).fetchall()
    conn.close()
    return [(r["class_name"], r["n"]) for r in rows]


def get_students(tid: int, class_name: str):
    conn = db()
    rows = conn.execute(
        "SELECT id, student_name FROM students WHERE telegram_id = ? AND class_name = ? "
        "ORDER BY student_name",
        (tid, class_name),
    ).fetchall()
    conn.close()
    return [(r["id"], r["student_name"]) for r in rows]


async def _require_access(update: Update):
    query = update.callback_query
    teacher = get_teacher(update.effective_user.id)
    if not teacher or not is_access_ok(teacher):
        await query.answer("Subscribe to use this feature.", show_alert=True)
        return None
    await query.answer()
    return teacher


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
        [InlineKeyboardButton("✅ Take Attendance", callback_data="att_new")],
        [InlineKeyboardButton("👥 My Students", callback_data="stu_menu")],
        [InlineKeyboardButton("📊 View History", callback_data="att_history")],
    ])
    await update.message.reply_text(
        f"📋 *Attendance*{trial_banner(teacher)}",
        parse_mode="Markdown",
        reply_markup=buttons,
    )


# ---- Student roster ---------------------------------------------------------

async def on_stu_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    tid = update.effective_user.id
    classes = get_classes(tid)
    if classes:
        text = "👥 *My Students*\n\n" + "\n".join(f"🏫 {c} — {n} student(s)" for c, n in classes)
    else:
        text = "👥 *My Students*\n\nYou have not added any students yet."
    kb = [[InlineKeyboardButton("➕ Add Students", callback_data="stu_add")]]
    if classes:
        kb.append([InlineKeyboardButton("📃 View Class List", callback_data="stu_view")])
        kb.append([InlineKeyboardButton("🗑 Remove a Student", callback_data="stu_del")])
    await update.callback_query.message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
    )


async def on_stu_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    tid = update.effective_user.id
    context.user_data["stu"] = {"step": "class"}
    rows = [[c] for c, _ in get_classes(tid)[:6]]
    rows.append(["❌ Cancel"])
    await update.callback_query.message.reply_text(
        "➕ *Add Students*\n\nWhich class/section? (e.g. Grade 9A)\n"
        "Tap an existing class or type a new one.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True),
    )


SEX_TOKENS = {"m", "f", "male", "female"}
HEADER_WORDS = {
    "name", "names", "sex", "gender", "age", "no", "no.", "s.n", "sn", "school", "grade",
    "class", "section", "list", "roll", "student", "students", "attendance", "total", "date",
}


def clean_name_line(line):
    line = re.sub(r"^\s*\d+\s*[\.\)\-:]*\s*", "", str(line).strip())
    tokens = [t for t in line.split() if not t.isdigit() and t.lower() not in SEX_TOKENS]
    name = " ".join(tokens).strip(" -–.,;:|\t")
    if len(name) < 2 or not any(ch.isalpha() for ch in name):
        return None
    if any(w.lower().strip(".:") in HEADER_WORDS for w in name.split()):
        return None
    return name[:60]


def _find_name_col(rows):
    for r in rows[:10]:
        for j, c in enumerate(r):
            if c and "name" in str(c).lower():
                return j
    return None


def _name_from_row(cells, col=None):
    texts = ["" if c is None else str(c).strip() for c in cells]
    if col is not None:
        return clean_name_line(texts[col]) if col < len(texts) else None
    run = []
    for t in texts:
        if t and len(t) >= 2 and any(ch.isalpha() for ch in t) and t.lower() not in SEX_TOKENS:
            run.append(t)
            if len(run) == 3:
                break
        elif run:
            break
    return clean_name_line(" ".join(run)) if run else None


def _rows_to_names(rows):
    col = _find_name_col(rows)
    return [_name_from_row(r, col) for r in rows]


def extract_names(filename, data):
    ext = os.path.splitext(filename.lower())[1]
    names = []
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        names = _rows_to_names(rows)
    elif ext == ".docx":
        from docx import Document
        doc = Document(io.BytesIO(data))
        if doc.tables:
            for t in doc.tables:
                names += _rows_to_names([[c.text for c in row.cells] for row in t.rows])
        else:
            names = [clean_name_line(p.text) for p in doc.paragraphs]
    elif ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages:
            for line in (page.extract_text() or "").splitlines():
                names.append(clean_name_line(line))
    elif ext in (".csv", ".txt"):
        text = data.decode("utf-8-sig", errors="ignore")
        names = [clean_name_line(l) for l in text.splitlines()]
    else:
        raise ValueError("Unsupported file type. Please send a PDF, Word (.docx), Excel (.xlsx), CSV or TXT file.")

    out, seen = [], set()
    for n in names:
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out[:300]


def add_students(tid, class_name, names):
    existing = {n.lower() for _, n in get_students(tid, class_name)}
    now = datetime.now().isoformat(timespec="seconds")
    conn = db()
    added = 0
    for n in names:
        if n.lower() in existing:
            continue
        existing.add(n.lower())
        conn.execute(
            "INSERT INTO students (telegram_id, class_name, student_name, created_at) VALUES (?, ?, ?, ?)",
            (tid, class_name, n, now),
        )
        added += 1
    conn.commit()
    conn.close()
    return added, len(names) - added


async def handle_students_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    text = update.message.text.strip()
    stu = context.user_data.get("stu", {})
    teacher = get_teacher(tid)

    if stu.get("step") == "class":
        stu["class_name"] = text[:40]
        stu["step"] = "names"
        context.user_data["stu"] = stu
        await update.message.reply_text(
            f"🏫 {stu['class_name']}\n\n"
            "Now add the students in one of two ways:\n\n"
            "📝 *Type or paste* the names, one per line\n"
            "📎 *Upload a file* — PDF, Word (.docx) or Excel (.xlsx) with the class list\n",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True),
        )

    elif stu.get("step") == "names":
        parts = re.split(r"\n+", text) if "\n" in text else re.split(r",", text)
        names, seen = [], set()
        for p in parts:
            n = clean_name_line(p)
            if n and n.lower() not in seen:
                seen.add(n.lower())
                names.append(n)
        if not names:
            await update.message.reply_text("Please send at least one name, or upload a file.")
            return
        added, skipped = add_students(tid, stu["class_name"], names)
        context.user_data.pop("stu", None)
        msg = f"✅ Added {added} student(s) to {stu['class_name']}."
        if skipped:
            msg += f"\n({skipped} already in the class, skipped.)"
        await update.message.reply_text(msg, reply_markup=main_menu(teacher))
        await update.message.reply_text(
            "What next?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Take Attendance", callback_data="att_new")],
                [InlineKeyboardButton("➕ Add More", callback_data="stu_add")],
            ]),
        )

    elif stu.get("step") == "confirm":
        await update.message.reply_text("Tap ✅ Add or ❌ Cancel on the list above, or send ❌ Cancel.")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stu = context.user_data.get("stu")
    if not stu or stu.get("step") != "names":
        await update.message.reply_text(
            "To import a class list, open 📋 Attendance → 👥 My Students → ➕ Add Students, "
            "choose the class, then upload the file."
        )
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("That file is too big (max 5 MB).")
        return
    await update.message.reply_text("⏳ Reading the file…")
    try:
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
        names = await asyncio.to_thread(extract_names, doc.file_name or "", data)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return
    except Exception as e:
        print(f"[import] failed: {e}")
        await update.message.reply_text(
            "❌ I couldn't read that file. Make sure it is a PDF with selectable text (not a scan), "
            ".docx or .xlsx (old .xls/.doc are not supported). You can also paste the names as text."
        )
        return

    if not names:
        await update.message.reply_text(
            "I couldn't find any names in that file. If it is a scanned PDF/photo, "
            "please type or paste the names instead."
        )
        return

    stu["pending"] = names
    stu["step"] = "confirm"
    context.user_data["stu"] = stu
    preview = "\n".join(f"{i}. {n}" for i, n in enumerate(names[:40], 1))
    more = f"\n…and {len(names) - 40} more" if len(names) > 40 else ""
    await update.message.reply_text(
        f"📎 Found {len(names)} name(s) for {stu['class_name']}:\n\n{preview}{more}\n\nAdd them?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Add {len(names)} students", callback_data="stu_ok")],
            [InlineKeyboardButton("❌ Cancel", callback_data="stu_no")],
        ]),
    )


async def on_stu_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    tid = update.effective_user.id
    stu = context.user_data.get("stu")
    if not stu or stu.get("step") != "confirm":
        return
    added, skipped = add_students(tid, stu["class_name"], stu["pending"])
    context.user_data.pop("stu", None)
    msg = f"✅ Added {added} student(s) to {stu['class_name']}."
    if skipped:
        msg += f"\n({skipped} already in the class, skipped.)"
    await update.callback_query.message.reply_text(msg, reply_markup=main_menu(get_teacher(tid)))


async def on_stu_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data.pop("stu", None)
    await update.callback_query.message.reply_text(
        "Cancelled.", reply_markup=main_menu(get_teacher(update.effective_user.id))
    )


def _class_picker(classes, prefix):
    kb = [
        [InlineKeyboardButton(f"🏫 {c} ({n})", callback_data=f"{prefix}:{i}")]
        for i, (c, n) in enumerate(classes)
    ]
    return InlineKeyboardMarkup(kb)


async def on_stu_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    classes = get_classes(update.effective_user.id)
    context.user_data["classes"] = [c for c, _ in classes]
    await update.callback_query.message.reply_text(
        "Which class?", reply_markup=_class_picker(classes, "stu_vcls")
    )


async def on_stu_vcls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    classes = context.user_data.get("classes", [])
    i = int(update.callback_query.data.split(":")[1])
    if i >= len(classes):
        return
    students = get_students(update.effective_user.id, classes[i])
    lines = [f"{n}. {name}" for n, (_, name) in enumerate(students, 1)]
    await update.callback_query.message.reply_text(
        f"🏫 {classes[i]} — {len(students)} student(s)\n\n" + "\n".join(lines)
    )


async def on_stu_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    classes = get_classes(update.effective_user.id)
    context.user_data["classes"] = [c for c, _ in classes]
    await update.callback_query.message.reply_text(
        "Remove a student from which class?", reply_markup=_class_picker(classes, "stu_dcls")
    )


async def on_stu_dcls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    classes = context.user_data.get("classes", [])
    i = int(update.callback_query.data.split(":")[1])
    if i >= len(classes):
        return
    students = get_students(update.effective_user.id, classes[i])
    kb = [[InlineKeyboardButton(f"🗑 {name}", callback_data=f"stu_rm:{sid}")] for sid, name in students]
    await update.callback_query.message.reply_text(
        f"Tap a student to remove from {classes[i]}:", reply_markup=InlineKeyboardMarkup(kb)
    )


async def on_stu_rm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    sid = int(update.callback_query.data.split(":")[1])
    conn = db()
    conn.execute("DELETE FROM students WHERE id = ? AND telegram_id = ?", (sid, update.effective_user.id))
    conn.commit()
    conn.close()
    await update.callback_query.message.reply_text("🗑 Student removed.")


# ---- Taking attendance ------------------------------------------------------

async def on_att_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    tid = update.effective_user.id
    classes = get_classes(tid)
    if not classes:
        await update.callback_query.message.reply_text(
            "You haven't added any students yet. Add your class list once, then you can just tap.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Students", callback_data="stu_add")]]),
        )
        return
    context.user_data["classes"] = [c for c, _ in classes]
    await update.callback_query.message.reply_text(
        f"📋 Which class? (date: {date.today().isoformat()})",
        reply_markup=_class_picker(classes, "att_cls"),
    )


PAGE_SIZE = 20
NEXT_STATUS = {"present": "absent", "absent": "late", "late": "present"}


def _att_counts(sess):
    c = {"present": 0, "absent": 0, "late": 0}
    for s in sess["marks"].values():
        c[s] += 1
    return c


def _att_text(sess):
    c = _att_counts(sess)
    n = len(sess["students"])
    pages = max(1, -(-n // PAGE_SIZE))
    return (
        f"📋 {sess['class_name']} — {sess['att_date']}\n"
        f"👥 {n} students    ✅ {c['present']}  ❌ {c['absent']}  🕒 {c['late']}\n\n"
        f"Everyone starts as ✅ present. Call the names. If a student does not "
        f"respond, tap their name:\n"
        f"1 tap = ❌ absent, 2 taps = 🕒 late, 3 taps = ✅ present again.\n\n"
        f"Page {sess['page'] + 1} of {pages}"
    )


def _att_kb(sess):
    students = sess["students"]
    n = len(students)
    pages = max(1, -(-n // PAGE_SIZE))
    start = sess["page"] * PAGE_SIZE
    rows = []
    for i, (sid, name) in enumerate(students[start:start + PAGE_SIZE]):
        icon = STATUS_ICON[sess["marks"].get(sid, "present")]
        rows.append([InlineKeyboardButton(f"{icon} {start + i + 1}. {name[:32]}", callback_data=f"att_tog:{sid}")])
    nav = []
    if sess["page"] > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"att_page:{sess['page'] - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(f"{sess['page'] + 1}/{pages}", callback_data="att_noop"))
    if sess["page"] < pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"att_page:{sess['page'] + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("💾 Save Attendance", callback_data="att_save")])
    return InlineKeyboardMarkup(rows)


async def _safe_edit(query, text, kb):
    try:
        await query.edit_message_text(text, reply_markup=kb)
    except BadRequest:
        pass


async def on_att_cls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    classes = context.user_data.get("classes", [])
    i = int(update.callback_query.data.split(":")[1])
    if i >= len(classes):
        await update.callback_query.message.reply_text("Please tap Take Attendance again.")
        return
    class_name = classes[i]
    students = get_students(update.effective_user.id, class_name)
    if not students:
        await update.callback_query.message.reply_text("This class has no students.")
        return
    sess = {
        "class_name": class_name,
        "att_date": date.today().isoformat(),
        "students": students,
        "page": 0,
        "marks": {sid: "present" for sid, _ in students},
    }
    context.user_data["att_session"] = sess
    await update.callback_query.message.reply_text(_att_text(sess), reply_markup=_att_kb(sess))


async def on_att_tog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    sess = context.user_data.get("att_session")
    if not sess:
        await query.answer("Session expired. Start again.", show_alert=True)
        return
    await query.answer()
    sid = int(query.data.split(":")[1])
    if sid in sess["marks"]:
        sess["marks"][sid] = NEXT_STATUS[sess["marks"][sid]]
    await _safe_edit(query, _att_text(sess), _att_kb(sess))


async def on_att_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    sess = context.user_data.get("att_session")
    if not sess:
        await query.answer("Session expired. Start again.", show_alert=True)
        return
    await query.answer()
    sess["page"] = int(query.data.split(":")[1])
    await _safe_edit(query, _att_text(sess), _att_kb(sess))


async def on_att_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def on_att_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    sess = context.user_data.get("att_session")
    if not sess:
        await query.answer("Session expired. Start again.", show_alert=True)
        return
    await query.answer()
    await _finish_attendance(update, context, sess)


async def _finish_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE, sess):
    tid = update.effective_user.id
    marks = sess["marks"]
    counts = {"present": 0, "absent": 0, "late": 0}
    for s in marks.values():
        counts[s] += 1

    now = datetime.now().isoformat(timespec="seconds")
    conn = db()
    # Re-taking the same class on the same day overwrites
    conn.execute(
        "DELETE FROM attendance_marks WHERE telegram_id = ? AND class_name = ? AND att_date = ?",
        (tid, sess["class_name"], sess["att_date"]),
    )
    conn.execute(
        "DELETE FROM attendance WHERE telegram_id = ? AND class_name = ? AND att_date = ?",
        (tid, sess["class_name"], sess["att_date"]),
    )
    for sid, name in sess["students"]:
        conn.execute(
            "INSERT INTO attendance_marks (telegram_id, class_name, att_date, student_id, student_name, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tid, sess["class_name"], sess["att_date"], sid, name, marks.get(sid, "present")),
        )
    conn.execute(
        "INSERT INTO attendance (telegram_id, class_name, att_date, present, absent, late, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, sess["class_name"], sess["att_date"], counts["present"], counts["absent"], counts["late"], "", now),
    )
    conn.commit()
    conn.close()
    context.user_data.pop("att_session", None)

    absent = [n for sid, n in sess["students"] if marks.get(sid) == "absent"]
    late = [n for sid, n in sess["students"] if marks.get(sid) == "late"]
    text = (
        f"✅ Attendance saved!\n\n"
        f"📅 {sess['att_date']}   🏫 {sess['class_name']}\n"
        f"👥 Total: {len(sess['students'])}\n"
        f"✅ Present: {counts['present']}   ❌ Absent: {counts['absent']}   🕒 Late: {counts['late']}"
    )
    if absent:
        text += "\n\n❌ Absent:\n" + "\n".join(f"• {n}" for n in absent)
    if late:
        text += "\n\n🕒 Late:\n" + "\n".join(f"• {n}" for n in late)
    await update.callback_query.edit_message_text(text)


async def on_att_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tid = update.effective_user.id
    await query.answer()

    conn = db()
    rows = conn.execute(
        "SELECT * FROM attendance WHERE telegram_id = ? ORDER BY att_date DESC, id DESC LIMIT 10",
        (tid,),
    ).fetchall()

    if not rows:
        conn.close()
        await query.message.reply_text("No attendance records yet.")
        return

    lines = ["📊 Recent Attendance (last 10):\n"]
    for r in rows:
        absent_rows = conn.execute(
            "SELECT student_name FROM attendance_marks "
            "WHERE telegram_id = ? AND class_name = ? AND att_date = ? AND status = 'absent'",
            (tid, r["class_name"], r["att_date"]),
        ).fetchall()
        entry = (
            f"📅 {r['att_date']} — {r['class_name']}\n"
            f"   ✅ {r['present']}  ❌ {r['absent']}  🕒 {r['late']}"
        )
        if absent_rows:
            entry += "\n   Absent: " + ", ".join(a["student_name"] for a in absent_rows)
        lines.append(entry)
    conn.close()
    await query.message.reply_text("\n\n".join(lines))


# ── Exam Results ──────────────────────────────────────────────────────────────

def fmt_num(x):
    return "-" if x is None else f"{x:g}"


async def btn_exams(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    teacher = get_teacher(tid)
    if not teacher or not is_access_ok(teacher):
        await update.message.reply_text(
            "🔒 This feature requires a subscription.\n\nTap 💳 *Subscribe* to unlock.",
            parse_mode="Markdown",
        )
        return
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Exam Results", callback_data="ex_new")],
        [InlineKeyboardButton("📚 My Exams / Send", callback_data="ex_list")],
        [InlineKeyboardButton("🔗 Parent & Student Link", callback_data="ex_link")],
    ])
    await update.message.reply_text(
        f"📝 *Exam Results*{trial_banner(teacher)}\n\n"
        "Enter marks for a class, then send each student's result straight to "
        "their parent/student on Telegram.",
        parse_mode="Markdown",
        reply_markup=buttons,
    )


async def on_ex_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    classes = get_classes(update.effective_user.id)
    if not classes:
        await update.callback_query.message.reply_text(
            "Add your students first (📋 Attendance → 👥 My Students).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Students", callback_data="stu_add")]]),
        )
        return
    context.user_data["classes"] = [c for c, _ in classes]
    await update.callback_query.message.reply_text(
        "📝 Which class are these results for?", reply_markup=_class_picker(classes, "ex_cls")
    )


async def on_ex_cls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    classes = context.user_data.get("classes", [])
    i = int(update.callback_query.data.split(":")[1])
    if i >= len(classes):
        await update.callback_query.message.reply_text("Please tap New Exam Results again.")
        return
    students = get_students(update.effective_user.id, classes[i])
    if not students:
        await update.callback_query.message.reply_text("This class has no students.")
        return
    context.user_data["exam"] = {"step": "name", "class_name": classes[i], "students": students}
    await update.callback_query.message.reply_text(
        f"🏫 {classes[i]}\n\nWhat is the exam name? (e.g. Math Mid-term)",
        reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True),
    )


def _exam_prompt(ex):
    sid, name = ex["students"][ex["idx"]]
    n = len(ex["students"])
    text = f"👤 {ex['idx'] + 1}/{n}: {name}\nScore out of {fmt_num(ex['max'])}?  (send a number, or - if absent)"
    if ex["idx"] == 0:
        text += (
            "\n\n💡 Faster: send many scores at once, one per line, in the same "
            "order as your class list. Use - for absent."
        )
    return text


async def handle_exam_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    text = update.message.text.strip()
    ex = context.user_data.get("exam", {})
    step = ex.get("step")

    if step == "name":
        ex["exam_name"] = text[:60]
        ex["step"] = "max"
        context.user_data["exam"] = ex
        await update.message.reply_text("Total marks? (e.g. 50 or 100)")

    elif step == "max":
        try:
            m = float(text)
            if m <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send a number, e.g. 50")
            return
        ex["max"] = m
        ex["step"] = "scores"
        ex["idx"] = 0
        ex["scores"] = {}
        context.user_data["exam"] = ex
        await update.message.reply_text(
            _exam_prompt(ex),
            reply_markup=ReplyKeyboardMarkup([["-", "⬅️ Back"], ["❌ Cancel"]], resize_keyboard=True),
        )

    elif step == "scores":
        if text == "⬅️ Back":
            ex["idx"] = max(0, ex["idx"] - 1)
            await update.message.reply_text(_exam_prompt(ex))
            return

        tokens = [t for t in re.split(r"[\s,;]+", text) if t]
        vals = []
        for t in tokens:
            if t.lower() in ("-", "skip", "abs", "absent"):
                vals.append(None)
                continue
            try:
                v = float(t)
            except ValueError:
                await update.message.reply_text(f"'{t}' is not a number. Send a score, or - if absent.")
                return
            if v < 0 or v > ex["max"]:
                await update.message.reply_text(f"{t} is outside 0–{fmt_num(ex['max'])}. Please try again.")
                return
            vals.append(v)
        remaining = len(ex["students"]) - ex["idx"]
        if not vals:
            return
        if len(vals) > remaining:
            await update.message.reply_text(
                f"You sent {len(vals)} scores but only {remaining} student(s) are left. Please check and resend."
            )
            return
        for v in vals:
            sid, _ = ex["students"][ex["idx"]]
            ex["scores"][sid] = v
            ex["idx"] += 1
        context.user_data["exam"] = ex

        if ex["idx"] < len(ex["students"]):
            await update.message.reply_text(_exam_prompt(ex))
            return
        await _save_exam(update, context, ex)


async def _save_exam(update: Update, context: ContextTypes.DEFAULT_TYPE, ex):
    tid = update.effective_user.id
    now = datetime.now().isoformat(timespec="seconds")
    conn = db()
    conn.execute(
        "INSERT INTO exams (telegram_id, class_name, exam_name, max_score, exam_date, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, ex["class_name"], ex["exam_name"], ex["max"], date.today().isoformat(), now),
    )
    row = conn.execute("SELECT id FROM exams WHERE telegram_id = ? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    exam_id = row["id"]
    for sid, name in ex["students"]:
        conn.execute(
            "INSERT INTO exam_scores (exam_id, student_id, student_name, score) VALUES (?, ?, ?, ?)",
            (exam_id, sid, name, ex["scores"].get(sid)),
        )
    conn.commit()
    conn.close()
    context.user_data.pop("exam", None)

    await update.message.reply_text("✅ Results saved!", reply_markup=main_menu(get_teacher(tid)))
    await _show_exam(update.message, tid, exam_id)


def _load_exam(tid, exam_id):
    conn = db()
    exam = conn.execute("SELECT * FROM exams WHERE id = ? AND telegram_id = ?", (exam_id, tid)).fetchone()
    scores = []
    if exam:
        scores = conn.execute("SELECT * FROM exam_scores WHERE exam_id = ? ORDER BY id", (exam_id,)).fetchall()
    conn.close()
    return exam, scores


def _exam_summary_text(exam, scores):
    vals = [s["score"] for s in scores if s["score"] is not None]
    lines = [
        f"📝 {exam['exam_name']}",
        f"🏫 {exam['class_name']}    📅 {exam['exam_date']}",
        f"Out of: {fmt_num(exam['max_score'])}",
    ]
    if vals:
        lines.append(
            f"📊 Average: {sum(vals) / len(vals):.1f}   Highest: {fmt_num(max(vals))}   Lowest: {fmt_num(min(vals))}"
        )
    absent = len(scores) - len(vals)
    if absent:
        lines.append(f"Absent / no score: {absent}")
    lines.append("")
    for i, s in enumerate(scores, 1):
        lines.append(f"{i}. {s['student_name']} — {fmt_num(s['score']) if s['score'] is not None else 'Absent'}")
    text = "\n".join(lines)
    return text if len(text) < 3900 else text[:3900] + "\n…"


async def _show_exam(message, tid, exam_id):
    exam, scores = _load_exam(tid, exam_id)
    if not exam:
        await message.reply_text("Exam not found.")
        return
    await message.reply_text(
        _exam_summary_text(exam, scores),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Send to Students/Parents", callback_data=f"ex_send:{exam_id}")],
            [InlineKeyboardButton("🗑 Delete", callback_data=f"ex_del:{exam_id}")],
        ]),
    )


async def on_ex_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    tid = update.effective_user.id
    conn = db()
    rows = conn.execute(
        "SELECT * FROM exams WHERE telegram_id = ? ORDER BY id DESC LIMIT 10", (tid,)
    ).fetchall()
    conn.close()
    if not rows:
        await update.callback_query.message.reply_text("No exam results yet.")
        return
    kb = [
        [InlineKeyboardButton(f"📝 {r['exam_name']} — {r['class_name']} ({r['exam_date']})", callback_data=f"ex_view:{r['id']}")]
        for r in rows
    ]
    await update.callback_query.message.reply_text("Your recent exams:", reply_markup=InlineKeyboardMarkup(kb))


async def on_ex_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    await _show_exam(update.callback_query.message, update.effective_user.id, int(update.callback_query.data.split(":")[1]))


async def on_ex_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    tid = update.effective_user.id
    exam_id = int(update.callback_query.data.split(":")[1])
    exam, _ = _load_exam(tid, exam_id)
    if not exam:
        return
    conn = db()
    conn.execute("DELETE FROM exam_scores WHERE exam_id = ?", (exam_id,))
    conn.execute("DELETE FROM exams WHERE id = ? AND telegram_id = ?", (exam_id, tid))
    conn.commit()
    conn.close()
    await update.callback_query.message.reply_text("🗑 Exam deleted.")


def _exam_contacts(exam_id):
    conn = db()
    rows = conn.execute(
        "SELECT student_id, chat_id FROM student_contacts "
        "WHERE student_id IN (SELECT student_id FROM exam_scores WHERE exam_id = ?)",
        (exam_id,),
    ).fetchall()
    conn.close()
    m = {}
    for r in rows:
        m.setdefault(r["student_id"], []).append(r["chat_id"])
    return m


async def on_ex_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    tid = update.effective_user.id
    exam_id = int(update.callback_query.data.split(":")[1])
    exam, scores = _load_exam(tid, exam_id)
    if not exam:
        return
    contacts = _exam_contacts(exam_id)
    linked = sum(1 for s in scores if s["student_id"] in contacts)
    unlinked = len(scores) - linked
    if linked == 0:
        await update.callback_query.message.reply_text(
            "No parent or student has joined yet, so there is nobody to send to.\n\n"
            "Tap 🔗 Parent & Student Link in the 📝 Exam Results menu and share the link with them."
        )
        return
    await update.callback_query.message.reply_text(
        f"📤 Send *{exam['exam_name']}* results to {linked} student(s)/parent(s)?\n"
        f"({unlinked} student(s) have not joined and will not receive anything.)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, send now", callback_data=f"ex_go:{exam_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="att_noop")],
        ]),
    )


async def on_ex_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    tid = update.effective_user.id
    exam_id = int(update.callback_query.data.split(":")[1])
    exam, scores = _load_exam(tid, exam_id)
    if not exam:
        return
    teacher = get_teacher(tid)
    contacts = _exam_contacts(exam_id)
    vals = [s["score"] for s in scores if s["score"] is not None]
    avg = sum(vals) / len(vals) if vals else None
    max_score = exam["max_score"]

    await update.callback_query.message.reply_text("⏳ Sending…")
    sent = failed = 0
    not_joined = []
    for s in scores:
        chats = contacts.get(s["student_id"])
        if not chats:
            not_joined.append(s["student_name"])
            continue
        if s["score"] is None:
            score_line = "Score: Absent / not recorded"
        else:
            score_line = f"Score: {fmt_num(s['score'])} / {fmt_num(max_score)}  ({s['score'] / max_score * 100:.0f}%)"
        msg = (
            f"📝 Exam Result\n\n"
            f"👤 {s['student_name']}\n"
            f"🏫 {exam['class_name']} — {teacher['school']}\n"
            f"📚 {exam['exam_name']}   📅 {exam['exam_date']}\n\n"
            f"{score_line}\n"
        )
        if avg is not None:
            msg += f"Class average: {avg:.1f}\n"
        msg += f"\n— {teacher['name']}"
        ok = False
        for chat in chats:
            try:
                await context.bot.send_message(chat, msg)
                ok = True
            except Exception:
                pass
            await asyncio.sleep(0.05)
        if ok:
            sent += 1
        else:
            failed += 1

    report = f"✅ Sent to {sent} student(s)/parent(s)."
    if failed:
        report += f"\n⚠️ {failed} could not be delivered (they may have blocked the bot)."
    if not_joined:
        shown = ", ".join(not_joined[:30])
        report += f"\n\nNot joined yet ({len(not_joined)}): {shown}"
        if len(not_joined) > 30:
            report += ", …"
    await update.callback_query.message.reply_text(report)


async def on_ex_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    tid = update.effective_user.id
    conn = db()
    row = conn.execute(
        "SELECT COUNT(DISTINCT c.student_id) AS n FROM student_contacts c "
        "JOIN students s ON s.id = c.student_id WHERE s.telegram_id = ?",
        (tid,),
    ).fetchone()
    total = conn.execute("SELECT COUNT(*) AS n FROM students WHERE telegram_id = ?", (tid,)).fetchone()
    conn.close()
    link = f"https://t.me/{context.bot.username}?start=j{tid}"
    await update.callback_query.message.reply_text(
        f"🔗 {row['n']} of {total['n']} students have joined so far.\n\n"
        "Forward the message below to your students' parents (e.g. in your class Telegram group):"
    )
    await update.callback_query.message.reply_text(
        "📢 To receive your child's exam results on Telegram, open this link, "
        "choose the class, and tap your child's name:\n\n" + link
    )


# ---- Parent / student joining via link ---------------------------------------

async def handle_join(update: Update, context: ContextTypes.DEFAULT_TYPE, teacher_id: int):
    teacher = get_teacher(teacher_id)
    classes = get_classes(teacher_id)
    if not teacher or not classes:
        await update.message.reply_text("Sorry, this link is not valid.")
        return
    await update.message.reply_text(
        f"👋 Welcome!\n\nTeacher {teacher['name']} ({teacher['school']}) will send exam results here.\n\n"
        "Please choose your child's class:",
        reply_markup=_class_picker(classes, f"jn_cls:{teacher_id}"),
    )


async def _join_students(query, teacher_id, i):
    classes = get_classes(teacher_id)
    if i >= len(classes):
        await query.message.reply_text("Please open the link again.")
        return
    students = get_students(teacher_id, classes[i][0])
    kb = [[InlineKeyboardButton(name, callback_data=f"jn_pick:{sid}")] for sid, name in students[:95]]
    await query.message.reply_text("Tap the student's name:", reply_markup=InlineKeyboardMarkup(kb))


async def on_join_cls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    _, teacher_id, i = update.callback_query.data.split(":")
    await _join_students(update.callback_query, int(teacher_id), int(i))


async def on_join_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    sid = int(update.callback_query.data.split(":")[1])
    chat_id = update.effective_user.id
    conn = db()
    st = conn.execute("SELECT student_name FROM students WHERE id = ?", (sid,)).fetchone()
    if not st:
        conn.close()
        await update.callback_query.message.reply_text("Student not found.")
        return
    exists = conn.execute(
        "SELECT 1 FROM student_contacts WHERE student_id = ? AND chat_id = ?", (sid, chat_id)
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO student_contacts (student_id, chat_id, linked_at) VALUES (?, ?, ?)",
            (sid, chat_id, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    conn.close()
    await update.callback_query.message.reply_text(
        f"✅ Connected! Exam results for {st['student_name']} will be sent to you here."
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
    context.user_data.pop("att_session", None)
    context.user_data.pop("stu", None)
    context.user_data.pop("exam", None)
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
        "📝 *Exam Results* — Enter marks and send them to students/parents\n"
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

    # Student roster flow
    if context.user_data.get("stu"):
        await handle_students_flow(update, context)
        return

    # Exam results flow
    if context.user_data.get("exam"):
        await handle_exam_flow(update, context)
        return

    # Menu buttons
    if text == "📋 Attendance":
        await btn_attendance(update, context)
    elif text == "📝 Exam Results":
        await btn_exams(update, context)
    elif text == "💳 Subscribe":
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
            conn = db()
            linked = conn.execute("SELECT 1 FROM student_contacts WHERE chat_id = ?", (tid,)).fetchone()
            conn.close()
            if linked:
                await update.message.reply_text("✅ You are connected. Exam results will arrive here automatically.")
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
    app.add_handler(CallbackQueryHandler(on_att_cls, pattern="^att_cls:"))
    app.add_handler(CallbackQueryHandler(on_att_tog, pattern="^att_tog:"))
    app.add_handler(CallbackQueryHandler(on_att_page, pattern="^att_page:"))
    app.add_handler(CallbackQueryHandler(on_att_save, pattern="^att_save$"))
    app.add_handler(CallbackQueryHandler(on_att_noop, pattern="^att_noop$"))
    app.add_handler(CallbackQueryHandler(on_stu_ok, pattern="^stu_ok$"))
    app.add_handler(CallbackQueryHandler(on_stu_no, pattern="^stu_no$"))
    app.add_handler(CallbackQueryHandler(on_ex_new, pattern="^ex_new$"))
    app.add_handler(CallbackQueryHandler(on_ex_cls, pattern="^ex_cls:"))
    app.add_handler(CallbackQueryHandler(on_ex_list, pattern="^ex_list$"))
    app.add_handler(CallbackQueryHandler(on_ex_view, pattern="^ex_view:"))
    app.add_handler(CallbackQueryHandler(on_ex_del, pattern="^ex_del:"))
    app.add_handler(CallbackQueryHandler(on_ex_send, pattern="^ex_send:"))
    app.add_handler(CallbackQueryHandler(on_ex_go, pattern="^ex_go:"))
    app.add_handler(CallbackQueryHandler(on_ex_link, pattern="^ex_link$"))
    app.add_handler(CallbackQueryHandler(on_join_cls, pattern="^jn_cls:"))
    app.add_handler(CallbackQueryHandler(on_join_pick, pattern="^jn_pick:"))
    app.add_handler(CallbackQueryHandler(on_stu_menu, pattern="^stu_menu$"))
    app.add_handler(CallbackQueryHandler(on_stu_add, pattern="^stu_add$"))
    app.add_handler(CallbackQueryHandler(on_stu_view, pattern="^stu_view$"))
    app.add_handler(CallbackQueryHandler(on_stu_vcls, pattern="^stu_vcls:"))
    app.add_handler(CallbackQueryHandler(on_stu_del, pattern="^stu_del$"))
    app.add_handler(CallbackQueryHandler(on_stu_dcls, pattern="^stu_dcls:"))
    app.add_handler(CallbackQueryHandler(on_stu_rm, pattern="^stu_rm:"))
    app.add_handler(CallbackQueryHandler(on_go_subscribe, pattern="^go_subscribe$"))
    app.add_handler(CallbackQueryHandler(on_admin_action, pattern="^admin_(activate|suspend):"))

    # Photo (payment screenshot)
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))

    # Class list import (PDF / Word / Excel)
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.IMAGE, on_document))

    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("Teacher Pro Bot running.")
    app.run_polling()


if __name__ == "__main__":
    main()

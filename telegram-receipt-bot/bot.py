"""Telegram-бот: розпізнавання рахунків і контроль місячного плану продавців.

Ролі:
  власник (розробник)  — OWNER_ID у .env: білінг, рахунки замовнику, курс, бекапи
  адмін (замовник)     — ADMIN_ID у .env: продавці, плани, звіти, свій білінг
  продавці             — додаються адміном через бота
"""

from __future__ import annotations

import asyncio
import base64
import calendar
import csv
import io
import logging
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ───────────────────────────── конфіг ──────────────────────────────

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
CLIENT_NAME = os.getenv("CLIENT_NAME", "Замовник")

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
EFFORT = os.getenv("ANTHROPIC_EFFORT", "low")
# ціни Anthropic, $ за 1 млн токенів (за замовчуванням Sonnet 5.5)
PRICE_IN = float(os.getenv("PRICE_INPUT", "2.0"))
PRICE_OUT = float(os.getenv("PRICE_OUTPUT", "10.0"))
PRICE_CACHE_READ = float(os.getenv("PRICE_CACHE_READ", "0.20"))
PRICE_CACHE_WRITE = float(os.getenv("PRICE_CACHE_WRITE", "2.50"))

MARGIN_PERCENT = float(os.getenv("MARGIN_PERCENT", "36"))
SUBSCRIPTION_UAH = float(os.getenv("SUBSCRIPTION_UAH", "700"))
DEFAULT_USD_RATE = float(os.getenv("USD_RATE", "41.5"))
ALERT_USD = float(os.getenv("ALERT_USD", "10"))
OWNER_DETAILS = os.getenv("OWNER_DETAILS", "")  # реквізити для рахунку

TZ = ZoneInfo(os.getenv("TZ", "Europe/Kyiv"))
DB_PATH = Path(os.getenv("DB_PATH", "bot.db"))
CURRENCY = "грн"
MAX_IMAGE_BYTES = 5 * 1024 * 1024

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("receipt-bot")

MONTHS_UA = [
    "", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]
MONTHS_UA_GEN = [
    "", "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]

# ───────────────────────────── база ────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, admin_tg_id INTEGER UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sellers (
    id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id),
    tg_id INTEGER UNIQUE NOT NULL, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    seller_id INTEGER NOT NULL REFERENCES sellers(id), month TEXT NOT NULL,
    amount REAL NOT NULL, PRIMARY KEY (seller_id, month)
);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL REFERENCES sellers(id),
    month TEXT NOT NULL, invoice_no TEXT, invoice_date TEXT, amount REAL,
    status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, seller_id INTEGER, receipt_id INTEGER,
    month TEXT NOT NULL, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cache_read INTEGER, cache_write INTEGER, cost_usd REAL NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    client_id INTEGER NOT NULL, month TEXT NOT NULL, receipts_count INTEGER, api_cost_usd REAL,
    usd_rate REAL, margin_percent REAL, recognition_uah REAL, subscription_uah REAL, total_uah REAL,
    status TEXT NOT NULL, issued_at TEXT, paid_at TEXT, PRIMARY KEY (client_id, month)
);
CREATE TABLE IF NOT EXISTS pending_users (
    tg_id INTEGER PRIMARY KEY, name TEXT, username TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> int:
    """Створює таблиці і запис замовника. Повертає client_id."""
    with closing(db()) as conn, conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT id FROM clients WHERE admin_tg_id = ?", (ADMIN_ID,)).fetchone()
        if row:
            conn.execute("UPDATE clients SET name = ? WHERE id = ?", (CLIENT_NAME, row["id"]))
            return row["id"]
        cur = conn.execute(
            "INSERT INTO clients (name, admin_tg_id, created_at) VALUES (?, ?, ?)",
            (CLIENT_NAME, ADMIN_ID, now_iso()),
        )
        return cur.lastrowid


CLIENT_ID = 0  # заповнюється в main()


def setting(key: str, default: str | None = None) -> str | None:
    with closing(db()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with closing(db()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def usd_rate() -> float:
    return float(setting("usd_rate", str(DEFAULT_USD_RATE)))


# ───────────────────────────── утиліти ─────────────────────────────

def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def this_month() -> str:
    return now().strftime("%Y-%m")


def prev_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def month_title(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{MONTHS_UA[m]} {y}"


def month_gen(month: str) -> str:
    return MONTHS_UA_GEN[int(month.split("-")[1])]


def parse_month(arg: str | None) -> str | None:
    if not arg:
        return this_month()
    try:
        return datetime.strptime(arg, "%Y-%m").strftime("%Y-%m")
    except ValueError:
        return None


def fmt(x: float | None, dec: int = 0) -> str:
    if x is None:
        return "—"
    s = f"{x:,.{dec}f}".replace(",", " ").replace(".", ",")
    return s


def fmt_uah(x: float | None, dec: int = 0) -> str:
    return f"{fmt(x, dec)} {CURRENCY}"


def parse_amount(text: str) -> float | None:
    t = text.replace(" ", "").replace(" ", "").replace(",", ".").replace("грн", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v > 0 else None


def bar(pct: float, width: int = 10) -> str:
    filled = min(width, int(round(max(pct, 0) / 100 * width)))
    return "▓" * filled + "░" * (width - filled)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ───────────────────────────── ролі ────────────────────────────────

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID


def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID or uid == OWNER_ID


def get_seller(tg_id: int) -> sqlite3.Row | None:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM sellers WHERE tg_id = ? AND active = 1", (tg_id,)
        ).fetchone()


def seller_by_name(name: str) -> sqlite3.Row | None:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM sellers WHERE client_id = ? AND active = 1 AND lower(name) = lower(?)",
            (CLIENT_ID, name),
        ).fetchone()


# ───────────────────────────── плани і статистика ──────────────────

def plan_for(seller_id: int, month: str) -> tuple[float | None, bool]:
    """План на місяць. Якщо не задано, береться останній попередній. (сума, перенесений)."""
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT amount FROM plans WHERE seller_id = ? AND month = ?", (seller_id, month)
        ).fetchone()
        if row:
            return row["amount"], False
        row = conn.execute(
            "SELECT amount FROM plans WHERE seller_id = ? AND month < ? ORDER BY month DESC LIMIT 1",
            (seller_id, month),
        ).fetchone()
    return (row["amount"], True) if row else (None, False)


def sold_for(seller_id: int, month: str) -> tuple[float, int]:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) s, COUNT(*) n FROM receipts "
            "WHERE seller_id = ? AND month = ? AND status = 'confirmed'",
            (seller_id, month),
        ).fetchone()
    return row["s"], row["n"]


def stats_text(seller: sqlite3.Row, month: str | None = None) -> str:
    month = month or this_month()
    plan, carried = plan_for(seller["id"], month)
    sold, n = sold_for(seller["id"], month)
    lines = [f"<b>План {month_gen(month)}:</b> {fmt_uah(plan) if plan else 'не задано'}"
             + (" <i>(перенесено з минулого місяця)</i>" if carried else "")]
    if plan:
        pct = sold / plan * 100
        left = plan - sold
        lines.append(f"<b>Виконано:</b> {fmt_uah(sold)}  {bar(pct)} {pct:.0f}%")
        if left > 0:
            lines.append(f"<b>Залишилось:</b> {fmt_uah(left)}")
            if month == this_month():
                today = now().date()
                days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
                lines.append(f"Днів до кінця місяця: {days_left}, треба {fmt_uah(left / days_left)}/день")
        else:
            lines.append(f"🎉 План виконано, перевищення {fmt_uah(-left)}")
    else:
        lines.append(f"<b>Продано:</b> {fmt_uah(sold)}")
    lines.append(f"Рахунків: {n}")
    return "\n".join(lines)


# ───────────────────────────── білінг ──────────────────────────────

def billing_data(month: str) -> dict:
    """Дані білінгу за місяць: з виставленого рахунку, якщо є, інакше живий підрахунок."""
    with closing(db()) as conn:
        inv = conn.execute(
            "SELECT * FROM invoices WHERE client_id = ? AND month = ?", (CLIENT_ID, month)
        ).fetchone()
        usage = conn.execute(
            "SELECT COUNT(*) calls, COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(input_tokens),0) ti, "
            "COALESCE(SUM(output_tokens),0) to_, COALESCE(SUM(cache_read),0) tcr "
            "FROM api_calls WHERE client_id = ? AND month = ?",
            (CLIENT_ID, month),
        ).fetchone()
        cnt = conn.execute(
            "SELECT COUNT(*) n FROM receipts r JOIN sellers s ON s.id = r.seller_id "
            "WHERE s.client_id = ? AND r.month = ? AND r.status = 'confirmed'",
            (CLIENT_ID, month),
        ).fetchone()["n"]
    d = {
        "month": month,
        "calls": usage["calls"],
        "tokens_in": usage["ti"],
        "tokens_out": usage["to_"],
        "tokens_cache": usage["tcr"],
        "status": inv["status"] if inv else "live",
    }
    if inv:
        d.update(
            receipts=inv["receipts_count"], api_cost=inv["api_cost_usd"], rate=inv["usd_rate"],
            margin=inv["margin_percent"], recognition=inv["recognition_uah"],
            subscription=inv["subscription_uah"], total=inv["total_uah"],
            issued_at=inv["issued_at"], paid_at=inv["paid_at"],
        )
    else:
        rate = usd_rate()
        recognition = usage["cost"] * (1 + MARGIN_PERCENT / 100) * rate
        d.update(
            receipts=cnt, api_cost=usage["cost"], rate=rate, margin=MARGIN_PERCENT,
            recognition=recognition, subscription=SUBSCRIPTION_UAH,
            total=recognition + SUBSCRIPTION_UAH, issued_at=None, paid_at=None,
        )
    d["per_receipt"] = d["recognition"] / d["receipts"] if d["receipts"] else 0
    return d


STATUS_UA = {"live": "поточний підрахунок", "issued": "рахунок виставлено", "paid": "оплачено ✅"}


def billing_text(month: str, detailed: bool) -> str:
    d = billing_data(month)
    head = month_title(month)
    if month == this_month() and d["status"] == "live":
        head += f" (станом на {now().day} число)"
    lines = [f"<b>{head}</b>",
             f"Розпізнано рахунків: {d['receipts']}",
             f"Тариф: {fmt_uah(d['per_receipt'], 2)} за рахунок",
             f"Розпізнавання: {fmt_uah(d['recognition'], 2)}",
             f"Обслуговування бота: {fmt_uah(d['subscription'], 2)}",
             "───────────────",
             f"<b>{'Поточна сума' if d['status'] == 'live' else 'До оплати'}: {fmt_uah(d['total'], 2)}</b>",
             f"Статус: {STATUS_UA[d['status']]}"]
    if detailed:
        lines += ["",
                  "<i>Деталі (бачите лише ви):</i>",
                  f"Запитів до API: {d['calls']}",
                  f"Токени: {fmt(d['tokens_in'])} вхід / {fmt(d['tokens_out'])} вихід / {fmt(d['tokens_cache'])} кеш",
                  f"Вартість API: {d['api_cost']:.4f} $",
                  f"Маржа {d['margin']:.0f}%: {d['api_cost'] * d['margin'] / 100:.4f} $",
                  f"Курс: {d['rate']:.2f} грн/$"]
    return "\n".join(lines)


def issue_invoice(month: str) -> dict:
    d = billing_data(month)
    if d["status"] == "live":
        with closing(db()) as conn, conn:
            conn.execute(
                "INSERT INTO invoices (client_id, month, receipts_count, api_cost_usd, usd_rate, margin_percent, "
                "recognition_uah, subscription_uah, total_uah, status, issued_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'issued',?)",
                (CLIENT_ID, month, d["receipts"], d["api_cost"], d["rate"], d["margin"],
                 d["recognition"], d["subscription"], d["total"], now_iso()),
            )
        d["status"] = "issued"
    return d


def invoice_text(d: dict) -> str:
    lines = [f"<b>Рахунок за {month_title(d['month']).lower()}</b>",
             f"Розпізнано рахунків: {d['receipts']} × {fmt_uah(d['per_receipt'], 2)} = {fmt_uah(d['recognition'], 2)}",
             f"Обслуговування бота: {fmt_uah(d['subscription'], 2)}",
             "───────────────",
             f"<b>До оплати: {fmt_uah(d['total'], 2)}</b>"]
    if OWNER_DETAILS:
        lines += ["", esc(OWNER_DETAILS)]
    return "\n".join(lines)


# ───────────────────────────── розпізнавання ───────────────────────

aclient = anthropic.AsyncAnthropic()

RECOGNIZE_SYSTEM = (
    "Ти читаєш фото і скріншоти рахунків, накладних, чеків та квитанцій українських продавців. "
    "Витягни підсумкову суму до оплати (разом з ПДВ, якщо він є), номер документа і дату. "
    "Якщо на зображенні не рахунок або суму визначити неможливо, постав is_invoice=false або total_amount=null. "
    "Не вигадуй значень, яких не видно."
)

RECOGNIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_invoice": {"type": "boolean", "description": "Чи це рахунок/накладна/чек"},
        "total_amount": {"type": ["number", "null"], "description": "Підсумкова сума до оплати"},
        "currency": {"type": ["string", "null"], "description": "Код валюти, напр. UAH"},
        "invoice_number": {"type": ["string", "null"], "description": "Номер документа без символу №"},
        "invoice_date": {"type": ["string", "null"], "description": "Дата у форматі YYYY-MM-DD"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "note": {"type": "string", "description": "Коротке пояснення українською, якщо є сумніви, інакше порожній рядок"},
    },
    "required": ["is_invoice", "total_amount", "currency", "invoice_number", "invoice_date", "confidence", "note"],
    "additionalProperties": False,
}


def call_cost(usage) -> float:
    ti = usage.input_tokens or 0
    to = usage.output_tokens or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (ti * PRICE_IN + to * PRICE_OUT + cr * PRICE_CACHE_READ + cw * PRICE_CACHE_WRITE) / 1_000_000


async def recognize(image: bytes, media_type: str, seller_id: int) -> dict:
    """Повертає dict за RECOGNIZE_SCHEMA або {'error': текст}. Записує вартість виклику."""
    import json

    b64 = base64.standard_b64encode(image).decode()
    try:
        resp = await aclient.beta.messages.create(
            model=MODEL,
            max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=RECOGNIZE_SYSTEM,
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": RECOGNIZE_SCHEMA}},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": "Розпізнай цей рахунок."},
                ],
            }],
        )
    except anthropic.RateLimitError:
        return {"error": "Сервіс розпізнавання перевантажений, спробуйте за хвилину."}
    except anthropic.AuthenticationError:
        log.error("Невірний ключ Anthropic")
        return {"error": "Помилка налаштування розпізнавання. Зверніться до адміністратора."}
    except anthropic.APIStatusError as e:
        log.error("Anthropic API %s: %s", e.status_code, e.message)
        return {"error": "Помилка розпізнавання. Спробуйте ще раз."}
    except anthropic.APIConnectionError:
        return {"error": "Немає зв'язку з сервісом розпізнавання. Спробуйте пізніше."}

    cost = call_cost(resp.usage)
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO api_calls (client_id, seller_id, month, model, input_tokens, output_tokens, "
            "cache_read, cache_write, cost_usd, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (CLIENT_ID, seller_id, this_month(), resp.model, resp.usage.input_tokens, resp.usage.output_tokens,
             getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
             getattr(resp.usage, "cache_creation_input_tokens", 0) or 0, cost, now_iso()),
        )

    if resp.stop_reason == "refusal":
        return {"error": "Сервіс відмовився обробляти це зображення. Введіть суму вручну."}
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        return {"error": "Порожня відповідь розпізнавання. Введіть суму вручну."}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": "Не вдалося прочитати відповідь розпізнавання. Введіть суму вручну."}


async def check_cost_alert(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    month = this_month()
    if setting(f"alerted_{month}"):
        return
    d = billing_data(month)
    if d["api_cost"] >= ALERT_USD:
        set_setting(f"alerted_{month}", "1")
        await ctx.bot.send_message(
            OWNER_ID,
            f"⚠️ Витрати API за {month_gen(month)} досягли {d['api_cost']:.2f} $. Перевірте баланс Anthropic.",
        )


# ───────────────────────────── рахунки продавця ────────────────────

def receipt_card(r: sqlite3.Row, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    no = f"№{esc(r['invoice_no'])}" if r["invoice_no"] else "без номера"
    dt = ""
    if r["invoice_date"]:
        try:
            dt = " від " + datetime.strptime(r["invoice_date"], "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            dt = f" від {esc(r['invoice_date'])}"
    text = f"📄 <b>Рахунок {no}{dt}</b>\nСума: <b>{fmt_uah(r['amount'], 2)}</b>"
    if note:
        text += f"\n<i>{esc(note)}</i>"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Підтвердити", callback_data=f"rc:{r['id']}"),
        InlineKeyboardButton("✏️ Змінити суму", callback_data=f"re:{r['id']}"),
        InlineKeyboardButton("❌ Скасувати", callback_data=f"rx:{r['id']}"),
    ]])
    return text, kb


def get_receipt(rid: int) -> sqlite3.Row | None:
    with closing(db()) as conn:
        return conn.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()


def find_duplicate(r: sqlite3.Row) -> sqlite3.Row | None:
    if not r["invoice_no"]:
        return None
    with closing(db()) as conn:
        return conn.execute(
            "SELECT r.*, s.name seller_name FROM receipts r JOIN sellers s ON s.id = r.seller_id "
            "WHERE s.client_id = ? AND r.status = 'confirmed' AND r.id != ? "
            "AND r.invoice_no = ? AND ABS(r.amount - ?) < 0.01 AND r.month >= ?",
            (CLIENT_ID, r["id"], r["invoice_no"], r["amount"], prev_month(r["month"])),
        ).fetchone()


async def handle_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    seller = get_seller(update.effective_user.id)
    if not seller:
        if is_admin(update.effective_user.id):
            await msg.reply_text("Фото рахунків надсилають продавці. Ви адмін, вам доступні /report і /help.")
        else:
            await msg.reply_text("Ви не підключені. Натисніть /start, щоб надіслати запит адміну.")
        return

    if msg.photo:
        tg_file = await msg.photo[-1].get_file()
        media_type = "image/jpeg"
        size = msg.photo[-1].file_size or 0
    else:
        doc = msg.document
        media_type = doc.mime_type or ""
        if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            await msg.reply_text("Надішліть зображення: фото або скріншот (JPG, PNG).")
            return
        size = doc.file_size or 0
        tg_file = await doc.get_file()
    if size > MAX_IMAGE_BYTES:
        await msg.reply_text("Файл завеликий. Надішліть як фото (зі стисненням), а не як файл.")
        return

    wait = await msg.reply_text("🔍 Розпізнаю…")
    data = bytes(await tg_file.download_as_bytearray())
    result = await recognize(data, media_type, seller["id"])
    asyncio.create_task(check_cost_alert(ctx))

    if "error" in result or not result.get("is_invoice") or result.get("total_amount") is None:
        with closing(db()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO receipts (seller_id, month, invoice_no, invoice_date, amount, status, created_at) "
                "VALUES (?,?,?,?,NULL,'pending',?)",
                (seller["id"], this_month(), result.get("invoice_number"), result.get("invoice_date"), now_iso()),
            )
            rid = cur.lastrowid
        ctx.user_data["manual_receipt"] = rid
        reason = result.get("error") or result.get("note") or "Не вдалося знайти суму на зображенні."
        await wait.edit_text(f"{reason}\n\nВведіть суму рахунку вручну, наприклад: <code>12500</code>",
                             parse_mode=ParseMode.HTML)
        return

    currency = (result.get("currency") or "").upper()
    note = result.get("note") or ""
    if currency and currency not in ("UAH", "ГРН", "UA"):
        note = f"Валюта на рахунку: {currency}. " + note
    if result.get("confidence") == "low":
        note = "Низька впевненість, перевірте суму. " + note

    with closing(db()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO receipts (seller_id, month, invoice_no, invoice_date, amount, status, created_at) "
            "VALUES (?,?,?,?,?,'pending',?)",
            (seller["id"], this_month(), result.get("invoice_number"), result.get("invoice_date"),
             float(result["total_amount"]), now_iso()),
        )
        rid = cur.lastrowid
    text, kb = receipt_card(get_receipt(rid), note.strip())
    await wait.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручне введення суми після невдалого розпізнавання або кнопки «Змінити суму»."""
    rid = ctx.user_data.get("manual_receipt")
    if not rid:
        return
    amount = parse_amount(update.effective_message.text)
    if amount is None:
        await update.effective_message.reply_text("Не схоже на суму. Введіть число, наприклад 12500 або 12500,50.")
        return
    with closing(db()) as conn, conn:
        conn.execute("UPDATE receipts SET amount = ? WHERE id = ? AND status = 'pending'", (amount, rid))
    ctx.user_data.pop("manual_receipt", None)
    r = get_receipt(rid)
    text, kb = receipt_card(r, "Сума введена вручну")
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def on_receipt_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    action, rid = q.data.split(":")
    r = get_receipt(int(rid))
    seller = get_seller(q.from_user.id)
    if not r or not seller or r["seller_id"] != seller["id"]:
        await q.answer("Цей рахунок не ваш.", show_alert=True)
        return
    if r["status"] != "pending":
        await q.answer("Рахунок уже оброблено.")
        return

    if action == "rx":
        with closing(db()) as conn, conn:
            conn.execute("UPDATE receipts SET status = 'cancelled' WHERE id = ?", (r["id"],))
        ctx.user_data.pop("manual_receipt", None)
        await q.edit_message_text("❌ Скасовано.")
        await q.answer()
        return

    if action == "re":
        ctx.user_data["manual_receipt"] = r["id"]
        await q.edit_message_text("Введіть правильну суму, наприклад: <code>12500</code>", parse_mode=ParseMode.HTML)
        await q.answer()
        return

    # підтвердження
    if r["amount"] is None:
        await q.answer("Спочатку введіть суму.", show_alert=True)
        return
    dup = find_duplicate(r)
    if dup:
        with closing(db()) as conn, conn:
            conn.execute("UPDATE receipts SET status = 'duplicate' WHERE id = ?", (r["id"],))
        await q.edit_message_text(
            f"⚠️ Дубль. Рахунок №{esc(dup['invoice_no'])} на {fmt_uah(dup['amount'], 2)} "
            f"уже записано ({esc(dup['seller_name'])}, {dup['created_at'][:10]}).",
            parse_mode=ParseMode.HTML,
        )
        await q.answer()
        return
    with closing(db()) as conn, conn:
        conn.execute("UPDATE receipts SET status = 'confirmed' WHERE id = ?", (r["id"],))
    no = f"№{esc(r['invoice_no'])}" if r["invoice_no"] else ""
    await q.edit_message_text(
        f"✅ Записано: рахунок {no} на {fmt_uah(r['amount'], 2)}\n\n{stats_text(seller)}",
        parse_mode=ParseMode.HTML,
    )
    await q.answer("Записано")


# ───────────────────────────── команди: спільні ────────────────────

HELP_SELLER = (
    "Надішліть фото або скріншот рахунку, і я запишу суму.\n\n"
    "/stat — мій план і результат за місяць\n"
    "/help — ця довідка"
)
HELP_ADMIN = (
    "<b>Продавці</b>\n"
    "/list — перелік продавців і планів\n"
    "/add &lt;telegram_id&gt; &lt;ім'я&gt; — додати вручну\n"
    "/rename &lt;ім'я&gt; &lt;нове ім'я&gt;\n"
    "/remove &lt;ім'я&gt; — відключити\n\n"
    "<b>Плани</b>\n"
    "/plan &lt;ім'я&gt; &lt;сума&gt; — план на поточний місяць\n"
    "/plan &lt;ім'я&gt; &lt;сума&gt; 2026-11 — на інший місяць\n\n"
    "<b>Звіти</b>\n"
    "/report [2026-09] — зведення по продавцях\n"
    "/export [2026-09] — усі рахунки в CSV\n"
    "/billing [2026-09] — вартість обслуговування\n\n"
    "Нові продавці: вони натискають /start у бота, вам приходить запит із кнопкою «Додати»."
)
HELP_OWNER = (
    "<b>Власник</b>\n"
    "/billing [2026-09] — білінг з деталями і кнопкою виставлення рахунку\n"
    "/invoice [2026-09] — виставити рахунок замовнику\n"
    "/paid 2026-09 — відмітити оплаченим\n"
    "/rate 42.1 — курс долара\n"
    "/backup — надіслати копію бази\n\n"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if is_owner(u.id):
        await update.message.reply_text(HELP_OWNER + HELP_ADMIN, parse_mode=ParseMode.HTML)
        return
    if is_admin(u.id):
        await update.message.reply_text(f"Вітаю! Ви адмін ({esc(CLIENT_NAME)}).\n\n" + HELP_ADMIN,
                                        parse_mode=ParseMode.HTML)
        return
    if get_seller(u.id):
        await update.message.reply_text(HELP_SELLER)
        return
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_users (tg_id, name, username, created_at) VALUES (?,?,?,?)",
            (u.id, u.full_name, u.username, now_iso()),
        )
    await update.message.reply_text(
        f"Запит на підключення надіслано адміну. Ваш ID: {u.id}. Дочекайтесь підтвердження."
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Додати як продавця", callback_data=f"ua:{u.id}"),
        InlineKeyboardButton("✖ Ігнорувати", callback_data=f"ui:{u.id}"),
    ]])
    uname = f" (@{u.username})" if u.username else ""
    await ctx.bot.send_message(ADMIN_ID, f"Новий користувач: {esc(u.full_name)}{uname}, ID {u.id}",
                               reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if is_owner(uid):
        await update.message.reply_text(HELP_OWNER + HELP_ADMIN, parse_mode=ParseMode.HTML)
    elif is_admin(uid):
        await update.message.reply_text(HELP_ADMIN, parse_mode=ParseMode.HTML)
    elif get_seller(uid):
        await update.message.reply_text(HELP_SELLER)
    else:
        await update.message.reply_text("Натисніть /start, щоб надіслати запит на підключення.")


async def cmd_stat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    seller = get_seller(update.effective_user.id)
    if not seller:
        return
    await update.message.reply_text(f"<b>{esc(seller['name'])}</b>\n{stats_text(seller)}", parse_mode=ParseMode.HTML)


# ───────────────────────────── команди: адмін ──────────────────────

def admin_only(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            return
        await fn(update, ctx)
    return wrapper


def owner_only(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            return
        await fn(update, ctx)
    return wrapper


def add_seller(tg_id: int, name: str) -> str:
    with closing(db()) as conn, conn:
        row = conn.execute("SELECT * FROM sellers WHERE tg_id = ?", (tg_id,)).fetchone()
        if row:
            conn.execute("UPDATE sellers SET active = 1, name = ? WHERE id = ?", (name, row["id"]))
        else:
            conn.execute(
                "INSERT INTO sellers (client_id, tg_id, name, created_at) VALUES (?,?,?,?)",
                (CLIENT_ID, tg_id, name, now_iso()),
            )
        conn.execute("DELETE FROM pending_users WHERE tg_id = ?", (tg_id,))
    return name


async def on_user_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer()
        return
    action, tg_id = q.data.split(":")
    tg_id = int(tg_id)
    with closing(db()) as conn:
        p = conn.execute("SELECT * FROM pending_users WHERE tg_id = ?", (tg_id,)).fetchone()
    if action == "ui":
        with closing(db()) as conn, conn:
            conn.execute("DELETE FROM pending_users WHERE tg_id = ?", (tg_id,))
        await q.edit_message_text(f"Користувача {tg_id} проігноровано.")
        await q.answer()
        return
    name = (p["name"] if p else None) or f"Продавець {tg_id}"
    add_seller(tg_id, name)
    await q.edit_message_text(f"✅ {esc(name)} додано як продавця. Змінити ім'я: /rename {esc(name)} Нове ім'я",
                              parse_mode=ParseMode.HTML)
    await q.answer()
    try:
        await ctx.bot.send_message(tg_id, "✅ Вас підключено. Надсилайте фото рахунків.\n\n" + HELP_SELLER)
    except Exception:  # користувач міг заблокувати бота
        pass


@admin_only
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if len(ctx.args) < 2 or not ctx.args[0].isdigit():
        await update.message.reply_text("Формат: /add <telegram_id> <ім'я>")
        return
    name = add_seller(int(ctx.args[0]), " ".join(ctx.args[1:]))
    await update.message.reply_text(f"✅ {name} додано.")


@admin_only
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    month = this_month()
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM sellers WHERE client_id = ? AND active = 1 ORDER BY name", (CLIENT_ID,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("Продавців ще немає. Вони з'являться, коли напишуть боту /start.")
        return
    lines = [f"<b>Продавці ({month_gen(month)})</b>"]
    for i, s in enumerate(rows, 1):
        plan, carried = plan_for(s["id"], month)
        p = fmt_uah(plan) + (" (перенесено)" if carried else "") if plan else "план не задано"
        lines.append(f"{i}. {esc(s['name'])} — {p}  <code>{s['tg_id']}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = list(ctx.args)
    month = this_month()
    if args and parse_month(args[-1]) and "-" in args[-1]:
        month = parse_month(args.pop())
    if len(args) < 2:
        await update.message.reply_text("Формат: /plan <ім'я> <сума> [2026-11]")
        return
    amount = parse_amount(args[-1])
    name = " ".join(args[:-1])
    seller = seller_by_name(name)
    if not seller:
        await update.message.reply_text(f"Продавця «{name}» не знайдено. Перелік: /list")
        return
    if amount is None:
        await update.message.reply_text("Сума має бути числом, наприклад 150000.")
        return
    with closing(db()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO plans (seller_id, month, amount) VALUES (?,?,?)",
                     (seller["id"], month, amount))
    await update.message.reply_text(f"План {esc(seller['name'])} на {month_gen(month)}: {fmt_uah(amount)}",
                                    parse_mode=ParseMode.HTML)
    try:
        await ctx.bot.send_message(seller["tg_id"], f"Ваш план на {month_gen(month)}: {fmt_uah(amount)}")
    except Exception:
        pass


@admin_only
async def cmd_rename(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if len(ctx.args) < 2:
        await update.message.reply_text("Формат: /rename <ім'я> <нове ім'я>")
        return
    # пробуємо всі розбиття: перші k слів — старе ім'я
    for k in range(1, len(ctx.args)):
        old, new = " ".join(ctx.args[:k]), " ".join(ctx.args[k:])
        seller = seller_by_name(old)
        if seller:
            with closing(db()) as conn, conn:
                conn.execute("UPDATE sellers SET name = ? WHERE id = ?", (new, seller["id"]))
            await update.message.reply_text(f"✅ {old} → {new}")
            return
    await update.message.reply_text("Продавця не знайдено. Перелік: /list")


@admin_only
async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    name = " ".join(ctx.args)
    seller = seller_by_name(name) if name else None
    if not seller:
        await update.message.reply_text("Формат: /remove <ім'я>. Перелік: /list")
        return
    with closing(db()) as conn, conn:
        conn.execute("UPDATE sellers SET active = 0 WHERE id = ?", (seller["id"],))
    await update.message.reply_text(f"✅ {esc(seller['name'])} відключено. Історія рахунків збережена.",
                                    parse_mode=ParseMode.HTML)


def report_text(month: str) -> str:
    with closing(db()) as conn:
        sellers = conn.execute(
            "SELECT * FROM sellers WHERE client_id = ? AND (active = 1 OR id IN "
            "(SELECT seller_id FROM receipts WHERE month = ?)) ORDER BY name",
            (CLIENT_ID, month),
        ).fetchall()
    if not sellers:
        return "Продавців ще немає."
    w = max(len(s["name"]) for s in sellers)
    w = min(max(w, 5), 14)
    lines = [f"<b>{month_title(month)}</b>", "<pre>"]
    lines.append(f"{'':<{w}} {'План':>9} {'Факт':>9} {'%':>4} {'Залишок':>9}")
    tp = ts = 0.0
    for s in sellers:
        plan, _ = plan_for(s["id"], month)
        sold, _ = sold_for(s["id"], month)
        tp += plan or 0
        ts += sold
        name = s["name"][:w]
        if plan:
            pct = f"{sold / plan * 100:.0f}"
            left = plan - sold
            left_s = "✅" if left <= 0 else f"-{fmt(left)}"
            lines.append(f"{name:<{w}} {fmt(plan):>9} {fmt(sold):>9} {pct:>4} {left_s:>9}")
        else:
            lines.append(f"{name:<{w}} {'—':>9} {fmt(sold):>9} {'':>4} {'':>9}")
    pct = f"{ts / tp * 100:.0f}" if tp else ""
    lines.append("─" * (w + 35))
    lines.append(f"{'Разом':<{w}} {fmt(tp):>9} {fmt(ts):>9} {pct:>4} {('-' + fmt(tp - ts)) if tp > ts else '✅':>9}")
    lines.append("</pre>")
    return "\n".join(lines)


@admin_only
async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    month = parse_month(ctx.args[0] if ctx.args else None)
    if not month:
        await update.message.reply_text("Формат місяця: 2026-09")
        return
    await update.message.reply_text(report_text(month), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    month = parse_month(ctx.args[0] if ctx.args else None)
    if not month:
        await update.message.reply_text("Формат місяця: 2026-09")
        return
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT r.created_at, s.name, r.invoice_no, r.invoice_date, r.amount, r.status "
            "FROM receipts r JOIN sellers s ON s.id = r.seller_id "
            "WHERE s.client_id = ? AND r.month = ? ORDER BY r.created_at",
            (CLIENT_ID, month),
        ).fetchall()
    buf = io.StringIO()
    wr = csv.writer(buf, delimiter=";")
    wr.writerow(["Дата запису", "Продавець", "№ рахунку", "Дата рахунку", "Сума", "Статус"])
    for r in rows:
        wr.writerow([r["created_at"][:16].replace("T", " "), r["name"], r["invoice_no"] or "",
                     r["invoice_date"] or "", f"{r['amount']:.2f}".replace(".", ",") if r["amount"] else "",
                     r["status"]])
    data = io.BytesIO(("﻿" + buf.getvalue()).encode("utf-8"))
    data.name = f"receipts_{month}.csv"
    await update.message.reply_document(data, caption=f"Рахунки за {month_gen(month)}: {len(rows)}")


async def cmd_billing(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    month = parse_month(ctx.args[0] if ctx.args else None)
    if not month:
        await update.message.reply_text("Формат місяця: 2026-09")
        return
    text = billing_text(month, detailed=is_owner(uid))
    kb = None
    if is_owner(uid):
        d = billing_data(month)
        if d["status"] == "live":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📨 Виставити рахунок замовнику",
                                                             callback_data=f"inv:{month}")]])
        elif d["status"] == "issued":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("💰 Відмітити оплаченим",
                                                             callback_data=f"paid:{month}")]])
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ───────────────────────────── команди: власник ────────────────────

async def send_invoice(month: str, bot) -> dict:
    d = issue_invoice(month)
    await bot.send_message(ADMIN_ID, invoice_text(d), parse_mode=ParseMode.HTML)
    return d


@owner_only
async def cmd_invoice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    month = parse_month(ctx.args[0] if ctx.args else prev_month(this_month()))
    if not month:
        await update.message.reply_text("Формат місяця: 2026-09")
        return
    if billing_data(month)["status"] != "live":
        await update.message.reply_text("Рахунок за цей місяць уже виставлено.")
        return
    d = await send_invoice(month, ctx.bot)
    await update.message.reply_text("Надіслано замовнику:\n\n" + invoice_text(d), parse_mode=ParseMode.HTML)


def mark_paid(month: str) -> bool:
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "UPDATE invoices SET status = 'paid', paid_at = ? WHERE client_id = ? AND month = ? AND status = 'issued'",
            (now_iso(), CLIENT_ID, month),
        )
    return cur.rowcount > 0


@owner_only
async def cmd_paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    month = parse_month(ctx.args[0] if ctx.args else None)
    if not ctx.args or not month:
        await update.message.reply_text("Формат: /paid 2026-09")
        return
    if mark_paid(month):
        await update.message.reply_text(f"✅ {month_title(month)} відмічено оплаченим.")
        await ctx.bot.send_message(ADMIN_ID, f"✅ Оплату за {month_gen(month)} отримано, дякую!")
    else:
        await update.message.reply_text("Немає виставленого рахунку за цей місяць.")


async def on_billing_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer()
        return
    action, month = q.data.split(":")
    if action == "inv":
        if billing_data(month)["status"] != "live":
            await q.answer("Уже виставлено.")
            return
        d = await send_invoice(month, ctx.bot)
        await q.edit_message_text("Надіслано замовнику:\n\n" + invoice_text(d), parse_mode=ParseMode.HTML)
    else:
        if mark_paid(month):
            await q.edit_message_text(billing_text(month, detailed=True), parse_mode=ParseMode.HTML)
            await ctx.bot.send_message(ADMIN_ID, f"✅ Оплату за {month_gen(month)} отримано, дякую!")
    await q.answer()


@owner_only
async def cmd_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    rate = parse_amount(ctx.args[0]) if ctx.args else None
    if rate is None:
        await update.message.reply_text(f"Поточний курс: {usd_rate():.2f} грн/$. Змінити: /rate 42.1")
        return
    set_setting("usd_rate", str(rate))
    await update.message.reply_text(f"Курс оновлено: {rate:.2f} грн/$")


async def send_backup(bot) -> None:
    with closing(db()) as conn:
        bak = sqlite3.connect(":memory:")
        conn.backup(bak)
        buf = io.BytesIO()
        for line in bak.iterdump():
            buf.write((line + "\n").encode())
        bak.close()
    buf.seek(0)
    buf.name = f"bot_{now().strftime('%Y-%m-%d')}.sql"
    await bot.send_document(OWNER_ID, buf, caption="Резервна копія бази")


@owner_only
async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await send_backup(ctx.bot)


# ───────────────────────────── планові задачі ──────────────────────

async def job_daily(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Щодня: бекап власнику. 1-го числа: білінг за минулий місяць і перевірка планів."""
    try:
        await send_backup(ctx.bot)
    except Exception as e:
        log.error("Бекап не вдався: %s", e)
    if now().day != 1:
        return
    month = this_month()
    last = prev_month(month)
    text = billing_text(last, detailed=True)
    kb = None
    if billing_data(last)["status"] == "live":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📨 Виставити рахунок замовнику",
                                                         callback_data=f"inv:{last}")]])
    await ctx.bot.send_message(OWNER_ID, "Підсумок місяця:\n\n" + text, reply_markup=kb, parse_mode=ParseMode.HTML)

    with closing(db()) as conn:
        sellers = conn.execute(
            "SELECT * FROM sellers WHERE client_id = ? AND active = 1 ORDER BY name", (CLIENT_ID,)
        ).fetchall()
    carried = [s["name"] for s in sellers if plan_for(s["id"], month)[1]]
    missing = [s["name"] for s in sellers if plan_for(s["id"], month)[0] is None]
    msg = [f"Новий місяць: {month_title(month)}."]
    if carried:
        msg.append("Плани перенесено з минулого місяця: " + ", ".join(carried) + ". Змінити: /plan <ім'я> <сума>")
    if missing:
        msg.append("Без плану: " + ", ".join(missing))
    if len(msg) > 1:
        await ctx.bot.send_message(ADMIN_ID, "\n".join(msg))


async def post_init(app: Application) -> None:
    common = [BotCommand("start", "Почати"), BotCommand("help", "Довідка")]
    await app.bot.set_my_commands(common + [BotCommand("stat", "Мій план і результат")])
    admin_cmds = common + [
        BotCommand("report", "Зведення по продавцях"), BotCommand("list", "Продавці і плани"),
        BotCommand("plan", "Задати план"), BotCommand("export", "Рахунки в CSV"),
        BotCommand("billing", "Вартість обслуговування"),
    ]
    owner_cmds = admin_cmds + [BotCommand("invoice", "Виставити рахунок"), BotCommand("paid", "Відмітити оплату"),
                               BotCommand("rate", "Курс долара"), BotCommand("backup", "Копія бази")]
    for uid, cmds in ((ADMIN_ID, admin_cmds), (OWNER_ID, owner_cmds)):
        try:
            await app.bot.set_my_commands(cmds, scope=BotCommandScopeChat(uid))
        except Exception as e:  # чат ще не відкрито
            log.warning("Не вдалося задати меню для %s: %s", uid, e)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Помилка обробки", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Сталася помилка. Спробуйте ще раз.")
        except Exception:
            pass


# ───────────────────────────── запуск ──────────────────────────────

def main() -> None:
    global CLIENT_ID
    CLIENT_ID = init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    for name, fn in (
        ("start", cmd_start), ("help", cmd_help), ("stat", cmd_stat),
        ("add", cmd_add), ("list", cmd_list), ("plan", cmd_plan), ("rename", cmd_rename),
        ("remove", cmd_remove), ("report", cmd_report), ("export", cmd_export), ("billing", cmd_billing),
        ("invoice", cmd_invoice), ("paid", cmd_paid), ("rate", cmd_rate), ("backup", cmd_backup),
    ):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(on_receipt_button, pattern=r"^r[cex]:\d+$"))
    app.add_handler(CallbackQueryHandler(on_user_button, pattern=r"^u[ai]:\d+$"))
    app.add_handler(CallbackQueryHandler(on_billing_button, pattern=r"^(inv|paid):\d{4}-\d{2}$"))
    app.add_error_handler(on_error)

    app.job_queue.run_daily(job_daily, time=time(hour=9, minute=0, tzinfo=TZ))

    log.info("Бот запущено. Модель: %s, маржа %.0f%%, абонплата %.0f грн", MODEL, MARGIN_PERCENT, SUBSCRIPTION_UAH)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

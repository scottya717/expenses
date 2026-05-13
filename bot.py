import os
import re
import sqlite3
import csv
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from aiohttp import web

try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# ==================== CONFIG ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_PATH = "expenses.db"
DEFAULT_CATEGORIES = [
    "Продукты", "Транспорт", "Кафе", "Развлечения",
    "Здоровье", "Одежда", "Коммунальные", "Другое"
]

WAITING_AMOUNT, WAITING_CATEGORY, WAITING_EDIT_SELECT, WAITING_EDIT_FIELD, WAITING_EDIT_VALUE = range(5)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==================== DB ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            date TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def add_expense(user_id: int, amount: float, category: str, description: str = ""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO expenses (user_id, amount, category, description, date) VALUES (?, ?, ?, ?, ?)",
        (user_id, amount, category, description, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

def get_expenses(user_id: int, days: Optional[int] = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if days:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        c.execute(
            "SELECT id, amount, category, description, date FROM expenses WHERE user_id = ? AND date > ? ORDER BY date DESC",
            (user_id, since),
        )
    else:
        c.execute(
            "SELECT id, amount, category, description, date FROM expenses WHERE user_id = ? ORDER BY date DESC",
            (user_id,),
        )
    rows = c.fetchall()
    conn.close()
    return rows

def get_expense_by_id(expense_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, amount, category, description, date FROM expenses WHERE id = ? AND user_id = ?",
        (expense_id, user_id),
    )
    row = c.fetchone()
    conn.close()
    return row

def update_expense(expense_id: int, user_id: int, field: str, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        f"UPDATE expenses SET {field} = ? WHERE id = ? AND user_id = ?",
        (value, expense_id, user_id),
    )
    conn.commit()
    conn.close()

def delete_expense(expense_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id))
    conn.commit()
    conn.close()

def get_summary_by_category(user_id: int, days: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).isoformat()
    c.execute(
        """
        SELECT category, SUM(amount), COUNT(*) 
        FROM expenses 
        WHERE user_id = ? AND date > ? 
        GROUP BY category 
        ORDER BY SUM(amount) DESC
        """,
        (user_id, since),
    )
    rows = c.fetchall()
    conn.close()
    return rows

# ==================== OCR ====================
def extract_amount_from_text(text: str) -> Optional[float]:
    t = text.lower().replace(" ", "").replace(",", ".")
    patterns = [r'(\d[\d]*[.]?\d*)\s*[₽рp]', r'(\d{3,}[\d]*[.]?\d*)']
    for p in patterns:
        m = re.findall(p, t)
        if m:
            try:
                return float(m[0])
            except ValueError:
                continue
    return None

async def process_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OCR_AVAILABLE:
        await update.message.reply_text("⚠️ OCR не доступен.")
        return
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    path = f"photo_{update.message.from_user.id}.jpg"
    await file.download_to_drive(path)
    try:
        image = Image.open(path)
        text = pytesseract.image_to_string(image, lang="rus+eng")
        amount = extract_amount_from_text(text)
        if amount:
            context.user_data["ocr_amount"] = amount
            context.user_data["ocr_description"] = text[:200]
            keyboard = [
                [InlineKeyboardButton(cat, callback_data=f"ocr_cat_{cat}") for cat in DEFAULT_CATEGORIES[i:i+2]]
                for i in range(0, len(DEFAULT_CATEGORIES), 2)
            ]
            keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="ocr_cancel")])
            await update.message.reply_text(
                f"📸 Распознано: *{amount:.2f} ₽*\n_{text[:100].replace(chr(10), ' ')}..._\n\nВыбери категорию:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("❌ Сумма не распознана. Попробуй /add")
    except Exception as e:
        logger.error(f"OCR error: {e}")
        await update.message.reply_text("❌ Ошибка обработки.")
    finally:
        if os.path.exists(path):
            os.remove(path)

# ==================== HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Бот учёта трат!\n\n"
        "📸 Отправь скриншот чека\n"
        "✏️ /add — вручную\n"
        "📊 /week /month /categories — отчёты\n"
        "📝 /list — редактировать\n"
        "📤 /export — бэкап CSV\n\n"
        "Категории: " + ", ".join(DEFAULT_CATEGORIES)
    )

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введи сумму (250.50):")
    return WAITING_AMOUNT

async def add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(",", ".").replace(" ", ""))
        context.user_data["add_amount"] = amount
        keyboard = [
            [InlineKeyboardButton(cat, callback_data=f"addcat_{cat}") for cat in DEFAULT_CATEGORIES[i:i+2]]
            for i in range(0, len(DEFAULT_CATEGORIES), 2)
        ]
        await update.message.reply_text("Выбери категорию:", reply_markup=InlineKeyboardMarkup(keyboard))
        return WAITING_CATEGORY
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введи число:")
        return WAITING_AMOUNT

async def add_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.replace("addcat_", "")
    amount = context.user_data["add_amount"]
    add_expense(query.from_user.id, amount, category)
    await query.edit_message_text(f"✅ Добавлено: *{amount:.2f} ₽* — {category}", parse_mode="Markdown")
    return ConversationHandler.END

async def week_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    total = sum(r[1] for r in get_expenses(uid, 7))
    by_cat = get_summary_by_category(uid, 7)
    text = f"📊 *Неделя:* {total:.2f} ₽\n\n"
    for cat, s, c in by_cat:
        text += f"• {cat}: {s:.2f} ₽ ({c} шт.)\n"
    await update.message.reply_text(text or "Нет трат за неделю.", parse_mode="Markdown")

async def month_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    total = sum(r[1] for r in get_expenses(uid, 30))
    by_cat = get_summary_by_category(uid, 30)
    text = f"📊 *Месяц:* {total:.2f} ₽\n\n"
    for cat, s, c in by_cat:
        text += f"• {cat}: {s:.2f} ₽ ({c} шт.)\n"
    await update.message.reply_text(text or "Нет трат за месяц.", parse_mode="Markdown")

async def categories_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    by_cat = get_summary_by_category(uid, 30)
    if not by_cat:
        await update.message.reply_text("Нет данных.")
        return
    text = "📂 *Категории (30 дней):*\n\n"
    for cat, s, c in by_cat:
        text += f"• {cat}: {s:.2f} ₽ ({c} шт.)\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    rows = get_expenses(uid)
    if not rows:
        await update.message.reply_text("Нет данных.")
        return
    fn = f"export_{uid}.csv"
    with open(fn, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ID", "Сумма", "Категория", "Описание", "Дата"])
        w.writerows(rows)
    await update.message.reply_document(document=open(fn, "rb"), caption="📤 Экспорт трат")
    os.remove(fn)

# ==================== EDIT ====================
async def list_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    rows = get_expenses(uid, 30)
    if not rows:
        await update.message.reply_text("Нет записей.")
        return ConversationHandler.END
    keyboard = []
    text = "📝 *Последние траты:*\n\n"
    for row in rows[:20]:
        eid, amount, category, desc, date = row
        text += f"#{eid} | {date[:10]} | {amount:.0f} ₽ | {category}\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{eid} — {amount:.0f} ₽ ({category})", callback_data=f"edit_{eid}")])
    await update.message.reply_text(text + "\nВыбери запись:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return WAITING_EDIT_SELECT

async def edit_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    eid = int(query.data.replace("edit_", ""))
    context.user_data["edit_id"] = eid
    row = get_expense_by_id(eid, query.from_user.id)
    if not row:
        await query.edit_message_text("❌ Не найдено.")
        return ConversationHandler.END
    _, amount, category, desc, date = row
    keyboard = [
        [InlineKeyboardButton("💰 Сумма", callback_data="editfield_amount")],
        [InlineKeyboardButton("📂 Категория", callback_data="editfield_category")],
        [InlineKeyboardButton("🗑 Удалить", callback_data="editfield_delete")],
        [InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")],
    ]
    await query.edit_message_text(
        f"✏️ #{eid}:\nСумма: {amount:.2f} ₽\nКатегория: {category}\nДата: {date[:10]}\n\nЧто изменить?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_EDIT_FIELD

async def edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field = query.data.replace("editfield_", "")
    if field == "delete":
        delete_expense(context.user_data["edit_id"], query.from_user.id)
        await query.edit_message_text("🗑 Удалено.")
        return ConversationHandler.END
    if field == "cancel":
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END
    context.user_data["edit_field"] = field
    names = {"amount": "сумму", "category": "категорию"}
    await query.edit_message_text(f"Введи новую {names.get(field, field)}:")
    return WAITING_EDIT_VALUE

async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    eid = context.user_data["edit_id"]
    field = context.user_data["edit_field"]
    if field == "amount":
        try:
            val = float(update.message.text.replace(",", ".").replace(" ", ""))
            update_expense(eid, uid, "amount", val)
            await update.message.reply_text(f"✅ Сумма: {val:.2f} ₽")
        except ValueError:
            await update.message.reply_text("❌ Неверный формат.")
    elif field == "category":
        val = update.message.text.strip()
        update_expense(eid, uid, "category", val)
        await update.message.reply_text(f"✅ Категория: {val}")
    return ConversationHandler.END

async def ocr_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "ocr_cancel":
        await query.edit_message_text("❌ Отменено.")
        return
    category = query.data.replace("ocr_cat_", "")
    amount = context.user_data.get("ocr_amount", 0)
    desc = context.user_data.get("ocr_description", "")
    add_expense(query.from_user.id, amount, category, desc)
    await query.edit_message_text(f"✅ Сохранено: *{amount:.2f} ₽* — {category}\n_(со скриншота)_", parse_mode="Markdown")

# ==================== WEB SERVER ====================
async def health(request):
    return web.Response(text="Bot OK")

async def webhook(request):
    app = request.app['ptb_app']
    try:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500)

async def on_startup(app):
    await app['ptb_app'].initialize()
    await app['ptb_app'].start()
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if host:
        url = f"https://{host}/webhook"
        await app['ptb_app'].bot.set_webhook(url)
        logger.info(f"Webhook: {url}")

async def on_cleanup(app):
    await app['ptb_app'].stop()
    await app['ptb_app'].shutdown()

def main():
    init_db()
    ptb_app = Application.builder().token(BOT_TOKEN).build()

    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("week", week_report))
    ptb_app.add_handler(CommandHandler("month", month_report))
    ptb_app.add_handler(CommandHandler("categories", categories_report))
    ptb_app.add_handler(CommandHandler("export", export_csv))

    ptb_app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            WAITING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)],
            WAITING_CATEGORY: [CallbackQueryHandler(add_category_callback, pattern=r"^addcat_")],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("Отменено."))],
    ))

    ptb_app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("list", list_expenses)],
        states={
            WAITING_EDIT_SELECT: [CallbackQueryHandler(edit_select_callback, pattern=r"^edit_\d+$")],
            WAITING_EDIT_FIELD: [
                CallbackQueryHandler(edit_field_callback, pattern=r"^editfield_"),
                CallbackQueryHandler(lambda u, c: u.callback_query.edit_message_text("Отменено."), pattern=r"^edit_cancel$"),
            ],
            WAITING_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("Отменено."))],
    ))

    ptb_app.add_handler(CallbackQueryHandler(ocr_category_callback, pattern=r"^ocr_"))
    ptb_app.add_handler(MessageHandler(filters.PHOTO, process_photo))

    aio_app = web.Application()
    aio_app['ptb_app'] = ptb_app
    aio_app.router.add_get('/', health)
    aio_app.router.add_post('/webhook', webhook)
    aio_app.on_startup.append(on_startup)
    aio_app.on_cleanup.append(on_cleanup)

    port = int(os.environ.get("PORT", "8080"))
    web.run_app(aio_app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
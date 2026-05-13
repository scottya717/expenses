import os
import re
import sqlite3
import csv
import json
import base64
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from io import BytesIO

import aiohttp
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
from PIL import Image

# ==================== CONFIG ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID")

DB_PATH = "expenses.db"
DEFAULT_CATEGORIES = [
    "Продукты", "Транспорт", "Кафе", "Развлечения",
    "Здоровье", "Одежда", "Коммунальные", "Другое"
]

# Состояния
WAITING_AMOUNT, WAITING_CATEGORY, WAITING_DATE, \
WAITING_EDIT_SELECT, WAITING_EDIT_FIELD, WAITING_EDIT_VALUE, \
WAITING_SCREENSHOT_DATE, WAITING_SCREENSHOT_CONFIRM = range(8)

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

def add_expense(user_id: int, amount: float, category: str, description: str = "", date: Optional[str] = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    d = date or datetime.now().isoformat()
    c.execute(
        "INSERT INTO expenses (user_id, amount, category, description, date) VALUES (?, ?, ?, ?, ?)",
        (user_id, amount, category, description, d),
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

# ==================== YANDEX OCR ====================
async def yandex_ocr(image_bytes: bytes) -> str:
    """Распознаёт текст через Yandex Vision OCR"""
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        return ""
    
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    body = {
        "folderId": YANDEX_FOLDER_ID,
        "analyzeSpecs": [{
            "content": encoded,
            "features": [{"type": "TEXT_DETECTION", "textDetectionConfig": {"languageCodes": ["ru", "en"]}}]
        }]
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze",
            headers={"Authorization": f"Api-Key {YANDEX_API_KEY}", "Content-Type": "application/json"},
            json=body,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status != 200:
                logger.error(f"Yandex OCR error: {resp.status}")
                return ""
            data = await resp.json()
            texts = []
            for result in data.get("results", []):
                for page in result.get("results", []):
                    for block in page.get("textDetection", {}).get("pages", [{}])[0].get("blocks", []):
                        for line in block.get("lines", []):
                            line_text = " ".join(w.get("text", "") for w in line.get("words", []))
                            texts.append(line_text)
            return "\n".join(texts)

def parse_expenses_from_text(text: str) -> List[Tuple[str, float]]:
    """Извлекает пары (описание, сумма) из текста"""
    lines = text.split("\n")
    expenses = []
    
    for line in lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue
        
        # Ищем число в конце или середине строки
        # Паттерны: "Продукты 1250", "Кафе - 340 руб", "Такси 1 250 ₽"
        match = re.search(r'^(.*?)\s+([\d\s]+[.,]?\d*)\s*[₽рp]?$', line)
        if not match:
            match = re.search(r'^(.*?)\s*[-–:]\s*([\d\s]+[.,]?\d*)\s*[₽рp]?$', line)
        
        if match:
            desc = match.group(1).strip()
            amount_str = match.group(2).replace(" ", "").replace(",", ".")
            try:
                amount = float(amount_str)
                if amount > 0 and amount < 1000000:  # разумные пределы
                    expenses.append((desc, amount))
            except ValueError:
                continue
    
    return expenses

def guess_category(description: str) -> str:
    """Угадывает категорию по описанию"""
    desc_lower = description.lower()
    keywords = {
        "Продукты": ["продукт", "пятероч", "магнит", "перекрест", "азбука", "лента", " Spar ", "вкусно", "еда", "овощ", "мясо", "молоко", "хлеб", "овощи", "фрукты", "супермаркет", "гипер", "покупка"],
        "Транспорт": ["такси", "метро", "автобус", "трамвай", "электричк", "поезд", "билет", "яндекс такси", "uber", "ситимобил", "бензин", "заправк", "парковка", "транспорт"],
        "Кафе": ["кафе", "ресторан", "кофе", "кофейня", "шоколадница", "старбакс", "kfc", "макдоналдс", "бургер", "пицца", "суши", "доставка", "обед", "ужин", "покушать", "поесть"],
        "Развлечения": ["кино", "театр", "концерт", "игра", "steam", "playstation", "xbox", "книг", "подписка", "netflix", "spotify", "музыка", "развлеч"],
        "Здоровье": ["аптек", "лекарств", "врач", "больниц", "клиник", "анализ", "массаж", "стоматолог", "зуб", "терапевт", "медицин", "здоровье"],
        "Одежда": ["одежда", "обувь", "zara", "h&m", "уникло", "спортмастер", "lamoda", "wildberries", "ozon", "шмотк", "куртк", "джинс", "футболк"],
        "Коммунальные": ["жкх", "коммунал", "интернет", "свет", "вода", "газ", "аренда", "ипотек", "квартплат", "тинькофф жкх"],
    }
    
    for cat, words in keywords.items():
        for word in words:
            if word in desc_lower:
                return cat
    return "Другое"

# ==================== SCREENSHOT FLOW ====================
async def process_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает скриншот дня — распознаёт несколько трат"""
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    
    # Скачиваем в память
    bio = BytesIO()
    await file.download_to_memory(bio)
    bio.seek(0)
    image_bytes = bio.read()
    
    # Распознаём
    await update.message.reply_text("🔍 Распознаю текст...")
    text = await yandex_ocr(image_bytes)
    
    if not text:
        await update.message.reply_text(
            "❌ Не удалось распознать текст.\n\n"
            "Убедись, что:\n"
            "• На скриншоте видны суммы\n"
            "• Текст не размыт\n\n"
            "Или добавь вручную: /add"
        )
        return
    
    # Парсим траты
    expenses = parse_expenses_from_text(text)
    
    if not expenses:
        await update.message.reply_text(
            f"❌ Не нашёл трат в тексте.\n\nВот что распознал:\n```\n{text[:500]}\n```\n\nПопробуй /add",
            parse_mode="Markdown"
        )
        return
    
    # Сохраняем для следующего шага
    context.user_data["screenshot_expenses"] = expenses
    context.user_data["screenshot_text"] = text
    
    # Показываем что распознали
    msg = "📸 *Распознано:*\n\n"
    for i, (desc, amount) in enumerate(expenses, 1):
        cat = guess_category(desc)
        msg += f"{i}. {desc} — *{amount:.0f} ₽* ({cat})\n"
    
    msg += f"\nВсего трат: {len(expenses)}\n\nУкажи дату (ДД.ММ.YYYY) или напиши 'сегодня':"
    
    await update.message.reply_text(msg, parse_mode="Markdown")
    return WAITING_SCREENSHOT_DATE

async def screenshot_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает дату для трат со скриншота"""
    text = update.message.text.strip().lower()
    
    if text == "сегодня":
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        try:
            # Пробуем разные форматы
            for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(text, fmt)
                    date_str = dt.strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
            else:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Неверный формат. Введи ДД.ММ.YYYY или 'сегодня':")
            return WAITING_SCREENSHOT_DATE
    
    context.user_data["screenshot_date"] = date_str
    expenses = context.user_data["screenshot_expenses"]
    
    # Показываем итоговый список с категориями
    msg = f"📅 *Дата:* {date_str}\n\n*Траты:*\n"
    for desc, amount in expenses:
        cat = guess_category(desc)
        msg += f"• {desc} — {amount:.0f} ₽ ({cat})\n"
    
    msg += f"\n*Итого:* {sum(e[1] for e in expenses):.0f} ₽"
    
    keyboard = [
        [InlineKeyboardButton("✅ Сохранить все", callback_data="ss_save_all")],
        [InlineKeyboardButton("📝 Изменить категории", callback_data="ss_edit_cats")],
        [InlineKeyboardButton("❌ Отмена", callback_data="ss_cancel")],
    ]
    
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return WAITING_SCREENSHOT_CONFIRM

async def screenshot_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает подтверждение скриншота"""
    query = update.callback_query
    await query.answer()
    action = query.data
    
    if action == "ss_cancel":
        await query.edit_message_text("❌ Отменено.")
        context.user_data.pop("screenshot_expenses", None)
        return ConversationHandler.END
    
    if action == "ss_save_all":
        expenses = context.user_data.get("screenshot_expenses", [])
        date_str = context.user_data.get("screenshot_date", datetime.now().isoformat())
        uid = query.from_user.id
        
        for desc, amount in expenses:
            cat = guess_category(desc)
            add_expense(uid, amount, cat, desc, date_str)
        
        total = sum(e[1] for e in expenses)
        await query.edit_message_text(
            f"✅ Сохранено *{len(expenses)}* трат на сумму *{total:.0f} ₽*",
            parse_mode="Markdown"
        )
        context.user_data.pop("screenshot_expenses", None)
        return ConversationHandler.END
    
    if action == "ss_edit_cats":
        # Показываем кнопки для изменения категорий по очереди
        expenses = context.user_data.get("screenshot_expenses", [])
        if not expenses:
            await query.edit_message_text("❌ Ошибка.")
            return ConversationHandler.END
        
        # Начинаем с первой траты
        context.user_data["ss_edit_index"] = 0
        return await show_category_selector(query, context)

async def show_category_selector(query, context: ContextTypes.DEFAULT_TYPE):
    """Показывает выбор категории для текущей траты"""
    idx = context.user_data.get("ss_edit_index", 0)
    expenses = context.user_data.get("screenshot_expenses", [])
    
    if idx >= len(expenses):
        # Все категории выбраны, сохраняем
        return await save_all_after_edit(query, context)
    
    desc, amount = expenses[idx]
    current_cat = guess_category(desc)
    
    keyboard = []
    for i in range(0, len(DEFAULT_CATEGORIES), 2):
        row = []
        for cat in DEFAULT_CATEGORIES[i:i+2]:
            prefix = "✅ " if cat == current_cat else ""
            row.append(InlineKeyboardButton(f"{prefix}{cat}", callback_data=f"sscat_{cat}"))
        keyboard.append(row)
    
    await query.edit_message_text(
        f"📝 Трата {idx+1}/{len(expenses)}:\n*{desc}* — {amount:.0f} ₽\n\nВыбери категорию:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def screenshot_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор категории для траты со скриншота"""
    query = update.callback_query
    await query.answer()
    category = query.data.replace("sscat_", "")
    
    idx = context.user_data.get("ss_edit_index", 0)
    expenses = context.user_data.get("screenshot_expenses", [])
    
    if idx < len(expenses):
        # Заменяем категорию (сохраняем в отдельном списке)
        if "ss_categories" not in context.user_data:
            context.user_data["ss_categories"] = []
        
        # Дополняем список категорий до текущего индекса
        while len(context.user_data["ss_categories"]) <= idx:
            desc, amount = expenses[len(context.user_data["ss_categories"])]
            context.user_data["ss_categories"].append(guess_category(desc))
        
        context.user_data["ss_categories"][idx] = category
        context.user_data["ss_edit_index"] = idx + 1
        
        return await show_category_selector(query, context)
    
    return ConversationHandler.END

async def save_all_after_edit(query, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет все траты после ручного выбора категорий"""
    expenses = context.user_data.get("screenshot_expenses", [])
    categories = context.user_data.get("ss_categories", [])
    date_str = context.user_data.get("screenshot_date", datetime.now().isoformat())
    uid = query.from_user.id
    
    for i, (desc, amount) in enumerate(expenses):
        cat = categories[i] if i < len(categories) else guess_category(desc)
        add_expense(uid, amount, cat, desc, date_str)
    
    total = sum(e[1] for e in expenses)
    await query.edit_message_text(
        f"✅ Сохранено *{len(expenses)}* трат на сумму *{total:.0f} ₽*",
        parse_mode="Markdown"
    )
    
    # Чистим
    for key in ["screenshot_expenses", "screenshot_date", "screenshot_text", "ss_categories", "ss_edit_index"]:
        context.user_data.pop(key, None)
    
    return ConversationHandler.END

# ==================== MANUAL ADD ====================
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

# ==================== REPORTS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Бот учёта трат!\n\n"
        "📸 Отправь скриншот со списком трат за день\n"
        "✏️ /add — добавить вручную\n"
        "📊 /week /month /categories — отчёты\n"
        "📝 /list — редактировать или удалить\n"
        "📤 /export — бэкап CSV\n\n"
        "Категории: " + ", ".join(DEFAULT_CATEGORIES)
    )

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
    host = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RENDER_EXTERNAL_HOSTNAME")
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

    # Ручное добавление
    ptb_app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            WAITING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)],
            WAITING_CATEGORY: [CallbackQueryHandler(add_category_callback, pattern=r"^addcat_")],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("Отменено."))],
        per_message=True,
    ))

    # Редактирование
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
        per_message=True,
    ))

    # Скриншот (не ConversationHandler — чтобы не конфликтовал с другими)
    ptb_app.add_handler(MessageHandler(filters.PHOTO, process_screenshot))
    ptb_app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        screenshot_date,
        block=False,
    ))
    
    # Callback для скриншота (вне ConversationHandler)
    ptb_app.add_handler(CallbackQueryHandler(screenshot_confirm_callback, pattern=r"^ss_"))
    ptb_app.add_handler(CallbackQueryHandler(screenshot_category_callback, pattern=r"^sscat_"))

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

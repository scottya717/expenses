import os
import re
import sqlite3
import csv
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

# Volume path для Railway (не сбрасывается при redeploy)
DB_PATH = "/app/data/expenses.db"

# Состояния
WAITING_AMOUNT, WAITING_CATEGORY, WAITING_NEW_CATEGORY, \
WAITING_EDIT_SELECT, WAITING_EDIT_FIELD, WAITING_EDIT_VALUE = range(6)

STATE_IDLE = "idle"
STATE_SCREENSHOT_DATE = "screenshot_date"
STATE_SCREENSHOT_CONFIRM = "screenshot_confirm"
STATE_SCREENSHOT_EDIT_CAT = "screenshot_edit_cat"
STATE_SCREENSHOT_DELETE = "screenshot_delete"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==================== DB ====================
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_categories (
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            PRIMARY KEY (user_id, category)
        )
    """)
    conn.commit()
    conn.close()

def get_user_categories(user_id: int) -> List[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT category FROM user_categories WHERE user_id = ?", (user_id,))
    custom = [r[0] for r in c.fetchall()]
    conn.close()
    
    default = ["Продукты", "Транспорт", "Кафе", "Развлечения",
               "Здоровье", "Одежда", "Коммунальные", "Другое"]
    seen = set()
    result = []
    for cat in custom + default:
        if cat not in seen:
            seen.add(cat)
            result.append(cat)
    return result

def add_user_category(user_id: int, category: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO user_categories (user_id, category) VALUES (?, ?)", (user_id, category))
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
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        logger.error("YANDEX_API_KEY or YANDEX_FOLDER_ID not set")
        return ""
    
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    body = {
        "folderId": YANDEX_FOLDER_ID,
        "analyzeSpecs": [{
            "content": encoded,
            "features": [{"type": "TEXT_DETECTION", "textDetectionConfig": {"languageCodes": ["ru", "en"]}}]
        }]
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze",
                headers={"Authorization": f"Api-Key {YANDEX_API_KEY}", "Content-Type": "application/json"},
                json=body,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Yandex OCR error: {resp.status} - {text}")
                    return ""
                data = await resp.json()
                texts = []
                for result in data.get("results", []):
                    for page in result.get("results", []):
                        text_data = page.get("textDetection", {})
                        for p in text_data.get("pages", [{}]):
                            for block in p.get("blocks", []):
                                for line in block.get("lines", []):
                                    line_text = " ".join(w.get("text", "") for w in line.get("words", []))
                                    texts.append(line_text)
                return "\n".join(texts)
    except Exception as e:
        logger.error(f"Yandex OCR exception: {e}")
        return ""

# ==================== PARSER ====================
def parse_bank_screenshot(text: str) -> Tuple[Optional[str], List[Tuple[str, float]]]:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    expenses = []
    current_date = None
    i = 0
    
    date_patterns = [
        (r'(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)', 'ru'),
        (r'(\d{1,2})\.(\d{1,2})\.(\d{2,4})', 'num'),
        (r'(\d{1,2})/(\d{1,2})/(\d{2,4})', 'num'),
    ]
    
    months_ru = {
        'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6,
        'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12
    }
    
    def extract_amount(s: str) -> Optional[float]:
        if re.match(r'^\s*\+', s):
            return None
        
        patterns = [
            r'[-–]?\s*([\d\s]+[.,]\d{2})\s*[₽PРp]',
            r'[-–]?\s*([\d\s]+)\s*[₽PРp]',
            r'[-–]?\s*([\d\s]+[.,]\d{2})',
            r'[-–]?\s*([\d\s]{3,})',
        ]
        for p in patterns:
            m = re.search(p, s)
            if m:
                try:
                    val = m.group(1).replace(" ", "").replace(",", ".")
                    return abs(float(val))
                except ValueError:
                    continue
        return None
    
    def is_service_line(line: str) -> bool:
        service_words = [
            "переводы", "двойной чёрный", "дебетовая карта", "супермаркеты",
            "местный транспорт", "перевод", "зачисление", "доходы", "траты",
            "счета и карты", "без переводов", "операции", "все операции",
            "счёт", "карта", "остаток", "баланс", "пополнение", "зачисление",
            "входящий", "возврат", "кэшбэк"
        ]
        line_lower = line.lower()
        return any(sw in line_lower for sw in service_words) or line in ["+1", "+2", "+3", ")", "("]
    
    def is_income(line: str) -> bool:
        income_markers = ["зачисление", "пополнение", "возврат", "кэшбэк", "доход", "зарплата", "перевод от"]
        line_lower = line.lower()
        return any(m in line_lower for m in income_markers) or re.match(r'^\s*\+', line)
    
    while i < len(lines):
        line = lines[i]
        
        if is_income(line):
            i += 1
            continue
        
        found_date = False
        for pattern, ptype in date_patterns:
            m = re.match(pattern, line, re.IGNORECASE)
            if m:
                try:
                    if ptype == 'ru':
                        day = int(m.group(1))
                        month = months_ru.get(m.group(2).lower(), 1)
                        year = datetime.now().year
                        current_date = f"{year:04d}-{month:02d}-{day:02d}"
                    else:
                        day = int(m.group(1))
                        month = int(m.group(2))
                        year = int(m.group(3))
                        if year < 100:
                            year += 2000
                        current_date = f"{year:04d}-{month:02d}-{day:02d}"
                except (ValueError, IndexError):
                    pass
                found_date = True
                break
        
        if found_date:
            i += 1
            continue
        
        if not is_service_line(line) and not is_income(line) and i + 1 < len(lines):
            next_line = lines[i + 1]
            
            if is_income(next_line):
                i += 2
                continue
            
            amount = extract_amount(next_line)
            
            if amount and amount > 0:
                desc = line
                i += 2
                while i < len(lines) and (is_service_line(lines[i]) or is_income(lines[i])):
                    i += 1
                expenses.append((desc, amount))
                continue
        
        i += 1
    
    return current_date, expenses

def guess_category(description: str, user_categories: List[str]) -> str:
    desc_lower = description.lower()
    keywords = {
        "Продукты": ["продукт", "пятероч", "магнит", "перекрест", "азбука", "лента", " Spar ", "вкусно", "еда", "овощ", "мясо", "молоко", "хлеб", "овощи", "фрукты", "супермаркет", "гипер", "покупка", "магазин", "торговый центр"],
        "Транспорт": ["такси", "метро", "автобус", "трамвай", "электричк", "поезд", "билет", "яндекс такси", "uber", "ситимобил", "бензин", "заправк", "парковка", "транспорт", "городской транспорт", "местный транспорт", "ярослав"],
        "Кафе": ["кафе", "ресторан", "кофе", "кофейня", "шоколадница", "старбакс", "kfc", "макдоналдс", "бургер", "пицца", "суши", "доставка", "обед", "ужин", "покушать", "поесть", "двойной чёрный", "чёрный", "капучино", "латте"],
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
    user_id = update.message.from_user.id
    
    state = context.user_data.get("state", STATE_IDLE)
    if state != STATE_IDLE:
        await update.message.reply_text("⏳ Сначала заверши текущую операцию или отправь /cancel")
        return
    
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    
    bio = BytesIO()
    await file.download_to_memory(bio)
    bio.seek(0)
    image_bytes = bio.read()
    
    await update.message.reply_text("🔍 Распознаю текст...")
    text = await yandex_ocr(image_bytes)
    
    if not text:
        await update.message.reply_text(
            "❌ Не удалось распознать текст.\n"
            "Проверь YANDEX_API_KEY и YANDEX_FOLDER_ID в Railway.\n"
            "Или добавь вручную: /add"
        )
        return
    
    parsed_date, expenses = parse_bank_screenshot(text)
    
    if not expenses:
        await update.message.reply_text(
            f"❌ Не нашёл трат в тексте.\n\nВот что распознал:\n```\n{text[:800]}\n```\n\n"
            f"Попробуй /add",
            parse_mode="Markdown"
        )
        return
    
    context.user_data["screenshot_expenses"] = expenses
    context.user_data["screenshot_text"] = text
    context.user_data["parsed_date"] = parsed_date
    context.user_data["state"] = STATE_SCREENSHOT_DATE
    
    msg = f"📸 *Распознано трат: {len(expenses)}*\n\n"
    for i, (desc, amount) in enumerate(expenses, 1):
        cat = guess_category(desc, get_user_categories(user_id))
        msg += f"{i}. {desc} — *{amount:.2f} ₽* ({cat})\n"
    
    total = sum(e[1] for e in expenses)
    msg += f"\n💰 *Итого:* {total:.2f} ₽"
    
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    day_before = today - timedelta(days=2)
    
    date_buttons = [
        InlineKeyboardButton("📅 Сегодня", callback_data=f"ssdate_{today.strftime('%Y-%m-%d')}"),
        InlineKeyboardButton("📅 Вчера", callback_data=f"ssdate_{yesterday.strftime('%Y-%m-%d')}"),
        InlineKeyboardButton(f"📅 {day_before.strftime('%d.%m')}", callback_data=f"ssdate_{day_before.strftime('%Y-%m-%d')}"),
    ]
    
    if parsed_date:
        try:
            dt = datetime.strptime(parsed_date, "%Y-%m-%d")
            date_str = dt.strftime("%d.%m")
            existing = [today, yesterday, day_before]
            if not any(d.strftime("%Y-%m-%d") == parsed_date for d in existing):
                date_buttons.insert(0, InlineKeyboardButton(f"📅 {date_str} (со скриншота)", callback_data=f"ssdate_{parsed_date}"))
        except:
            pass
    
    keyboard = [date_buttons]
    keyboard.append([InlineKeyboardButton("✏️ Ввести дату вручную", callback_data="ssdate_manual")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="ss_cancel")])
    
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def screenshot_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    
    if action == "ss_cancel":
        await query.edit_message_text("❌ Отменено.")
        clear_screenshot_data(context)
        return
    
    if action == "ssdate_manual":
        await query.edit_message_text("Введи дату в формате ДД.ММ.YYYY:")
        context.user_data["state"] = STATE_SCREENSHOT_DATE
        return
    
    date_str = action.replace("ssdate_", "")
    context.user_data["screenshot_date"] = date_str
    
    await show_confirm_screen(query, context)

async def handle_screenshot_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    state = context.user_data.get("state")
    
    if state != STATE_SCREENSHOT_DATE:
        return
    
    text = update.message.text.strip().lower()
    
    if text == "сегодня":
        date_str = datetime.now().strftime("%Y-%m-%d")
    elif text == "вчера":
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        try:
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
            await update.message.reply_text("❌ Неверный формат. Введи ДД.ММ.YYYY, 'сегодня' или 'вчера':")
            return
    
    context.user_data["screenshot_date"] = date_str
    context.user_data["state"] = STATE_SCREENSHOT_CONFIRM
    
    await show_confirm_message(update, context)

async def show_confirm_screen(query, context: ContextTypes.DEFAULT_TYPE):
    expenses = context.user_data.get("screenshot_expenses", [])
    date_str = context.user_data.get("screenshot_date", "не указана")
    user_id = query.from_user.id
    
    msg = f"📅 *Дата:* {date_str}\n\n*Траты:*\n"
    for i, (desc, amount) in enumerate(expenses, 1):
        cat = guess_category(desc, get_user_categories(user_id))
        msg += f"{i}. {desc} — {amount:.2f} ₽ ({cat})\n"
    
    total = sum(e[1] for e in expenses)
    msg += f"\n💰 *Итого:* {total:.2f} ₽"
    
    keyboard = [
        [InlineKeyboardButton("✅ Сохранить все", callback_data="ss_save_all")],
        [InlineKeyboardButton("📝 Изменить категории", callback_data="ss_edit_cats")],
        [InlineKeyboardButton("🗑 Удалить траты", callback_data="ss_delete")],
        [InlineKeyboardButton("📅 Изменить дату", callback_data="ss_change_date")],
        [InlineKeyboardButton("❌ Отмена", callback_data="ss_cancel")],
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_confirm_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expenses = context.user_data.get("screenshot_expenses", [])
    date_str = context.user_data.get("screenshot_date", "не указана")
    user_id = update.message.from_user.id
    
    msg = f"📅 *Дата:* {date_str}\n\n*Траты:*\n"
    for i, (desc, amount) in enumerate(expenses, 1):
        cat = guess_category(desc, get_user_categories(user_id))
        msg += f"{i}. {desc} — {amount:.2f} ₽ ({cat})\n"
    
    total = sum(e[1] for e in expenses)
    msg += f"\n💰 *Итого:* {total:.2f} ₽"
    
    keyboard = [
        [InlineKeyboardButton("✅ Сохранить все", callback_data="ss_save_all")],
        [InlineKeyboardButton("📝 Изменить категории", callback_data="ss_edit_cats")],
        [InlineKeyboardButton("🗑 Удалить траты", callback_data="ss_delete")],
        [InlineKeyboardButton("📅 Изменить дату", callback_data="ss_change_date")],
        [InlineKeyboardButton("❌ Отмена", callback_data="ss_cancel")],
    ]
    
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def screenshot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    
    if action == "ss_cancel":
        await query.edit_message_text("❌ Отменено.")
        clear_screenshot_data(context)
        return
    
    if action == "ss_change_date":
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        day_before = today - timedelta(days=2)
        
        keyboard = [
            [
                InlineKeyboardButton("📅 Сегодня", callback_data=f"ssdate_{today.strftime('%Y-%m-%d')}"),
                InlineKeyboardButton("📅 Вчера", callback_data=f"ssdate_{yesterday.strftime('%Y-%m-%d')}"),
                InlineKeyboardButton(f"📅 {day_before.strftime('%d.%m')}", callback_data=f"ssdate_{day_before.strftime('%Y-%m-%d')}"),
            ],
            [InlineKeyboardButton("✏️ Ввести дату вручную", callback_data="ssdate_manual")],
            [InlineKeyboardButton("❌ Отмена", callback_data="ss_cancel")],
        ]
        
        await query.edit_message_text(
            "📅 *Выбери дату:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        context.user_data["state"] = STATE_SCREENSHOT_DATE
        return
    
    if action == "ss_save_all":
        expenses = context.user_data.get("screenshot_expenses", [])
        date_str = context.user_data.get("screenshot_date", datetime.now().isoformat())
        uid = query.from_user.id
        
        for desc, amount in expenses:
            cat = guess_category(desc, get_user_categories(uid))
            add_expense(uid, amount, cat, desc, date_str)
        
        total = sum(e[1] for e in expenses)
        await query.edit_message_text(
            f"✅ Сохранено *{len(expenses)}* трат на сумму *{total:.2f} ₽*",
            parse_mode="Markdown"
        )
        clear_screenshot_data(context)
        return
    
    if action == "ss_edit_cats":
        expenses = context.user_data.get("screenshot_expenses", [])
        if not expenses:
            await query.edit_message_text("❌ Ошибка.")
            clear_screenshot_data(context)
            return
        
        context.user_data["ss_edit_index"] = 0
        context.user_data["ss_categories"] = [guess_category(e[0], get_user_categories(query.from_user.id)) for e in expenses]
        context.user_data["state"] = STATE_SCREENSHOT_EDIT_CAT
        await show_category_selector(query, context)
    
    if action == "ss_delete":
        expenses = context.user_data.get("screenshot_expenses", [])
        if not expenses:
            await query.edit_message_text("❌ Нет трат для удаления.")
            clear_screenshot_data(context)
            return
        
        context.user_data["state"] = STATE_SCREENSHOT_DELETE
        await show_delete_selector(query, context)

async def show_delete_selector(query, context: ContextTypes.DEFAULT_TYPE):
    expenses = context.user_data.get("screenshot_expenses", [])
    
    keyboard = []
    for i, (desc, amount) in enumerate(expenses):
        keyboard.append([InlineKeyboardButton(f"🗑 {i+1}. {desc} — {amount:.0f} ₽", callback_data=f"ssdel_{i}")])
    
    keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="ssdel_done")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="ss_cancel")])
    
    await query.edit_message_text(
        "🗑 *Выбери траты для удаления:*\n(Нажми ещё раз, чтобы отменить удаление)\n",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def screenshot_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    
    if action == "ss_cancel":
        await query.edit_message_text("❌ Отменено.")
        clear_screenshot_data(context)
        return
    
    if action == "ssdel_done":
        expenses = context.user_data.get("screenshot_expenses", [])
        if not expenses:
            await query.edit_message_text("❌ Все траты удалены.")
            clear_screenshot_data(context)
            return
        
        deleted = context.user_data.get("ss_deleted", set())
        remaining = [(d, a) for i, (d, a) in enumerate(expenses) if i not in deleted]
        context.user_data["screenshot_expenses"] = remaining
        
        await show_confirm_screen(query, context)
        return
    
    idx = int(action.replace("ssdel_", ""))
    deleted = context.user_data.setdefault("ss_deleted", set())
    
    if idx in deleted:
        deleted.remove(idx)
        await query.answer(f"Трата {idx+1} восстановлена")
    else:
        deleted.add(idx)
        await query.answer(f"Трата {idx+1} отмечена для удаления")
    
    await show_delete_selector(query, context)

async def screenshot_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.replace("sscat_", "")
    
    idx = context.user_data.get("ss_edit_index", 0)
    expenses = context.user_data.get("screenshot_expenses", [])
    
    if idx >= len(expenses):
        await save_all_after_edit(query, context)
        return
    
    context.user_data["ss_categories"][idx] = category
    context.user_data["ss_edit_index"] = idx + 1
    
    if context.user_data["ss_edit_index"] >= len(expenses):
        await save_all_after_edit(query, context)
    else:
        await show_category_selector(query, context)

async def show_category_selector(query, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data.get("ss_edit_index", 0)
    expenses = context.user_data.get("screenshot_expenses", [])
    
    if idx >= len(expenses):
        await save_all_after_edit(query, context)
        return
    
    desc, amount = expenses[idx]
    current_cat = context.user_data["ss_categories"][idx]
    user_cats = get_user_categories(query.from_user.id)
    
    keyboard = []
    for i in range(0, len(user_cats), 2):
        row = []
        for cat in user_cats[i:i+2]:
            prefix = "✅ " if cat == current_cat else ""
            row.append(InlineKeyboardButton(f"{prefix}{cat}", callback_data=f"sscat_{cat}"))
        keyboard.append(row)
    
    await query.edit_message_text(
        f"📝 Трата {idx+1}/{len(expenses)}:\n*{desc}* — {amount:.2f} ₽\n\nВыбери категорию:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def save_all_after_edit(query, context: ContextTypes.DEFAULT_TYPE):
    expenses = context.user_data.get("screenshot_expenses", [])
    categories = context.user_data.get("ss_categories", [])
    date_str = context.user_data.get("screenshot_date", datetime.now().isoformat())
    uid = query.from_user.id
    
    for i, (desc, amount) in enumerate(expenses):
        cat = categories[i] if i < len(categories) else guess_category(desc, get_user_categories(uid))
        add_expense(uid, amount, cat, desc, date_str)
    
    total = sum(e[1] for e in expenses)
    await query.edit_message_text(
        f"✅ Сохранено *{len(expenses)}* трат на сумму *{total:.2f} ₽*",
        parse_mode="Markdown"
    )
    
    clear_screenshot_data(context)

def clear_screenshot_data(context: ContextTypes.DEFAULT_TYPE):
    keys = ["screenshot_expenses", "screenshot_date", "screenshot_text", "parsed_date", 
            "ss_categories", "ss_edit_index", "ss_deleted", "state"]
    for key in keys:
        context.user_data.pop(key, None)

# ==================== CUSTOM CATEGORIES ====================
async def setcategory_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_screenshot_data(context)
    await update.message.reply_text(
        "📝 Введи название новой категории:\n"
        "(например: Спорт, Подарки, Обучение)\n\n"
        "Текущие категории:\n" + "\n".join(f"• {c}" for c in get_user_categories(update.message.from_user.id))
    )
    return WAITING_NEW_CATEGORY

async def setcategory_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = update.message.text.strip()
    if not category or len(category) > 50:
        await update.message.reply_text("❌ Название слишком длинное или пустое. Попробуй ещё:")
        return WAITING_NEW_CATEGORY
    
    add_user_category(update.message.from_user.id, category)
    await update.message.reply_text(
        f"✅ Категория *{category}* добавлена!\n\n"
        f"Теперь она будет в списке при добавлении трат.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ==================== MANUAL ADD ====================
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_screenshot_data(context)
    await update.message.reply_text("Введи сумму (250.50):")
    return WAITING_AMOUNT

async def add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(",", ".").replace(" ", ""))
        context.user_data["add_amount"] = amount
        user_cats = get_user_categories(update.message.from_user.id)
        keyboard = [
            [InlineKeyboardButton(cat, callback_data=f"addcat_{cat}") for cat in user_cats[i:i+2]]
            for i in range(0, len(user_cats), 2)
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
    clear_screenshot_data(context)
    cats = get_user_categories(update.message.from_user.id)
    await update.message.reply_text(
        "👋 Бот учёта трат!\n\n"
        "📸 Отправь скриншот из банковского приложения\n"
        "✏️ /add — добавить вручную\n"
        "📊 /week /month /categories — отчёты\n"
        "📝 /list — редактировать или удалить\n"
        "📤 /export — бэкап CSV\n"
        "🏷 /setcategory — добавить свою категорию\n"
        "❌ /cancel — отменить\n\n"
        "Категории: " + ", ".join(cats)
    )

async def week_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_screenshot_data(context)
    uid = update.message.from_user.id
    total = sum(r[1] for r in get_expenses(uid, 7))
    by_cat = get_summary_by_category(uid, 7)
    text = f"📊 *Неделя:* {total:.2f} ₽\n\n"
    for cat, s, c in by_cat:
        text += f"• {cat}: {s:.2f} ₽ ({c} шт.)\n"
    await update.message.reply_text(text or "Нет трат за неделю.", parse_mode="Markdown")

async def month_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_screenshot_data(context)
    uid = update.message.from_user.id
    total = sum(r[1] for r in get_expenses(uid, 30))
    by_cat = get_summary_by_category(uid, 30)
    text = f"📊 *Месяц:* {total:.2f} ₽\n\n"
    for cat, s, c in by_cat:
        text += f"• {cat}: {s:.2f} ₽ ({c} шт.)\n"
    await update.message.reply_text(text or "Нет трат за месяц.", parse_mode="Markdown")

async def categories_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_screenshot_data(context)
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
    clear_screenshot_data(context)
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

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_screenshot_data(context)
    await update.message.reply_text("✅ Операция отменена.")

# ==================== EDIT ====================
async def list_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_screenshot_data(context)
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
    ptb_app.add_handler(CommandHandler("cancel", cancel_cmd))

    ptb_app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("setcategory", setcategory_start)],
        states={
            WAITING_NEW_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, setcategory_save)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    ))

    ptb_app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            WAITING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)],
            WAITING_CATEGORY: [CallbackQueryHandler(add_category_callback, pattern=r"^addcat_")],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
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
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    ))

    ptb_app.add_handler(MessageHandler(filters.PHOTO, process_screenshot))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_screenshot_date))
    
    ptb_app.add_handler(CallbackQueryHandler(screenshot_date_callback, pattern=r"^ssdate_"))
    ptb_app.add_handler(CallbackQueryHandler(screenshot_callback, pattern=r"^ss_"))
    ptb_app.add_handler(CallbackQueryHandler(screenshot_category_callback, pattern=r"^sscat_"))
    ptb_app.add_handler(CallbackQueryHandler(screenshot_delete_callback, pattern=r"^ssdel_"))

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

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
    ContextTypes,
    filters,
)
from aiohttp import web

# ==================== CONFIG ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID")

DB_PATH = "/app/data/expenses.db"

STATE_IDLE = "idle"
STATE_ADD_AMOUNT = "add_amount"
STATE_ADD_CATEGORY = "add_category"
STATE_SETCATEGORY = "setcategory"
STATE_LIST_SELECT = "list_select"
STATE_LIST_FIELD = "list_field"
STATE_LIST_VALUE = "list_value"
STATE_IMPORT = "import"
STATE_SCREENSHOT_DATE = "screenshot_date"
STATE_SCREENSHOT_CONFIRM = "screenshot_confirm"
STATE_SCREENSHOT_EDIT_CAT = "screenshot_edit_cat"
STATE_SCREENSHOT_EDIT_AMOUNT = "screenshot_edit_amount"
STATE_SCREENSHOT_DELETE = "screenshot_delete"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.DEBUG,  # DEBUG для максимального логирования
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
            r'[-–]?\s*([\d\s]{2,})',
        ]
        for p in patterns:
            m = re.search(p, s)
            if m:
                try:
                    val = m.group(1).replace(" ", "").replace(",", ".")
                    f = abs(float(val))
                    if f > 0 and f < 1000000:
                        return f
                except ValueError:
                    continue
        return None
    
    def is_service_line(line: str) -> bool:
        line_lower = line.lower().strip()
        
        exact_service = {
            "переводы", "двойной чёрный", "двойной черный", "дебетовая карта",
            "местный транспорт", "перевод", "зачисление", "доходы", "траты",
            "счета и карты", "без переводов", "операции", "все операции",
            "счёт", "карта", "остаток", "баланс", "пополнение", "зачисление",
            "входящий", "возврат", "кэшбэк", ")", "(", "+1", "+2", "+3",
        }
        if line_lower in exact_service:
            return True
        
        section_headers = {
            "счета и карты", "без переводов", "все операции", "операции",
            "доходы", "траты", "переводы", "остаток", "баланс",
        }
        if line_lower in section_headers:
            return True
        
        category_headers = {
            "супермаркеты", "кафе и рестораны", "развлечения", "здоровье",
            "одежда", "коммунальные", "связь", "образование", "спорт",
            "фото и копицентры", "фото и копи центры", "фото",
        }
        if line_lower in category_headers:
            return True
        
        # "Местный транспорт" — всегда служебная
        if "местный транспорт" in line_lower:
            return True
        
        # "Городской транспорт" — если есть сумма, это трата
        if "транспорт" in line_lower:
            if extract_amount(line) is not None:
                return False
            if len(line_lower) < 30:
                return True
        
        return False
    
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
        "Транспорт": ["такси", "метро", "автобус", "трамвай", "электричк", "поезд", "билет", "яндекс такси", "uber", "ситимобил", "бензин", "заправк", "парковка", "транспорт", "городской транспорт", "ярослав"],
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
    logger.info(f"PHOTO received from user {update.message.from_user.id}")
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
    
    logger.info(f"OCR text length: {len(text)}")
    parsed_date, expenses = parse_bank_screenshot(text)
    logger.info(f"Parsed: date={parsed_date}, expenses={len(expenses)}")
    
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
    context.user_data["state"] = STATE_SCREENSHOT_CONFIRM
    
    await show_confirm_screen(query, context)

async def handle_screenshot_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        [InlineKeyboardButton("💰 Изменить суммы", callback_data="ss_edit_amounts")],
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
        [InlineKeyboardButton("💰 Изменить суммы", callback_data="ss_edit_amounts")],
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
        return
    
    if action == "ss_edit_amounts":
        expenses = context.user_data.get("screenshot_expenses", [])
        if not expenses:
            await query.edit_message_text("❌ Ошибка.")
            clear_screenshot_data(context)
            return
        
        context.user_data["ss_edit_index"] = 0
        context.user_data["ss_amounts"] = [amount for _, amount in expenses]
        context.user_data["state"] = STATE_SCREENSHOT_EDIT_AMOUNT
        await show_amount_editor(query, context)
        return
    
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

# ==================== EDIT AMOUNTS ====================
async def show_amount_editor(query, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data.get("ss_edit_index", 0)
    expenses = context.user_data.get("screenshot_expenses", [])
    
    if idx >= len(expenses):
        await show_confirm_screen(query, context)
        return
    
    desc, amount = expenses[idx]
    
    keyboard = [
        [InlineKeyboardButton("✅ Оставить как есть", callback_data=f"ssamt_keep")],
        [InlineKeyboardButton("❌ Отмена", callback_data="ss_cancel")],
    ]
    
    await query.edit_message_text(
        f"💰 Трата {idx+1}/{len(expenses)}:\n*{desc}*\nТекущая сумма: *{amount:.2f} ₽*\n\n"
        f"Введи новую сумму или нажми 'Оставить как есть':",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def screenshot_amount_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    
    if action == "ss_cancel":
        await query.edit_message_text("❌ Отменено.")
        clear_screenshot_data(context)
        return
    
    if action == "ssamt_keep":
        idx = context.user_data.get("ss_edit_index", 0)
        context.user_data["ss_edit_index"] = idx + 1
        
        expenses = context.user_data.get("screenshot_expenses", [])
        if context.user_data["ss_edit_index"] >= len(expenses):
            await show_confirm_screen(query, context)
        else:
            await show_amount_editor(query, context)
        return

async def handle_screenshot_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    
    if state != STATE_SCREENSHOT_EDIT_AMOUNT:
        return
    
    text = update.message.text.strip().replace(",", ".").replace(" ", "")
    
    try:
        new_amount = float(text)
        if new_amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введи положительное число:")
        return
    
    idx = context.user_data.get("ss_edit_index", 0)
    expenses = context.user_data.get("screenshot_expenses", [])
    
    if idx < len(expenses):
        desc, _ = expenses[idx]
        expenses[idx] = (desc, new_amount)
        context.user_data["screenshot_expenses"] = expenses
        
        amounts = context.user_data.get("ss_amounts", [])
        if idx < len(amounts):
            amounts[idx] = new_amount
            context.user_data["ss_amounts"] = amounts
    
    context.user_data["ss_edit_index"] = idx + 1
    
    if context.user_data["ss_edit_index"] >= len(expenses):
        await update.message.reply_text("✅ Все суммы отредактированы.")
        await show_confirm_message(update, context)
    else:
        await show_amount_editor_message(update, context)

async def show_amount_editor_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data.get("ss_edit_index", 0)
    expenses = context.user_data.get("screenshot_expenses", [])
    
    if idx >= len(expenses):
        await show_confirm_message(update, context)
        return
    
    desc, amount = expenses[idx]
    
    keyboard = [
        [InlineKeyboardButton("✅ Оставить как есть", callback_data=f"ssamt_keep")],
        [InlineKeyboardButton("❌ Отмена", callback_data="ss_cancel")],
    ]
    
    await update.message.reply_text(
        f"💰 Трата {idx+1}/{len(expenses)}:\n*{desc}*\nТекущая сумма: *{amount:.2f} ₽*\n\n"
        f"Введи новую сумму или нажми 'Оставить как есть':",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ==================== EDIT CATEGORIES ====================
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
    
    keyboard.append([InlineKeyboardButton("💰 Изменить сумму", callback_data="sscat_edit_amount")])
    
    await query.edit_message_text(
        f"📝 Трата {idx+1}/{len(expenses)}:\n*{desc}* — {amount:.2f} ₽\n\nВыбери категорию:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def screenshot_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    
    if action == "ss_cancel":
        await query.edit_message_text("❌ Отменено.")
        clear_screenshot_data(context)
        return
    
    if action == "sscat_edit_amount":
        context.user_data["state"] = STATE_SCREENSHOT_EDIT_AMOUNT
        await show_amount_editor(query, context)
        return
    
    category = action.replace("sscat_", "")
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
            "ss_categories", "ss_edit_index", "ss_deleted", "ss_amounts", "state"]
    for key in keys:
        context.user_data.pop(key, None)

# ==================== ADD EXPENSE MANUALLY ====================
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"CMD /add from user {update.message.from_user.id}")
    clear_screenshot_data(context)
    context.user_data["state"] = STATE_ADD_AMOUNT
    await update.message.reply_text("Введи сумму (250.50):")

async def handle_add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"handle_add_amount called, state={context.user_data.get('state')}")
    if context.user_data.get("state") != STATE_ADD_AMOUNT:
        return
    
    try:
        amount = float(update.message.text.replace(",", ".").replace(" ", ""))
        context.user_data["add_amount"] = amount
        context.user_data["state"] = STATE_ADD_CATEGORY
        user_cats = get_user_categories(update.message.from_user.id)
        keyboard = [
            [InlineKeyboardButton(cat, callback_data=f"addcat_{cat}") for cat in user_cats[i:i+2]]
            for i in range(0, len(user_cats), 2)
        ]
        await update.message.reply_text("Выбери категорию:", reply_markup=InlineKeyboardMarkup(keyboard))
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введи число:")

async def add_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if context.user_data.get("state") != STATE_ADD_CATEGORY:
        return
    
    category = query.data.replace("addcat_", "")
    amount = context.user_data["add_amount"]
    add_expense(query.from_user.id, amount, category)
    context.user_data["state"] = STATE_IDLE
    await query.edit_message_text(f"✅ Добавлено: *{amount:.2f} ₽* — {category}", parse_mode="Markdown")

# ==================== CUSTOM CATEGORIES ====================
async def cmd_setcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"CMD /setcategory from user {update.message.from_user.id}")
    clear_screenshot_data(context)
    context.user_data["state"] = STATE_SETCATEGORY
    await update.message.reply_text(
        "📝 Введи название новой категории:\n"
        "(например: Спорт, Подарки, Обучение)\n\n"
        "Текущие категории:\n" + "\n".join(f"• {c}" for c in get_user_categories(update.message.from_user.id))
    )

async def handle_setcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"handle_setcategory called, state={context.user_data.get('state')}")
    if context.user_data.get("state") != STATE_SETCATEGORY:
        return
    
    category = update.message.text.strip()
    if not category or len(category) > 50:
        await update.message.reply_text("❌ Название слишком длинное или пустое. Попробуй ещё:")
        return
    
    add_user_category(update.message.from_user.id, category)
    context.user_data["state"] = STATE_IDLE
    await update.message.reply_text(
        f"✅ Категория *{category}* добавлена!\n\n"
        f"Теперь она будет в списке при добавлении трат.",
        parse_mode="Markdown"
    )

# ==================== LIST & EDIT ====================
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"CMD /list from user {update.message.from_user.id}")
    clear_screenshot_data(context)
    uid = update.message.from_user.id
    rows = get_expenses(uid, 30)
    if not rows:
        await update.message.reply_text("Нет записей.")
        context.user_data["state"] = STATE_IDLE
        return
    
    context.user_data["state"] = STATE_LIST_SELECT
    keyboard = []
    text = "📝 *Последние траты:*\n\n"
    for row in rows[:20]:
        eid, amount, category, desc, date = row
        text += f"#{eid} | {date[:10]} | {amount:.0f} ₽ | {category}\n"
        keyboard.append([InlineKeyboardButton(f"✏️ #{eid} — {amount:.0f} ₽ ({category})", callback_data=f"edit_{eid}")])
    await update.message.reply_text(text + "\nВыбери запись:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def edit_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if context.user_data.get("state") != STATE_LIST_SELECT:
        return
    
    eid = int(query.data.replace("edit_", ""))
    context.user_data["edit_id"] = eid
    row = get_expense_by_id(eid, query.from_user.id)
    if not row:
        await query.edit_message_text("❌ Не найдено.")
        context.user_data["state"] = STATE_IDLE
        return
    
    _, amount, category, desc, date = row
    keyboard = [
        [InlineKeyboardButton("💰 Сумма", callback_data="editfield_amount")],
        [InlineKeyboardButton("📂 Категория", callback_data="editfield_category")],
        [InlineKeyboardButton("🗑 Удалить", callback_data="editfield_delete")],
        [InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")],
    ]
    context.user_data["state"] = STATE_LIST_FIELD
    await query.edit_message_text(
        f"✏️ #{eid}:\nСумма: {amount:.2f} ₽\nКатегория: {category}\nДата: {date[:10]}\n\nЧто изменить?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if context.user_data.get("state") != STATE_LIST_FIELD:
        return
    
    field = query.data.replace("editfield_", "")
    if field == "delete":
        delete_expense(context.user_data["edit_id"], query.from_user.id)
        context.user_data["state"] = STATE_IDLE
        await query.edit_message_text("🗑 Удалено.")
        return
    if field == "cancel":
        context.user_data["state"] = STATE_IDLE
        await query.edit_message_text("Отменено.")
        return
    
    context.user_data["edit_field"] = field
    context.user_data["state"] = STATE_LIST_VALUE
    names = {"amount": "сумму", "category": "категорию"}
    await query.edit_message_text(f"Введи новую {names.get(field, field)}:")

async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"edit_value called, state={context.user_data.get('state')}")
    if context.user_data.get("state") != STATE_LIST_VALUE:
        return
    
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
    
    context.user_data["state"] = STATE_IDLE

# ==================== IMPORT CSV ====================
async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"CMD /import from user {update.message.from_user.id}")
    clear_screenshot_data(context)
    context.user_data["state"] = STATE_IMPORT
    await update.message.reply_text(
        "📥 *Импорт из CSV*\n\n"
        "Отправь файл `.csv` со следующими колонками:\n"
        "`Сумма,Категория,Описание,Дата`\n\n"
        "Дата в формате `YYYY-MM-DD` или `DD.MM.YYYY`\n"
        "Разделитель — запятая или точка с запятой\n\n"
        "❌ /cancel — отменить",
        parse_mode="Markdown"
    )

async def handle_import_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"handle_import_file called, state={context.user_data.get('state')}")
    if context.user_data.get("state") != STATE_IMPORT:
        return
    
    user_id = update.message.from_user.id
    
    if not update.message.document:
        await update.message.reply_text("❌ Пожалуйста, отправь файл CSV.")
        return
    
    doc = update.message.document
    if not doc.file_name.lower().endswith('.csv'):
        await update.message.reply_text("❌ Нужен файл с расширением `.csv`")
        return
    
    try:
        file = await context.bot.get_file(doc.file_id)
        bio = BytesIO()
        await file.download_to_memory(bio)
        bio.seek(0)
        content = bio.read().decode('utf-8-sig')
    except Exception as e:
        logger.error(f"Import download error: {e}")
        await update.message.reply_text("❌ Ошибка загрузки файла.")
        context.user_data["state"] = STATE_IDLE
        return
    
    lines = content.strip().split('\n')
    if not lines:
        await update.message.reply_text("❌ Файл пустой.")
        context.user_data["state"] = STATE_IDLE
        return
    
    first_line = lines[0]
    delimiter = ';' if ';' in first_line else ','
    
    imported = 0
    errors = []
    
    start_idx = 0
    header_keywords = ['сумма', 'amount', 'категория', 'category', 'дата', 'date', 'описание', 'description']
    first_lower = first_line.lower()
    if any(kw in first_lower for kw in header_keywords):
        start_idx = 1
    
    for i, line in enumerate(lines[start_idx:], start=start_idx+1):
        line = line.strip()
        if not line:
            continue
        
        parts = [p.strip().strip('"').strip("'") for p in line.split(delimiter)]
        
        if len(parts) < 2:
            errors.append(f"Строка {i}: мало колонок")
            continue
        
        amount = None
        category = None
        description = ""
        date_str = None
        
        for j, part in enumerate(parts):
            clean = part.replace(" ", "").replace(",", ".").replace("₽", "").replace("р", "")
            try:
                val = float(clean)
                if val > 0 and amount is None:
                    amount = val
                    amount_idx = j
                    break
            except ValueError:
                continue
        
        if amount is None:
            errors.append(f"Строка {i}: не найдена сумма")
            continue
        
        date_patterns = [
            (r'^\d{4}-\d{2}-\d{2}$', '%Y-%m-%d'),
            (r'^\d{2}\.\d{2}\.\d{4}$', '%d.%m.%Y'),
            (r'^\d{2}\.\d{2}\.\d{2}$', '%d.%m.%y'),
            (r'^\d{2}/\d{2}/\d{4}$', '%d/%m/%Y'),
        ]
        
        for j, part in enumerate(parts):
            if j == amount_idx:
                continue
            for pattern, fmt in date_patterns:
                if re.match(pattern, part):
                    try:
                        dt = datetime.strptime(part, fmt)
                        date_str = dt.strftime('%Y-%m-%d')
                        date_idx = j
                        break
                    except ValueError:
                        continue
            if date_str:
                break
        
        if date_str is None:
            date_str = datetime.now().strftime('%Y-%m-%d')
            date_idx = -1
        
        remaining = [(j, p) for j, p in enumerate(parts) if j != amount_idx and j != date_idx]
        
        if len(remaining) >= 1:
            cats_lower = [c.lower() for c in get_user_categories(user_id)]
            found_cat = False
            
            for j, part in remaining:
                if part.lower() in cats_lower or len(part) <= 30:
                    category = part
                    cat_idx = j
                    found_cat = True
                    break
            
            if not found_cat:
                category = remaining[0][1]
                cat_idx = remaining[0][0]
            
            desc_parts = [p for j, p in remaining if j != cat_idx]
            description = " ".join(desc_parts).strip() or ""
        
        if not category:
            category = "Другое"
        
        add_expense(user_id, amount, category, description, date_str)
        imported += 1
        
        if category not in get_user_categories(user_id):
            add_user_category(user_id, category)
    
    msg = f"✅ *Импорт завершён*\n\n"
    msg += f"📥 Импортировано: *{imported}* записей\n"
    if errors:
        msg += f"⚠️ Ошибок: *{len(errors)}*\n"
        msg += "\n".join(errors[:10])
        if len(errors) > 10:
            msg += f"\n... и ещё {len(errors) - 10}"
    
    await update.message.reply_text(msg, parse_mode="Markdown")
    context.user_data["state"] = STATE_IDLE

# ==================== REPORTS ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"CMD /start from user {update.message.from_user.id}")
    clear_screenshot_data(context)
    cats = get_user_categories(update.message.from_user.id)
    await update.message.reply_text(
        "👋 Бот учёта трат!\n\n"
        "📸 Отправь скриншот из банковского приложения\n"
        "✏️ /add — добавить вручную\n"
        "📊 /week /month /categories — отчёты\n"
        "📝 /list — редактировать или удалить\n"
        "📤 /export — бэкап CSV\n"
        "📥 /import — загрузить CSV\n"
        "🏷 /setcategory — добавить свою категорию\n"
        "❌ /cancel — отменить\n\n"
        "Категории: " + ", ".join(cats)
    )

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"CMD /week from user {update.message.from_user.id}")
    clear_screenshot_data(context)
    uid = update.message.from_user.id
    total = sum(r[1] for r in get_expenses(uid, 7))
    by_cat = get_summary_by_category(uid, 7)
    text = f"📊 *Неделя:* {total:.2f} ₽\n\n"
    for cat, s, c in by_cat:
        text += f"• {cat}: {s:.2f} ₽ ({c} шт.)\n"
    await update.message.reply_text(text or "Нет трат за неделю.", parse_mode="Markdown")

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"CMD /month from user {update.message.from_user.id}")
    clear_screenshot_data(context)
    uid = update.message.from_user.id
    total = sum(r[1] for r in get_expenses(uid, 30))
    by_cat = get_summary_by_category(uid, 30)
    text = f"📊 *Месяц:* {total:.2f} ₽\n\n"
    for cat, s, c in by_cat:
        text += f"• {cat}: {s:.2f} ₽ ({c} шт.)\n"
    await update.message.reply_text(text or "Нет трат за месяц.", parse_mode="Markdown")

async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"CMD /categories from user {update.message.from_user.id}")
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

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"CMD /export from user {update.message.from_user.id}")
    clear_screenshot_data(context)
    uid = update.message.from_user.id
    rows = get_expenses(uid)
    if not rows:
        await update.message.reply_text("Нет данных.")
        return
    fn = f"export_{uid}.csv"
    with open(fn, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=';', lineterminator='\n')
        w.writerow(["Сумма", "Категория", "Описание", "Дата"])
        for row in rows:
            eid, amount, category, desc, date = row
            w.writerow([amount, category, desc or "", date[:10]])
    await update.message.reply_document(document=open(fn, "rb"), caption="📤 Экспорт трат")
    os.remove(fn)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"CMD /cancel from user {update.message.from_user.id}, state was {context.user_data.get('state')}")
    clear_screenshot_data(context)
    context.user_data["state"] = STATE_IDLE
    await update.message.reply_text("✅ Операция отменена.")

# ==================== UNIFIED MESSAGE HANDLER ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Единый обработчик всех текстовых сообщений и документов."""
    state = context.user_data.get("state", STATE_IDLE)
    logger.info(f"handle_message: state={state}, text={update.message.text[:50] if update.message.text else 'None'}")
    
    if update.message.document and state == STATE_IMPORT:
        await handle_import_file(update, context)
        return
    
    if state == STATE_IDLE:
        return
    elif state == STATE_ADD_AMOUNT:
        await handle_add_amount(update, context)
    elif state == STATE_SETCATEGORY:
        await handle_setcategory(update, context)
    elif state == STATE_LIST_VALUE:
        await edit_value(update, context)
    elif state == STATE_SCREENSHOT_DATE:
        await handle_screenshot_date(update, context)
    elif state == STATE_SCREENSHOT_EDIT_AMOUNT:
        await handle_screenshot_amount(update, context)
    else:
        logger.warning(f"Unknown state: {state}")

# ==================== WEB SERVER ====================
async def health(request):
    return web.Response(text="Bot OK")

async def webhook(request):
    app = request.app['ptb_app']
    try:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        logger.info(f"Webhook received: update_id={update.update_id}, message={update.message is not None}, callback_query={update.callback_query is not None}")
        await app.process_update(update)
        logger.info(f"Webhook processed successfully")
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return web.Response(status=500)

async def on_startup(app):
    logger.info("=== ON STARTUP ===")
    await app['ptb_app'].initialize()
    await app['ptb_app'].start()
    logger.info("PTB app started")
    host = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if host:
        url = f"https://{host}/webhook"
        await app['ptb_app'].bot.set_webhook(url)
        logger.info(f"Webhook set: {url}")

async def on_cleanup(app):
    logger.info("=== ON CLEANUP ===")
    await app['ptb_app'].stop()
    await app['ptb_app'].shutdown()

def main():
    logger.info("=== MAIN START ===")
    init_db()
    ptb_app = Application.builder().token(BOT_TOKEN).build()
    logger.info(f"PTB app built, handlers count: {len(ptb_app.handlers)}")

    # === CommandHandler'ы ===
    ptb_app.add_handler(CommandHandler("start", cmd_start))
    ptb_app.add_handler(CommandHandler("add", cmd_add))
    ptb_app.add_handler(CommandHandler("list", cmd_list))
    ptb_app.add_handler(CommandHandler("setcategory", cmd_setcategory))
    ptb_app.add_handler(CommandHandler("import", cmd_import))
    ptb_app.add_handler(CommandHandler("week", cmd_week))
    ptb_app.add_handler(CommandHandler("month", cmd_month))
    ptb_app.add_handler(CommandHandler("categories", cmd_categories))
    ptb_app.add_handler(CommandHandler("export", cmd_export))
    ptb_app.add_handler(CommandHandler("cancel", cmd_cancel))
    logger.info(f"Command handlers added, total handlers: {len(ptb_app.handlers)}")

    # === CallbackQueryHandler'ы ===
    ptb_app.add_handler(CallbackQueryHandler(add_category_callback, pattern=r"^addcat_"))
    ptb_app.add_handler(CallbackQueryHandler(edit_select_callback, pattern=r"^edit_\d+$"))
    ptb_app.add_handler(CallbackQueryHandler(edit_field_callback, pattern=r"^editfield_"))
    
    # Скриншот callback'и
    ptb_app.add_handler(CallbackQueryHandler(screenshot_date_callback, pattern=r"^ssdate_"))
    ptb_app.add_handler(CallbackQueryHandler(screenshot_callback, pattern=r"^ss_"))
    ptb_app.add_handler(CallbackQueryHandler(screenshot_category_callback, pattern=r"^sscat_"))
    ptb_app.add_handler(CallbackQueryHandler(screenshot_delete_callback, pattern=r"^ssdel_"))
    ptb_app.add_handler(CallbackQueryHandler(screenshot_amount_callback, pattern=r"^ssamt_"))
    logger.info(f"Callback handlers added, total handlers: {len(ptb_app.handlers)}")

    # === MessageHandler'ы ===
    ptb_app.add_handler(MessageHandler(filters.PHOTO, process_screenshot))
    ptb_app.add_handler(MessageHandler(filters.TEXT | filters.Document.ALL, handle_message))
    logger.info(f"Message handlers added, total handlers: {len(ptb_app.handlers)}")

    aio_app = web.Application()
    aio_app['ptb_app'] = ptb_app
    aio_app.router.add_get('/', health)
    aio_app.router.add_post('/webhook', webhook)
    aio_app.on_startup.append(on_startup)
    aio_app.on_cleanup.append(on_cleanup)

    port = int(os.environ.get("PORT", "8080"))
    logger.info(f"Starting web server on port {port}")
    web.run_app(aio_app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()

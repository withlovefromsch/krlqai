import os
import sqlite3
from datetime import datetime, timedelta, date
from calendar import monthrange
from typing import Set

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler,
    ContextTypes
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ---------- ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ----------
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле!")

# Загружаем ID администраторов
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_STR.split(",") if id.strip().isdigit()]
if not ADMIN_IDS:
    raise ValueError("ADMIN_IDS не найден в .env файле!")

ALL_ADMINS: Set[int] = set(ADMIN_IDS)

# Дополнительные настройки
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "10"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "0"))

# Состояния для диалогов
ADMIN_ADD_DATE, ADMIN_ADD_SLOTS = 10, 11
CLIENT_SERVICE, CLIENT_DATE, CLIENT_TIME, CLIENT_PHONE, CLIENT_PET_NAME, CLIENT_BREED, CLIENT_COMMENT = 20, 21, 22, 23, 24, 25, 26
ADMIN_ANALYTICS_START, ADMIN_ANALYTICS_END = 30, 31
ADMIN_MONTH_SELECT = 40
ADMIN_CANCEL_APPOINTMENT = 50


# ---------- БАЗА ДАННЫХ ----------
def init_db():
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    
    # Создаём таблицу services
    cur.execute('''CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE
    )''')
    
    # Создаём таблицу slots
    cur.execute('''CREATE TABLE IF NOT EXISTS slots (
        id INTEGER PRIMARY KEY,
        date TEXT,
        start_time TEXT,
        end_time TEXT,
        UNIQUE(date, start_time, end_time)
    )''')
    
    # Создаём таблицу appointments (сразу с полем comment)
    cur.execute('''CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        username TEXT,
        service TEXT,
        date TEXT,
        start_time TEXT,
        end_time TEXT,
        phone TEXT,
        pet_name TEXT,
        breed TEXT,
        price INTEGER DEFAULT 0,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        slot_id INTEGER,
        comment TEXT DEFAULT ''
    )''')
    
    # Обновляем услуги
    cur.execute("DELETE FROM services")
    services = ["🛁 Купание", "✨ Комплексный уход", "📝 Уход по запросу"]
    for s in services:
        cur.execute("INSERT INTO services (name) VALUES (?)", (s,))
    
    conn.commit()
    conn.close()


init_db()


def is_admin(user_id: int) -> bool:
    return user_id in ALL_ADMINS


def format_date(date_str: str) -> str:
    """Конвертирует дату из формата YYYY-MM-DD в DD.MM.YY"""
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        return date_obj.strftime("%d.%m.%y")
    except:
        return date_str


def get_free_slots(date_str):
    """Получает свободные слоты на дату"""
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute("SELECT start_time, end_time, id FROM slots WHERE date=? AND id NOT IN (SELECT slot_id FROM appointments WHERE status IN ('pending','confirmed') AND date=?)", (date_str, date_str))
    rows = cur.fetchall()
    conn.close()
    return [(r[0], r[1], r[2]) for r in rows]


def get_all_slots(date_str):
    """Получает все слоты на дату (и свободные, и занятые)"""
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute("SELECT start_time, end_time, id FROM slots WHERE date=?", (date_str,))
    rows = cur.fetchall()
    conn.close()
    return [(r[0], r[1], r[2]) for r in rows]


def get_available_dates():
    """Возвращает даты, на которых есть свободные слоты"""
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date FROM slots WHERE id NOT IN (SELECT slot_id FROM appointments WHERE status IN ('pending','confirmed'))")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_services():
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute("SELECT name FROM services")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


def add_slots(date_str, intervals):
    """Добавляет новые слоты"""
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    added = []
    existed = []
    for interval in intervals:
        if '-' not in interval:
            continue
        start, end = interval.split('-')
        start = start.strip()
        end = end.strip()
        try:
            cur.execute("INSERT INTO slots (date, start_time, end_time) VALUES (?,?,?)",
                        (date_str, start, end))
            added.append(f"{start}-{end}")
        except sqlite3.IntegrityError:
            existed.append(f"{start}-{end}")
    conn.commit()
    conn.close()
    return added, existed


def create_appointment(user_id, username, service, date_str, start_time, end_time, phone, pet_name, breed, slot_id, comment=""):
    """Создаёт новую запись, привязанную к конкретному слоту"""
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute('''INSERT INTO appointments 
        (user_id, username, service, date, start_time, end_time, phone, pet_name, breed, status, slot_id, comment)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (user_id, username, service, date_str, start_time, end_time, phone, pet_name, breed, "pending", slot_id, comment))
    app_id = cur.lastrowid
    conn.commit()
    conn.close()
    return app_id


def confirm_appointment(app_id, price):
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute("UPDATE appointments SET status='confirmed', price=? WHERE id=?", (price, app_id))
    conn.commit()
    conn.close()


def cancel_appointment_by_admin(app_id):
    """Отменяет запись админом - удаляем запись, слот становится свободным"""
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    # Получаем данные записи перед удалением
    cur.execute("SELECT user_id, pet_name, slot_id, date, service, start_time, end_time, phone, comment FROM appointments WHERE id=?", (app_id,))
    row = cur.fetchone()
    if row:
        user_id, pet_name, slot_id, date_str, service, start_time, end_time, phone, comment = row
        # Удаляем запись
        cur.execute("DELETE FROM appointments WHERE id=?", (app_id,))
        conn.commit()
        conn.close()
        return user_id, pet_name, slot_id, date_str, service, start_time, end_time, phone, comment
    conn.close()
    return None, None, None, None, None, None, None, None, None


def get_all_appointments():
    """Получает все активные записи (для админа)"""
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute('''SELECT id, user_id, username, service, date, start_time, end_time, phone, pet_name, breed, price, status, comment
                  FROM appointments WHERE status IN ('pending','confirmed') 
                  ORDER BY date, start_time''')
    rows = cur.fetchall()
    conn.close()
    return rows


def get_user_active_appointments(user_id):
    """Для клиента - без телефона"""
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute('''SELECT id, service, date, start_time, end_time, status, price, pet_name, breed, comment
                  FROM appointments WHERE user_id=? AND status IN ('pending','confirmed') 
                  ORDER BY date, start_time''', (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_appointments_next_week():
    """Для админа - с телефоном"""
    today = date.today()
    end = today + timedelta(days=7)
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute('''SELECT id, user_id, username, service, date, start_time, end_time, phone, pet_name, breed, price, status, comment
                  FROM appointments WHERE date BETWEEN ? AND ? ORDER BY date, start_time''',
                  (today.isoformat(), end.isoformat()))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_appointments_by_month(year, month):
    """Для админа - с телефоном"""
    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = date(year, month + 1, 1) - timedelta(days=1)
    
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute('''SELECT id, user_id, username, service, date, start_time, end_time, phone, pet_name, breed, price, status, comment
                  FROM appointments WHERE date BETWEEN ? AND ? ORDER BY date, start_time''',
                  (start_date.isoformat(), end_date.isoformat()))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_appointments_by_date(target_date):
    """Получает все записи на конкретную дату"""
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute('''SELECT id, user_id, username, service, date, start_time, end_time, phone, pet_name, breed, price, status, comment
                  FROM appointments WHERE date=? ORDER BY start_time''', (target_date.isoformat(),))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_financial_summary(start_date, end_date):
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute("SELECT SUM(price) FROM appointments WHERE status='confirmed' AND date BETWEEN ? AND ?",
                (start_date.isoformat(), end_date.isoformat()))
    total = cur.fetchone()[0]
    conn.close()
    return total if total else 0


def get_appointment_by_id(app_id):
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute("SELECT user_id, service, date, start_time, end_time, pet_name, breed, phone, price, status, comment FROM appointments WHERE id=?", (app_id,))
    row = cur.fetchone()
    conn.close()
    return row


# ---------- КАЛЕНДАРЬ ----------
def build_calendar_for_client(year, month, prefix="client_date"):
    month_names = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
    first_day_weekday, days_in_month = monthrange(year, month)
    available_dates = get_available_dates()
    
    keyboard = []
    row = [InlineKeyboardButton(f"📅 {month_names[month-1]} {year}", callback_data=f"{prefix}_ignore")]
    row.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}_prev_{year}_{month}"))
    row.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}_next_{year}_{month}"))
    keyboard.append(row)
    
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard.append([InlineKeyboardButton(day, callback_data=f"{prefix}_ignore") for day in week_days])
    
    days_row = []
    for i in range(first_day_weekday):
        days_row.append(InlineKeyboardButton(" ", callback_data=f"{prefix}_ignore"))
    
    today_date = date.today()
    
    for day in range(1, days_in_month + 1):
        current_date = date(year, month, day)
        date_str = current_date.isoformat()
        is_available = date_str in available_dates and current_date >= today_date
        
        if is_available:
            days_row.append(InlineKeyboardButton(str(day), callback_data=f"{prefix}_day_{year}_{month}_{day}"))
        else:
            days_row.append(InlineKeyboardButton(f"❌{day}", callback_data=f"{prefix}_unavailable"))
        
        if len(days_row) == 7:
            keyboard.append(days_row)
            days_row = []
    
    if days_row:
        while len(days_row) < 7:
            days_row.append(InlineKeyboardButton(" ", callback_data=f"{prefix}_ignore"))
        keyboard.append(days_row)
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"{prefix}_cancel")])
    return InlineKeyboardMarkup(keyboard)


def build_calendar_for_admin(year, month, prefix="admin_date"):
    month_names = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
    first_day_weekday, days_in_month = monthrange(year, month)
    keyboard = []
    row = [InlineKeyboardButton(f"📅 {month_names[month-1]} {year}", callback_data=f"{prefix}_ignore")]
    row.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}_prev_{year}_{month}"))
    row.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}_next_{year}_{month}"))
    keyboard.append(row)
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard.append([InlineKeyboardButton(day, callback_data=f"{prefix}_ignore") for day in week_days])
    days_row = []
    for i in range(first_day_weekday):
        days_row.append(InlineKeyboardButton(" ", callback_data=f"{prefix}_ignore"))
    for day in range(1, days_in_month+1):
        days_row.append(InlineKeyboardButton(str(day), callback_data=f"{prefix}_day_{year}_{month}_{day}"))
        if len(days_row) == 7:
            keyboard.append(days_row)
            days_row = []
    if days_row:
        while len(days_row) < 7:
            days_row.append(InlineKeyboardButton(" ", callback_data=f"{prefix}_ignore"))
        keyboard.append(days_row)
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"{prefix}_cancel")])
    return InlineKeyboardMarkup(keyboard)


def build_month_selection_keyboard(year, month, prefix="month_select"):
    month_names = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
    keyboard = []
    row = [InlineKeyboardButton(f"📅 {year}", callback_data=f"{prefix}_ignore")]
    row.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}_prev_year_{year}"))
    row.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}_next_year_{year}"))
    keyboard.append(row)
    
    months_row = []
    for i, month_name in enumerate(month_names, 1):
        months_row.append(InlineKeyboardButton(month_name, callback_data=f"{prefix}_month_{year}_{i}"))
        if len(months_row) == 3:
            keyboard.append(months_row)
            months_row = []
    if months_row:
        keyboard.append(months_row)
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"{prefix}_cancel")])
    return InlineKeyboardMarkup(keyboard)


async def show_calendar_for_client(update, context, prefix="client_date"):
    now = datetime.now()
    year, month = now.year, now.month
    reply_markup = build_calendar_for_client(year, month, prefix)
    if update.callback_query:
        await update.callback_query.edit_message_text("📅 Выберите доступную дату:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("📅 Выберите доступную дату:", reply_markup=reply_markup)


async def show_calendar_for_admin(update, context, prefix="admin_date"):
    now = datetime.now()
    year, month = now.year, now.month
    reply_markup = build_calendar_for_admin(year, month, prefix)
    if update.callback_query:
        await update.callback_query.edit_message_text("📅 Выберите дату:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("📅 Выберите дату:", reply_markup=reply_markup)


async def show_month_selector(update, context, prefix="month_select"):
    now = datetime.now()
    year, month = now.year, now.month
    reply_markup = build_month_selection_keyboard(year, month, prefix)
    if update.callback_query:
        await update.callback_query.edit_message_text("📅 Выберите месяц для просмотра записей:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("📅 Выберите месяц для просмотра записей:", reply_markup=reply_markup)


# ---------- КЛИЕНТ ----------
main_keyboard = ReplyKeyboardMarkup(
    [["✂️ Записаться", "📋 Мои записи"], ["❌ Отменить запись", "ℹ️ Помощь"]],
    resize_keyboard=True
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_admin(user.id):
        await update.message.reply_text(
            f"👑 Здравствуйте, администратор {user.first_name}!\n"
            f"Используйте команды:\n/admin - панель управления\n/start - это меню"
        )
    else:
        await update.message.reply_text(
            f"Добро пожаловать, {user.first_name}!\nЯ бот для записи на груминг ✂️\n\nВыберите действие:",
            reply_markup=main_keyboard
        )


async def client_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "✂️ Записаться":
        services = get_services()
        keyboard = [[InlineKeyboardButton(s, callback_data=f"service_{s}")] for s in services]
        await update.message.reply_text("Выберите услугу:", reply_markup=InlineKeyboardMarkup(keyboard))
        return CLIENT_SERVICE
    elif text == "📋 Мои записи":
        await show_my_appointments(update, context)
        return ConversationHandler.END
    elif text == "❌ Отменить запись":
        await list_appointments_for_cancel(update, context)
        return ConversationHandler.END
    elif text == "ℹ️ Помощь":
        await update.message.reply_text("Я помогаю записаться на груминг.\nВы можете записаться, посмотреть свои активные записи или отменить их.")
    return ConversationHandler.END


async def service_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    service = query.data.split("_", 1)[1]
    context.user_data["service"] = service
    
    # Если выбрана услуга "Уход по запросу", запрашиваем комментарий
    if service == "📝 Уход по запросу":
        await query.edit_message_text(
            "📝 Вы выбрали услугу «Уход по запросу».\n\n"
            "Пожалуйста, напишите, какая именно услуга нужна вашему питомцу:\n"
            "(например: стрижка когтей, чистка ушей, вычёсывание и т.д.)"
        )
        return CLIENT_COMMENT
    else:
        await show_calendar_for_client(update, context, prefix="client_date")
        return CLIENT_DATE


async def client_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ввода комментария для услуги 'Уход по запросу'"""
    comment = update.message.text
    context.user_data["comment"] = comment
    await update.message.reply_text(
        f"✅ Комментарий сохранён:\n\n📝 {comment}\n\n"
        f"Теперь выберите удобную дату для записи:"
    )
    await show_calendar_for_client(update, context, prefix="client_date")
    return CLIENT_DATE


async def client_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()
    prefix = "client_date"
    
    if data == f"{prefix}_cancel":
        await query.edit_message_text("❌ Запись отменена.")
        return ConversationHandler.END
    
    if data == f"{prefix}_unavailable":
        await query.answer("❌ На эту дату нет свободных окон для записи!", show_alert=True)
        return CLIENT_DATE
    
    parts = data.split('_')
    
    if len(parts) >= 5 and parts[2] == "day":
        try:
            year = int(parts[3])
            month = int(parts[4])
            day = int(parts[5])
            selected_date = date(year, month, day)
            
            if selected_date < date.today():
                await query.answer("❌ Нельзя выбрать прошедшую дату!", show_alert=True)
                return CLIENT_DATE
            
            free_slots = get_free_slots(selected_date.isoformat())
            if not free_slots:
                await query.answer("❌ На эту дату нет свободных интервалов!", show_alert=True)
                await show_calendar_for_client(update, context, prefix)
                return CLIENT_DATE
            
            context.user_data["appointment_date"] = selected_date.isoformat()
            keyboard = []
            for start, end, slot_id in free_slots:
                keyboard.append([InlineKeyboardButton(f"{start} - {end}", callback_data=f"slot_{slot_id}_{start}_{end}")])
            keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_date")])
            await query.edit_message_text("✅ Выберите время:", reply_markup=InlineKeyboardMarkup(keyboard))
            return CLIENT_TIME
            
        except (IndexError, ValueError) as e:
            print(f"Ошибка: {e}")
            await query.edit_message_text("❌ Ошибка при выборе даты. Попробуйте снова.")
            await show_calendar_for_client(update, context, prefix)
            return CLIENT_DATE
    
    elif len(parts) >= 5 and (data.startswith(f"{prefix}_prev") or data.startswith(f"{prefix}_next")):
        try:
            year = int(parts[3])
            month = int(parts[4])
            
            if data.startswith(f"{prefix}_prev"):
                month -= 1
                if month < 1:
                    month = 12
                    year -= 1
            else:
                month += 1
                if month > 12:
                    month = 1
                    year += 1
            
            reply_markup = build_calendar_for_client(year, month, prefix)
            await query.edit_message_text("📅 Выберите доступную дату:", reply_markup=reply_markup)
            return CLIENT_DATE
        except (IndexError, ValueError):
            await query.edit_message_text("❌ Ошибка при перелистывании календаря.")
            return CLIENT_DATE
    
    return CLIENT_DATE


async def client_time_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "back_to_date":
        await show_calendar_for_client(update, context, prefix="client_date")
        return CLIENT_DATE
    
    if data.startswith("slot_"):
        try:
            _, slot_id, start_time, end_time = data.split("_", 3)
            context.user_data["slot_id"] = int(slot_id)
            context.user_data["start_time"] = start_time
            context.user_data["end_time"] = end_time
            
            contact_keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton("📱 Отправить номер телефона", request_contact=True)]],
                resize_keyboard=True, one_time_keyboard=True
            )
            await query.edit_message_text("📞 Пожалуйста, отправьте ваш номер телефона:")
            await update.effective_message.reply_text("Нажмите кнопку:", reply_markup=contact_keyboard)
            return CLIENT_PHONE
        except Exception as e:
            print(f"Ошибка: {e}")
            await query.edit_message_text("❌ Ошибка при выборе времени. Попробуйте снова.")
            return CLIENT_DATE
    
    return CLIENT_TIME


async def client_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.contact:
        phone = update.message.contact.phone_number
    else:
        phone = update.message.text
    
    context.user_data["phone"] = phone
    await update.message.reply_text("🐕 Введите кличку питомца:")
    return CLIENT_PET_NAME


async def client_pet_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pet_name"] = update.message.text
    await update.message.reply_text("🐶 Введите породу питомца:")
    return CLIENT_BREED


async def client_breed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["breed"] = update.message.text
    user = update.effective_user
    
    # Получаем комментарий, если он есть (для услуги "Уход по запросу")
    comment = context.user_data.get("comment", "")
    
    app_id = create_appointment(
        user.id,
        user.username or user.first_name,
        context.user_data["service"],
        context.user_data["appointment_date"],
        context.user_data["start_time"],
        context.user_data["end_time"],
        context.user_data["phone"],
        context.user_data["pet_name"],
        context.user_data["breed"],
        context.user_data["slot_id"],
        comment
    )
    
    await notify_all_admins_new_appointment(context.bot, app_id)
    await update.message.reply_text(
        "✅ Заявка отправлена администратору!\nВы получите уведомление о подтверждении.",
        reply_markup=main_keyboard
    )
    return ConversationHandler.END


async def notify_all_admins_new_appointment(bot, app_id):
    data = get_appointment_by_id(app_id)
    if not data:
        return
    user_id, service, date_str, start, end, pet_name, breed, phone, price, status, comment = data
    formatted_date = format_date(date_str)
    
    text = (f"🆕 НОВАЯ ЗАЯВКА #{app_id}\n\n"
            f"🐕 Питомец: {pet_name} ({breed})\n"
            f"✂️ Услуга: {service}\n"
            f"📅 Дата: {formatted_date}\n"
            f"⏰ Время: {start} - {end}\n"
            f"📞 Телефон: {phone}")
    
    if comment:
        text += f"\n📝 Комментарий: {comment}"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ПОДТВЕРДИТЬ", callback_data=f"confirm_{app_id}"),
         InlineKeyboardButton("❌ ОТКЛОНИТЬ", callback_data=f"reject_{app_id}")]
    ])
    
    for admin_id in ALL_ADMINS:
        try:
            await bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
        except:
            pass


async def show_my_appointments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает активные записи клиента - БЕЗ телефона"""
    user_id = update.effective_user.id
    apps = get_user_active_appointments(user_id)
    
    if not apps:
        await update.message.reply_text("📭 У вас нет активных записей.")
        return
    
    text = "📋 **ВАШИ АКТИВНЫЕ ЗАПИСИ:**\n\n"
    for app in apps:
        app_id, service, date_str, start, end, status, price, pet_name, breed, comment = app
        formatted_date = format_date(date_str)
        
        status_emoji = "⏳" if status == "pending" else "✅"
        status_text = "ожидает подтверждения" if status == "pending" else "подтверждена"
        
        text += f"{status_emoji} **Запись #{app_id}**\n"
        text += f"   🐕 Питомец: {pet_name} ({breed})\n"
        text += f"   ✂️ Услуга: {service}\n"
        if comment:
            text += f"   📝 Комментарий: {comment}\n"
        text += f"   📅 Дата: {formatted_date}\n"
        text += f"   ⏰ Время: {start} - {end}\n"
        text += f"   📌 Статус: {status_text}\n"
        if status == "confirmed" and price > 0:
            text += f"   💰 Стоимость: {price} руб.\n"
        text += "\n"
    
    await update.message.reply_text(text, parse_mode="Markdown")


async def list_appointments_for_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    apps = get_user_active_appointments(user_id)
    if not apps:
        await update.message.reply_text("❌ Нет активных записей для отмены.")
        return
    keyboard = []
    for app in apps:
        app_id, service, date_str, start, end, status, price, pet_name, breed, comment = app
        formatted_date = format_date(date_str)
        keyboard.append([InlineKeyboardButton(f"{pet_name} - {service} ({formatted_date} {start})", callback_data=f"cancel_{app_id}")])
    await update.message.reply_text("🔍 Выберите запись для отмены:", reply_markup=InlineKeyboardMarkup(keyboard))


async def cancel_appointment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Клиент отменяет свою запись"""
    query = update.callback_query
    await query.answer()
    app_id = int(query.data.split("_")[1])
    
    # Отменяем запись
    user_id, pet_name, slot_id, date_str, service, start_time, end_time, phone, comment = cancel_appointment_by_admin(app_id)
    
    await query.edit_message_text("✅ Запись отменена. Слот освобождён.")
    
    # Уведомляем всех админов
    for admin_id in ALL_ADMINS:
        try:
            await context.bot.send_message(admin_id, f"⚠️ Клиент отменил запись #{app_id} (питомец: {pet_name})")
        except:
            pass


# ---------- АДМИН ----------
admin_keyboard = ReplyKeyboardMarkup(
    [["➕ Добавить дату", "📅 Записи на неделю"], 
     ["📆 Записи на месяц", "💰 Аналитика"],
     ["❌ Отменить запись"]],
    resize_keyboard=True
)


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ У вас нет прав администратора.")
        return
    
    await update.message.reply_text(
        "👑 **ПАНЕЛЬ АДМИНИСТРАТОРА**\n\nВыберите действие:",
        reply_markup=admin_keyboard,
        parse_mode="Markdown"
    )


async def admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text == "➕ Добавить дату":
        await show_calendar_for_admin(update, context, prefix="admin_date")
        return ADMIN_ADD_DATE
    
    elif text == "📅 Записи на неделю":
        rows = get_all_appointments_next_week()
        if not rows:
            await update.message.reply_text("📭 На ближайшую неделю нет записей.")
        else:
            week_text = "📋 **ЗАПИСИ НА БЛИЖАЙШУЮ НЕДЕЛЮ:**\n\n"
            for row in rows:
                app_id, user_id, username, service, date_str, start, end, phone, pet_name, breed, price, status, comment = row
                formatted_date = format_date(date_str)
                
                status_emoji = "⏳" if status == "pending" else "✅" if status == "confirmed" else "❌"
                status_text = "ожидает" if status == "pending" else "подтверждена" if status == "confirmed" else "отменена"
                week_text += f"{status_emoji} **#{app_id}**\n"
                week_text += f"   🐕 {pet_name} ({breed})\n"
                week_text += f"   📅 {formatted_date} {start}-{end}\n"
                week_text += f"   ✂️ {service}\n"
                if comment:
                    week_text += f"   📝 {comment}\n"
                week_text += f"   📞 {phone}\n"
                week_text += f"   📌 {status_text}\n"
                if status == "confirmed" and price > 0:
                    week_text += f"   💰 {price} руб.\n"
                week_text += f"   👤 @{username if username else 'нет'}\n\n"
            await update.message.reply_text(week_text, parse_mode="Markdown")
        return ConversationHandler.END
    
    elif text == "📆 Записи на месяц":
        await show_month_selector(update, context, prefix="month_select")
        return ADMIN_MONTH_SELECT
    
    elif text == "💰 Аналитика":
        await show_calendar_for_admin(update, context, prefix="analytics_start")
        return ADMIN_ANALYTICS_START
    
    elif text == "❌ Отменить запись":
        await show_all_appointments_for_admin_cancel(update, context)
        return ADMIN_CANCEL_APPOINTMENT
    
    return ConversationHandler.END


async def show_all_appointments_for_admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает все активные записи для отмены админом"""
    rows = get_all_appointments()
    
    if not rows:
        await update.message.reply_text("📭 Нет активных записей для отмены.")
        return ConversationHandler.END
    
    # Формируем сообщение со списком записей
    text = "📋 **ВСЕ АКТИВНЫЕ ЗАПИСИ:**\n\n"
    keyboard = []
    
    for row in rows:
        app_id, user_id, username, service, date_str, start, end, phone, pet_name, breed, price, status, comment = row
        formatted_date = format_date(date_str)
        status_emoji = "⏳" if status == "pending" else "✅"
        status_text = "ожидает" if status == "pending" else "подтверждена"
        
        text += f"{status_emoji} **#{app_id}**\n"
        text += f"   🐕 {pet_name} ({breed})\n"
        text += f"   📅 {formatted_date} {start}-{end}\n"
        text += f"   ✂️ {service}\n"
        if comment:
            text += f"   📝 {comment}\n"
        text += f"   📞 {phone}\n"
        text += f"   📌 {status_text}\n"
        text += f"   👤 @{username if username else 'нет'}\n\n"
        
        keyboard.append([InlineKeyboardButton(f"❌ Отменить #{app_id} - {pet_name} ({formatted_date} {start})", callback_data=f"admin_cancel_{app_id}")])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад в меню", callback_data="admin_cancel_back")])
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ADMIN_CANCEL_APPOINTMENT


async def admin_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик отмены записи админом"""
    query = update.callback_query
    data = query.data
    await query.answer()
    
    if data == "admin_cancel_back":
        # Возвращаемся в админ-панель
        await admin_panel(update, context)
        return ConversationHandler.END
    
    if data.startswith("admin_cancel_"):
        app_id = int(data.split("_")[2])
        
        # Получаем данные записи перед отменой
        user_id, pet_name, slot_id, date_str, service, start_time, end_time, phone, comment = cancel_appointment_by_admin(app_id)
        
        if user_id:
            formatted_date = format_date(date_str)
            
            # Уведомляем клиента
            try:
                msg = (f"❌ **Ваша запись отменена администратором!**\n\n"
                       f"🐕 Питомец: {pet_name}\n"
                       f"📅 Дата: {formatted_date}\n"
                       f"⏰ Время: {start_time} - {end_time}\n"
                       f"✂️ Услуга: {service}")
                if comment:
                    msg += f"\n📝 Комментарий: {comment}"
                msg += f"\n\nСлот освобождён. Вы можете записаться на другое время."
                
                await context.bot.send_message(user_id, msg, parse_mode="Markdown")
            except Exception as e:
                print(f"Не удалось уведомить клиента: {e}")
            
            # Уведомляем всех админов
            for admin_id in ALL_ADMINS:
                try:
                    await context.bot.send_message(admin_id, f"✅ Администратор отменил запись #{app_id} (питомец: {pet_name})")
                except:
                    pass
            
            await query.edit_message_text(f"✅ Запись #{app_id} для питомца {pet_name} успешно отменена. Слот освобождён.\n\nИспользуйте /admin для продолжения.")
        else:
            await query.edit_message_text("❌ Ошибка при отмене записи.")
        
        return ConversationHandler.END
    
    return ADMIN_CANCEL_APPOINTMENT


async def admin_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()
    prefix = "admin_date"
    
    if data == f"{prefix}_cancel":
        await query.edit_message_text("❌ Добавление даты отменено.")
        return ConversationHandler.END
    
    parts = data.split('_')
    
    if len(parts) >= 5 and parts[2] == "day":
        try:
            year = int(parts[3])
            month = int(parts[4])
            day = int(parts[5])
            selected_date = date(year, month, day)
            
            if selected_date < date.today():
                await query.edit_message_text("❌ Нельзя добавить прошедшую дату.")
                await show_calendar_for_admin(update, context, prefix)
                return ADMIN_ADD_DATE
            
            # Получаем существующие интервалы на эту дату
            existing_slots = get_all_slots(selected_date.isoformat())
            context.user_data["admin_selected_date"] = selected_date.isoformat()
            
            message = f"✅ Дата {format_date(selected_date.isoformat())} выбрана!\n\n"
            
            if existing_slots:
                message += "📋 **Существующие интервалы:**\n"
                # Проверяем, какие слоты свободны, а какие заняты
                for start, end, slot_id in existing_slots:
                    # Проверяем, есть ли активная запись на этот слот
                    conn = sqlite3.connect("grooming.db")
                    cur = conn.cursor()
                    cur.execute("SELECT id FROM appointments WHERE slot_id=? AND status IN ('pending','confirmed')", (slot_id,))
                    appointment = cur.fetchone()
                    conn.close()
                    
                    if appointment:
                        message += f"   🔴 {start} - {end} (ЗАНЯТ)\n"
                    else:
                        message += f"   🟢 {start} - {end} (СВОБОДЕН)\n"
                message += "\n"
            else:
                message += "📋 На эту дату пока нет интервалов.\n\n"
            
            message += "⌨️ **Введите новые интервалы** (10:00-11:00, 12:00-13:00):\n"
            message += "Интервалы, которые уже есть, не будут продублированы."
            
            await query.edit_message_text(message, parse_mode="Markdown")
            return ADMIN_ADD_SLOTS
            
        except (IndexError, ValueError):
            await query.edit_message_text("❌ Ошибка при выборе даты.")
            await show_calendar_for_admin(update, context, prefix)
            return ADMIN_ADD_DATE
    
    elif len(parts) >= 5 and (data.startswith(f"{prefix}_prev") or data.startswith(f"{prefix}_next")):
        try:
            year = int(parts[3])
            month = int(parts[4])
            if data.startswith(f"{prefix}_prev"):
                month -= 1
                if month < 1:
                    month = 12
                    year -= 1
            else:
                month += 1
                if month > 12:
                    month = 1
                    year += 1
            reply_markup = build_calendar_for_admin(year, month, prefix)
            await query.edit_message_text("📅 Выберите дату:", reply_markup=reply_markup)
            return ADMIN_ADD_DATE
        except:
            await query.edit_message_text("❌ Ошибка при перелистывании.")
            return ConversationHandler.END
    
    return ADMIN_ADD_DATE


async def admin_add_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    intervals = [i.strip() for i in text.split(',')]
    valid = True
    for interval in intervals:
        if '-' not in interval:
            valid = False
            break
        try:
            start, end = interval.split('-')
            datetime.strptime(start.strip(), "%H:%M")
            datetime.strptime(end.strip(), "%H:%M")
        except:
            valid = False
            break
    
    if not valid:
        await update.message.reply_text("❌ Неверный формат! Пример: 10:00-11:00, 12:00-13:00")
        return ADMIN_ADD_SLOTS
    
    date_str = context.user_data["admin_selected_date"]
    added, existed = add_slots(date_str, intervals)
    
    result_message = f"📅 **Дата:** {format_date(date_str)}\n\n"
    
    if added:
        result_message += "✅ **Добавлены интервалы:**\n"
        for interval in added:
            result_message += f"   • {interval}\n"
        result_message += "\n"
    
    if existed:
        result_message += "⚠️ **Уже существовали (не добавлены):**\n"
        for interval in existed:
            result_message += f"   • {interval}\n"
        result_message += "\n"
    
    if not added and not existed:
        result_message += "❌ Не добавлено ни одного интервала.\n"
    
    result_message += "Используйте /admin для продолжения."
    
    await update.message.reply_text(result_message, parse_mode="Markdown")
    return ConversationHandler.END


async def show_analytics_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_calendar_for_admin(update, context, prefix="analytics_start")


async def analytics_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()
    prefix = "analytics_start"
    
    if data == f"{prefix}_cancel":
        await query.edit_message_text("❌ Аналитика отменена.")
        return ConversationHandler.END
    
    parts = data.split('_')
    
    if len(parts) >= 5 and parts[2] == "day":
        try:
            year = int(parts[3])
            month = int(parts[4])
            day = int(parts[5])
            start_date = date(year, month, day)
            context.user_data["analytics_start"] = start_date
            await show_calendar_for_admin(update, context, prefix="analytics_end")
            return ADMIN_ANALYTICS_END
        except:
            await query.edit_message_text("❌ Ошибка.")
            return ConversationHandler.END
    
    elif len(parts) >= 5 and (data.startswith(f"{prefix}_prev") or data.startswith(f"{prefix}_next")):
        try:
            year = int(parts[3])
            month = int(parts[4])
            if data.startswith(f"{prefix}_prev"):
                month -= 1
                if month < 1:
                    month = 12
                    year -= 1
            else:
                month += 1
                if month > 12:
                    month = 1
                    year += 1
            reply_markup = build_calendar_for_admin(year, month, prefix)
            await query.edit_message_text("📅 Выберите начальную дату:", reply_markup=reply_markup)
            return ADMIN_ANALYTICS_START
        except:
            await query.edit_message_text("❌ Ошибка.")
            return ConversationHandler.END
    
    return ADMIN_ANALYTICS_START


async def analytics_end_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()
    prefix = "analytics_end"
    
    if data == f"{prefix}_cancel":
        await query.edit_message_text("❌ Аналитика отменена.")
        return ConversationHandler.END
    
    parts = data.split('_')
    
    if len(parts) >= 5 and parts[2] == "day":
        try:
            year = int(parts[3])
            month = int(parts[4])
            day = int(parts[5])
            end_date = date(year, month, day)
            start_date = context.user_data.get("analytics_start")
            
            if not start_date:
                await query.edit_message_text("❌ Ошибка: начальная дата не выбрана.")
                return ConversationHandler.END
            
            if end_date < start_date:
                await query.edit_message_text("❌ Конечная дата не может быть раньше начальной.")
                await show_calendar_for_admin(update, context, prefix="analytics_end")
                return ADMIN_ANALYTICS_END
            
            total = get_financial_summary(start_date, end_date)
            await query.edit_message_text(f"💰 **ФИНАНСОВАЯ АНАЛИТИКА**\n\n"
                                         f"📅 Период: {format_date(start_date.isoformat())} – {format_date(end_date.isoformat())}\n"
                                         f"💵 Общая выручка: {total} руб.", parse_mode="Markdown")
            return ConversationHandler.END
        except:
            await query.edit_message_text("❌ Ошибка.")
            return ConversationHandler.END
    
    elif len(parts) >= 5 and (data.startswith(f"{prefix}_prev") or data.startswith(f"{prefix}_next")):
        try:
            year = int(parts[3])
            month = int(parts[4])
            if data.startswith(f"{prefix}_prev"):
                month -= 1
                if month < 1:
                    month = 12
                    year -= 1
            else:
                month += 1
                if month > 12:
                    month = 1
                    year += 1
            reply_markup = build_calendar_for_admin(year, month, prefix)
            await query.edit_message_text("📅 Выберите конечную дату:", reply_markup=reply_markup)
            return ADMIN_ANALYTICS_END
        except:
            await query.edit_message_text("❌ Ошибка.")
            return ConversationHandler.END
    
    return ADMIN_ANALYTICS_END


async def month_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()
    prefix = "month_select"
    
    if data == f"{prefix}_cancel":
        await query.edit_message_text("❌ Просмотр записей отменён.")
        return ConversationHandler.END
    
    parts = data.split('_')
    
    if len(parts) >= 4 and parts[2] == "month":
        try:
            year = int(parts[3])
            month = int(parts[4])
            
            rows = get_appointments_by_month(year, month)
            month_names = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", 
                          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
            
            if not rows:
                await query.edit_message_text(f"📭 За {month_names[month-1]} {year} нет записей.")
            else:
                month_text = f"📋 **ЗАПИСИ ЗА {month_names[month-1]} {year}:**\n\n"
                for row in rows:
                    app_id, user_id, username, service, date_str, start, end, phone, pet_name, breed, price, status, comment = row
                    formatted_date = format_date(date_str)
                    
                    status_emoji = "⏳" if status == "pending" else "✅" if status == "confirmed" else "❌"
                    status_text = "ожидает" if status == "pending" else "подтверждена" if status == "confirmed" else "отменена"
                    month_text += f"{status_emoji} **#{app_id}**\n"
                    month_text += f"   🐕 {pet_name} ({breed})\n"
                    month_text += f"   📅 {formatted_date} {start}-{end}\n"
                    month_text += f"   ✂️ {service}\n"
                    if comment:
                        month_text += f"   📝 {comment}\n"
                    month_text += f"   📞 {phone}\n"
                    month_text += f"   📌 {status_text}\n"
                    if status == "confirmed" and price > 0:
                        month_text += f"   💰 {price} руб.\n"
                    month_text += f"   👤 @{username if username else 'нет'}\n\n"
                await query.edit_message_text(month_text, parse_mode="Markdown")
            return ConversationHandler.END
            
        except (IndexError, ValueError):
            await query.edit_message_text("❌ Ошибка при выборе месяца.")
            return ConversationHandler.END
    
    elif len(parts) >= 4 and (data.startswith(f"{prefix}_prev_year") or data.startswith(f"{prefix}_next_year")):
        try:
            year = int(parts[3])
            if data.startswith(f"{prefix}_prev_year"):
                year -= 1
            else:
                year += 1
            reply_markup = build_month_selection_keyboard(year, 1, prefix)
            await query.edit_message_text("📅 Выберите месяц для просмотра записей:", reply_markup=reply_markup)
            return ADMIN_MONTH_SELECT
        except:
            await query.edit_message_text("❌ Ошибка при перелистывании.")
            return ConversationHandler.END
    
    return ADMIN_MONTH_SELECT


async def handle_confirm_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("confirm_"):
        app_id = int(data.split("_")[1])
        context.user_data["confirm_app_id"] = app_id
        await query.edit_message_text(f"💰 Введите цену для записи #{app_id} (в рублях):")
        return
    elif data.startswith("reject_"):
        app_id = int(data.split("_")[1])
        app_info = get_appointment_by_id(app_id)
        
        # Отклоняем запись (удаляем)
        user_id, pet_name, slot_id, date_str, service, start_time, end_time, phone, comment = cancel_appointment_by_admin(app_id)
        
        await query.edit_message_text(f"❌ Запись #{app_id} отклонена.")
        if app_info:
            user_id = app_info[0]
            pet_name = app_info[4]
            await context.bot.send_message(user_id, f"❌ Запись для питомца {pet_name} была отклонена администратором.")
    
    return ConversationHandler.END


async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = int(update.message.text)
    except:
        await update.message.reply_text("❌ Введите число (цену в рублях).")
        return
    
    app_id = context.user_data.get("confirm_app_id")
    if not app_id:
        await update.message.reply_text("❌ Ошибка.")
        return
    
    confirm_appointment(app_id, price)
    app_info = get_appointment_by_id(app_id)
    if app_info:
        user_id, service, date_str, start, end, pet_name, breed, phone, price, status, comment = app_info
        formatted_date = format_date(date_str)
        
        text = (f"✅ **ЗАПИСЬ ПОДТВЕРЖДЕНА!**\n\n"
                f"🐕 Питомец: {pet_name} ({breed})\n"
                f"✂️ Услуга: {service}\n")
        if comment:
            text += f"📝 Комментарий: {comment}\n"
        text += (f"📅 Дата: {formatted_date}\n"
                f"⏰ Время: {start} - {end}\n"
                f"💰 Стоимость: {price} руб.\n\n"
                f"Спасибо за доверие! Ждём вас! 🐾")
        await context.bot.send_message(user_id, text, parse_mode="Markdown")
    await update.message.reply_text(f"✅ Запись #{app_id} подтверждена на сумму {price} руб.!")
    context.user_data.pop("confirm_app_id", None)


# ---------- НАПОМИНАНИЯ КЛИЕНТАМ ----------
def send_reminders_sync():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(send_reminders_async())
    loop.close()


async def send_reminders_async():
    from telegram import Bot
    bot = Bot(token=TOKEN)
    tomorrow = date.today() + timedelta(days=1)
    conn = sqlite3.connect("grooming.db")
    cur = conn.cursor()
    cur.execute("SELECT user_id, service, date, start_time, end_time, pet_name, breed, comment FROM appointments WHERE status='confirmed' AND date=?", (tomorrow.isoformat(),))
    rows = cur.fetchall()
    conn.close()
    for user_id, service, date_str, start, end, pet_name, breed, comment in rows:
        formatted_date = format_date(date_str)
        
        text = (f"🐾 **НАПОМИНАНИЕ!**\n\n"
                f"Завтра {formatted_date} в {start} у вас запланирован груминг.\n\n"
                f"🐕 Питомец: {pet_name} ({breed})\n"
                f"✂️ Услуга: {service}\n")
        if comment:
            text += f"📝 Комментарий: {comment}\n"
        text += f"\nЖдём вас! 🐾"
        await bot.send_message(user_id, text, parse_mode="Markdown")


# ---------- УВЕДОМЛЕНИЯ АДМИНИСТРАТОРУ ----------
def send_admin_morning_report_sync():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(send_admin_morning_report())
    loop.close()


async def send_admin_morning_report():
    from telegram import Bot
    bot = Bot(token=TOKEN)
    
    today = date.today()
    rows = get_appointments_by_date(today)
    formatted_date = format_date(today.isoformat())
    
    if not rows:
        text = f"📋 **ОТЧЁТ НА {formatted_date}**\n\n✅ Записей на сегодня нет."
    else:
        text = f"📋 **ОТЧЁТ НА {formatted_date}**\n\n"
        for row in rows:
            app_id, user_id, username, service, date_str, start, end, phone, pet_name, breed, price, status, comment = row
            
            status_emoji = "⏳" if status == "pending" else "✅" if status == "confirmed" else "❌"
            status_text = "ожидает" if status == "pending" else "подтверждена" if status == "confirmed" else "отменена"
            
            text += f"{status_emoji} **#{app_id}**\n"
            text += f"   🐕 {pet_name} ({breed})\n"
            text += f"   ⏰ {start} - {end}\n"
            text += f"   ✂️ {service}\n"
            if comment:
                text += f"   📝 {comment}\n"
            text += f"   📞 {phone}\n"
            text += f"   📌 {status_text}\n"
            if status == "confirmed" and price > 0:
                text += f"   💰 {price} руб.\n"
            text += f"   👤 @{username if username else 'нет'}\n\n"
    
    for admin_id in ALL_ADMINS:
        try:
            await bot.send_message(chat_id=admin_id, text=text, parse_mode="Markdown")
        except Exception as e:
            print(f"Не удалось отправить утренний отчёт админу {admin_id}: {e}")


def send_admin_evening_report_sync():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(send_admin_evening_report())
    loop.close()


async def send_admin_evening_report():
    from telegram import Bot
    bot = Bot(token=TOKEN)
    
    tomorrow = date.today() + timedelta(days=1)
    rows = get_appointments_by_date(tomorrow)
    formatted_date = format_date(tomorrow.isoformat())
    
    if not rows:
        text = f"📋 **ОТЧЁТ НА {formatted_date}**\n\n✅ Записей на завтра нет."
    else:
        text = f"📋 **ОТЧЁТ НА {formatted_date}**\n\n"
        for row in rows:
            app_id, user_id, username, service, date_str, start, end, phone, pet_name, breed, price, status, comment = row
            
            status_emoji = "⏳" if status == "pending" else "✅" if status == "confirmed" else "❌"
            status_text = "ожидает" if status == "pending" else "подтверждена" if status == "confirmed" else "отменена"
            
            text += f"{status_emoji} **#{app_id}**\n"
            text += f"   🐕 {pet_name} ({breed})\n"
            text += f"   ⏰ {start} - {end}\n"
            text += f"   ✂️ {service}\n"
            if comment:
                text += f"   📝 {comment}\n"
            text += f"   📞 {phone}\n"
            text += f"   📌 {status_text}\n"
            if status == "confirmed" and price > 0:
                text += f"   💰 {price} руб.\n"
            text += f"   👤 @{username if username else 'нет'}\n\n"
    
    for admin_id in ALL_ADMINS:
        try:
            await bot.send_message(chat_id=admin_id, text=text, parse_mode="Markdown")
        except Exception as e:
            print(f"Не удалось отправить вечерний отчёт админу {admin_id}: {e}")


# ---------- ЗАПУСК ----------
def main():
    application = Application.builder().token(TOKEN).build()
    
    # Клиент
    client_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^✂️ Записаться$"), client_menu)],
        states={
            CLIENT_SERVICE: [CallbackQueryHandler(service_selection, pattern="^service_")],
            CLIENT_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_comment)],
            CLIENT_DATE: [CallbackQueryHandler(client_date_callback, pattern="^client_date")],
            CLIENT_TIME: [CallbackQueryHandler(client_time_selection, pattern="^(slot_|back_to_date)")],
            CLIENT_PHONE: [MessageHandler(filters.TEXT | filters.CONTACT, client_phone)],
            CLIENT_PET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_pet_name)],
            CLIENT_BREED: [MessageHandler(filters.TEXT & ~filters.COMMAND, client_breed)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True
    )
    application.add_handler(client_conv)
    
    # Админ - добавление дат
    admin_add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить дату$"), admin_menu_handler)],
        states={
            ADMIN_ADD_DATE: [CallbackQueryHandler(admin_date_callback, pattern="^admin_date")],
            ADMIN_ADD_SLOTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_slots)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True
    )
    application.add_handler(admin_add_conv)
    
    # Админ - аналитика
    admin_analytics_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Аналитика$"), admin_menu_handler)],
        states={
            ADMIN_ANALYTICS_START: [CallbackQueryHandler(analytics_start_callback, pattern="^analytics_start")],
            ADMIN_ANALYTICS_END: [CallbackQueryHandler(analytics_end_callback, pattern="^analytics_end")],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True
    )
    application.add_handler(admin_analytics_conv)
    
    # Админ - просмотр записей за месяц
    admin_month_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📆 Записи на месяц$"), admin_menu_handler)],
        states={
            ADMIN_MONTH_SELECT: [CallbackQueryHandler(month_select_callback, pattern="^month_select")],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True
    )
    application.add_handler(admin_month_conv)
    
    # Админ - отмена записи
    admin_cancel_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^❌ Отменить запись$"), admin_menu_handler)],
        states={
            ADMIN_CANCEL_APPOINTMENT: [CallbackQueryHandler(admin_cancel_callback, pattern="^(admin_cancel_|admin_cancel_back)")],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True
    )
    application.add_handler(admin_cancel_conv)
    
    # Обработчики админ-меню (без состояния)
    application.add_handler(MessageHandler(filters.Regex("^📅 Записи на неделю$"), admin_menu_handler))
    
    # Остальные
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CallbackQueryHandler(handle_confirm_reject, pattern="^(confirm_|reject_)"))
    application.add_handler(MessageHandler(filters.Regex("^(📋 Мои записи|❌ Отменить запись|ℹ️ Помощь)$"), client_menu))
    application.add_handler(CallbackQueryHandler(cancel_appointment_callback, pattern="^cancel_"))
    application.add_handler(MessageHandler(filters.Regex(r"^\d+$") & ~filters.COMMAND, set_price))
    
    # Планировщик
    scheduler = BackgroundScheduler()
    
    # Напоминания клиентам в 10:00
    scheduler.add_job(send_reminders_sync, CronTrigger(hour=REMINDER_HOUR, minute=REMINDER_MINUTE))
    
    # Утренний отчёт админу в 9:45
    scheduler.add_job(send_admin_morning_report_sync, CronTrigger(hour=9, minute=45))
    
    # Вечерний отчёт админу в 22:00
    scheduler.add_job(send_admin_evening_report_sync, CronTrigger(hour=22, minute=0))
    
    scheduler.start()
    
    print("=" * 50)
    print("✅ БОТ ЗАПУЩЕН!")
    print("=" * 50)
    print("📅 Уведомления администратору:")
    print("   • Каждый день в 9:45 - отчёт о записях на сегодня")
    print("   • Каждый день в 22:00 - отчёт о записях на завтра")
    print("=" * 50)
    application.run_polling()


if __name__ == "__main__":
    main()

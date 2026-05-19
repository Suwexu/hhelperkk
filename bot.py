import asyncio
import logging
import os
from datetime import datetime
import pytz
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ==================== НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
GROUP_ID = os.getenv("GROUP_ID")  # ID группы/канала для рассылки

# Проверка наличия обязательных переменных
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден! Добавь переменную в Railway")
if not ADMIN_ID:
    raise ValueError("❌ ADMIN_ID не найден! Добавь переменную в Railway")
if not GROUP_ID:
    raise ValueError("❌ GROUP_ID не найден! Добавь переменную в Railway (ID группы/канала)")

# Преобразуем GROUP_ID в int (для групп ID отрицательный)
try:
    GROUP_ID = int(GROUP_ID)
except:
    raise ValueError(f"❌ Неверный формат GROUP_ID: {GROUP_ID}. Должно быть число (например, -1001234567890)")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Временное хранилище для состояний админа
admin_states = {}

# ==================== ПРОСТАЯ БАЗА ДАННЫХ ====================
import sqlite3
import json

class SimpleDB:
    def __init__(self):
        self.conn = sqlite3.connect('bot_database.db', check_same_thread=False)
        self.init_tables()
    
    def init_tables(self):
        cursor = self.conn.cursor()
        # Таблица пользователей (больше не нужна для рассылки, но оставим для статистики)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        ''')
        # Таблица рассылок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                content_type TEXT,
                text TEXT,
                photo_file_id TEXT,
                schedule_type TEXT,
                hour INTEGER,
                minute INTEGER,
                interval_minutes INTEGER,
                days TEXT,
                is_active INTEGER DEFAULT 1,
                last_sent_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()
        logger.info("✅ База данных инициализирована")
    
    def add_user(self, user_id, username=None, first_name=None):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, first_name, is_active)
            VALUES (?, ?, ?, 1)
        ''', (user_id, username, first_name))
        self.conn.commit()
    
    def remove_user(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE users SET is_active = 0 WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    def get_active_users(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT user_id FROM users WHERE is_active = 1')
        return [row[0] for row in cursor.fetchall()]
    
    def get_user_count(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users WHERE is_active = 1')
        return cursor.fetchone()[0]
    
    def get_all_users(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT user_id, username, first_name FROM users WHERE is_active = 1')
        return cursor.fetchall()
    
    def add_broadcast(self, name, content_type, schedule_type, text=None, photo_file_id=None,
                      hour=None, minute=None, interval_minutes=None, days=None):
        cursor = self.conn.cursor()
        days_json = json.dumps(days) if days else None
        cursor.execute('''
            INSERT INTO broadcasts (name, content_type, text, photo_file_id, schedule_type,
                                    hour, minute, interval_minutes, days, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ''', (name, content_type, text, photo_file_id, schedule_type, hour, minute, interval_minutes, days_json))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_all_broadcasts(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT id, name, content_type, schedule_type, hour, minute, interval_minutes, days, is_active, last_sent_at, created_at FROM broadcasts ORDER BY created_at DESC')
        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                'id': row[0],
                'name': row[1],
                'content_type': row[2],
                'schedule_type': row[3],
                'hour': row[4],
                'minute': row[5],
                'interval_minutes': row[6],
                'days': json.loads(row[7]) if row[7] else None,
                'is_active': bool(row[8]),
                'last_sent_at': row[9],
                'created_at': row[10]
            })
        return result
    
    def get_broadcast(self, broadcast_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT id, name, content_type, text, photo_file_id, schedule_type, hour, minute, interval_minutes, days, is_active, last_sent_at FROM broadcasts WHERE id = ?', (broadcast_id,))
        row = cursor.fetchone()
        if row:
            return {
                'id': row[0],
                'name': row[1],
                'content_type': row[2],
                'text': row[3],
                'photo_file_id': row[4],
                'schedule_type': row[5],
                'hour': row[6],
                'minute': row[7],
                'interval_minutes': row[8],
                'days': json.loads(row[9]) if row[9] else None,
                'is_active': bool(row[10]),
                'last_sent_at': row[11]
            }
        return None
    
    def update_broadcast(self, broadcast_id, **kwargs):
        cursor = self.conn.cursor()
        allowed = ['name', 'content_type', 'text', 'photo_file_id', 'hour', 'minute', 'interval_minutes', 'days', 'is_active']
        updates = []
        values = []
        for key, value in kwargs.items():
            if key in allowed:
                if key == 'days' and value is not None:
                    value = json.dumps(value)
                updates.append(f"{key} = ?")
                values.append(value)
        if updates:
            values.append(broadcast_id)
            cursor.execute(f"UPDATE broadcasts SET {', '.join(updates)} WHERE id = ?", values)
            self.conn.commit()
    
    def delete_broadcast(self, broadcast_id):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM broadcasts WHERE id = ?', (broadcast_id,))
        self.conn.commit()
    
    def update_last_sent(self, broadcast_id):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE broadcasts SET last_sent_at = CURRENT_TIMESTAMP WHERE id = ?', (broadcast_id,))
        self.conn.commit()

db = SimpleDB()

# ==================== ФУНКЦИЯ ПРОВЕРКИ АДМИНА ====================
def is_admin(user_id):
    return user_id == ADMIN_ID

# ==================== ОТПРАВКА РАССЫЛКИ В ГРУППУ ====================
async def send_broadcast(broadcast_id: int):
    """Отправляет рассылку в указанную группу/канал"""
    logger.info(f"🚀 Запуск рассылки #{broadcast_id} в {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    broadcast = db.get_broadcast(broadcast_id)
    if not broadcast:
        logger.warning(f"❌ Рассылка #{broadcast_id} не найдена")
        return
    
    if not broadcast['is_active']:
        logger.info(f"⏸ Рассылка #{broadcast_id} отключена, пропускаем")
        return
    
    try:
        if broadcast['content_type'] == 'text' and broadcast['text']:
            await bot.send_message(GROUP_ID, broadcast['text'])
            logger.info(f"✅ Текст отправлен в группу {GROUP_ID}")
        
        elif broadcast['content_type'] == 'photo' and broadcast['photo_file_id']:
            await bot.send_photo(GROUP_ID, broadcast['photo_file_id'], 
                                 caption=broadcast['text'] or "")
            logger.info(f"✅ Фото отправлено в группу {GROUP_ID}")
        
        else:
            logger.warning(f"⚠️ Рассылка #{broadcast_id}: нет контента для отправки")
            return
        
        db.update_last_sent(broadcast_id)
        logger.info(f"✅ Рассылка #{broadcast_id} ('{broadcast['name']}') успешно отправлена")
        
    except Exception as e:
        error_msg = str(e).lower()
        logger.error(f"❌ Ошибка отправки рассылки #{broadcast_id}: {e}")
        
        if "chat not found" in error_msg:
            logger.error(f"❌ Группа {GROUP_ID} не найдена! Проверь GROUP_ID в переменных Railway")
        elif "bot is not a member" in error_msg:
            logger.error(f"❌ Бот не является участником группы {GROUP_ID}! Добавь бота в группу и назначь админом")
        elif "forbidden" in error_msg:
            logger.error(f"❌ Нет прав для отправки в группу {GROUP_ID}! Назначь бота администратором")

# ==================== ЗАГРУЗКА РАССЫЛОК ПРИ СТАРТЕ ====================
async def load_broadcasts():
    """Загружает все активные рассылки из БД в планировщик"""
    broadcasts = db.get_all_broadcasts()
    logger.info(f"📋 Загрузка {len(broadcasts)} рассылок из БД")
    
    for b in broadcasts:
        job_id = f"broadcast_{b['id']}"
        
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        
        if not b['is_active']:
            logger.info(f"⏸ Рассылка #{b['id']} ('{b['name']}') неактивна, пропускаем")
            continue
        
        if b['schedule_type'] == 'fixed' and b['hour'] is not None:
            trigger = CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE)
            scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
            logger.info(f"📅 Загружена fixed-рассылка #{b['id']}: '{b['name']}' в {b['hour']:02d}:{b['minute']:02d} ({TIMEZONE})")
        
        elif b['schedule_type'] == 'interval' and b['interval_minutes']:
            trigger = IntervalTrigger(minutes=b['interval_minutes'])
            scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
            mins = b['interval_minutes']
            hours = mins // 60
            minutes = mins % 60
            if hours > 0:
                schedule_text = f"каждые {hours}ч {minutes}мин" if minutes > 0 else f"каждые {hours}ч"
            else:
                schedule_text = f"каждые {minutes}мин"
            logger.info(f"⏱ Загружена interval-рассылка #{b['id']}: '{b['name']}' {schedule_text}")

# ==================== КОМАНДЫ ДЛЯ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    db.add_user(user.id, user.username, user.first_name)
    await message.answer(
        "✅ **Бот для рассылок в группах!**\n\n"
        "👨‍💻 Администратор создаёт рассылки, а бот отправляет их в группу.\n\n"
        "Доступные команды:\n"
        "/start - информация\n"
        "/id - узнать свой ID\n"
        "/info - статус подписки (если нужно)",
        parse_mode="Markdown"
    )

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    db.remove_user(message.from_user.id)
    await message.answer("❌ Вы отписались от уведомлений (если они были)")

@dp.message(Command("info"))
async def cmd_info(message: Message):
    users = db.get_active_users()
    is_subscribed = message.from_user.id in users
    status = "✅ Подписан на уведомления" if is_subscribed else "❌ Не подписан"
    await message.answer(f"📊 **Ваш статус:** {status}\n\n📢 Рассылки отправляются в группу, а не в личные сообщения.", parse_mode="Markdown")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    chat_id = message.chat.id
    chat_type = message.chat.type
    await message.answer(
        f"🆔 **Информация об этом чате**\n\n"
        f"📝 Тип: `{chat_type}`\n"
        f"🆔 ID: `{chat_id}`\n\n"
        f"Если это группа и ты хочешь, чтобы бот отправлял сюда рассылки — "
        f"скопируй этот ID (включая минус, если есть) и добавь в переменную `GROUP_ID` в Railway.",
        parse_mode="Markdown"
    )

# ==================== КОМАНДЫ ДЛЯ АДМИНИСТРАТОРА ====================
@dp.message(Command("time"))
async def cmd_time(message: Message):
    """Проверка текущего времени на сервере"""
    if not is_admin(message.from_user.id):
        return
    
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    now_utc = datetime.now(pytz.UTC)
    
    await message.answer(
        f"🕐 **Информация о времени**\n\n"
        f"📅 Часовой пояс бота: `{TIMEZONE}`\n"
        f"🕐 Текущее время: `{now.strftime('%Y-%m-%d %H:%M:%S')}`\n"
        f"🌍 UTC время: `{now_utc.strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
        f"📢 Рассылки отправляются по времени `{TIMEZONE}` в группу с ID: `{GROUP_ID}`",
        parse_mode="Markdown"
    )

@dp.message(Command("check"))
async def cmd_check_group(message: Message):
    """Проверка связи с группой"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        await bot.send_message(GROUP_ID, "🔔 **Тестовое сообщение от бота**\n\nЕсли вы это видите — бот правильно настроен и может отправлять рассылки в эту группу!", parse_mode="Markdown")
        await message.answer(f"✅ **Тестовое сообщение успешно отправлено в группу!**\n\n📢 ID группы: `{GROUP_ID}`\n\nБот имеет права на отправку сообщений.", parse_mode="Markdown")
    except Exception as e:
        error_msg = str(e)
        await message.answer(
            f"❌ **Ошибка отправки в группу**\n\n"
            f"📢 ID группы: `{GROUP_ID}`\n"
            f"❌ Ошибка: `{error_msg}`\n\n"
            f"**Что проверить:**\n"
            f"1. Бот добавлен в группу?\n"
            f"2. Бот назначен администратором группы?\n"
            f"3. ID группы правильный? (должен начинаться с `-100` для групп)\n\n"
            f"💡 Чтобы узнать ID группы, отправь в группе команду `/id`",
            parse_mode="Markdown"
        )

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ **У вас нет доступа к этой команде!**\n\nЭта команда только для администратора бота.", parse_mode="Markdown")
        logger.warning(f"⚠️ Неавторизованный доступ к /admin от {message.from_user.id}")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать рассылку", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Список рассылок", callback_data="admin_list")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👥 Подписчики", callback_data="admin_users")],
        [InlineKeyboardButton(text="⏸ Все рассылки", callback_data="admin_all_toggle")],
        [InlineKeyboardButton(text="🕐 Проверить время", callback_data="admin_time")],
        [InlineKeyboardButton(text="📢 Проверить группу", callback_data="admin_check")]
    ])
    
    broadcasts = db.get_all_broadcasts()
    active_count = len([b for b in broadcasts if b['is_active']])
    
    await message.answer(
        f"🔧 **Панель администратора**\n\n"
        f"📢 ID группы для рассылок: `{GROUP_ID}`\n"
        f"🕐 Часовой пояс: `{TIMEZONE}`\n"
        f"📋 Всего рассылок: `{len(broadcasts)}`\n"
        f"✅ Активных: `{active_count}`\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа!")
        return
    
    broadcasts = db.get_all_broadcasts()
    active_count = len([b for b in broadcasts if b['is_active']])
    
    await message.answer(
        f"📊 **Статистика бота**\n\n"
        f"📢 ID группы: `{GROUP_ID}`\n"
        f"🕐 Часовой пояс: `{TIMEZONE}`\n"
        f"📋 Всего рассылок: `{len(broadcasts)}`\n"
        f"✅ Активных: `{active_count}`\n"
        f"📨 Последние отправки видны в списке рассылок",
        parse_mode="Markdown"
    )

@dp.message(Command("users"))
async def cmd_users(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа!")
        return
    
    users = db.get_all_users()
    if not users:
        await message.answer("📭 Нет активных подписчиков на уведомления")
        return
    
    text = "📋 **Подписчики на уведомления:**\n\n"
    for user in users[:30]:
        name = user[2] or user[1] or "Аноним"
        text += f"• {name} - ID: `{user[0]}`\n"
    
    if len(users) > 30:
        text += f"\n...и ещё {len(users) - 30}"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    if message.from_user.id in admin_states:
        del admin_states[message.from_user.id]
        await message.answer("❌ Действие отменено")
    else:
        await message.answer("Нет активных действий")

# ==================== ОБРАБОТКА CALLBACK КНОПОК ====================
@dp.callback_query()
async def handle_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    data = callback.data
    await callback.answer()
    
    if data == "admin_create":
        admin_states[callback.from_user.id] = {"step": "name"}
        await callback.message.answer("📝 Введите **название** рассылки:", parse_mode="Markdown")
    
    elif data == "admin_list":
        await show_broadcasts_list(callback.message)
    
    elif data == "admin_stats":
        await cmd_stats(callback.message)
    
    elif data == "admin_users":
        await cmd_users(callback.message)
    
    elif data == "admin_time":
        await cmd_time(callback.message)
    
    elif data == "admin_check":
        await cmd_check_group(callback.message)
    
    elif data == "admin_all_toggle":
        await toggle_all_broadcasts(callback.message)
    
    elif data.startswith("broadcast_toggle_"):
        broadcast_id = int(data.split("_")[2])
        b = db.get_broadcast(broadcast_id)
        if b:
            new_status = not b['is_active']
            db.update_broadcast(broadcast_id, is_active=new_status)
            job_id = f"broadcast_{broadcast_id}"
            if new_status:
                if b['schedule_type'] == 'fixed':
                    trigger = CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE)
                    scheduler.add_job(send_broadcast, trigger, args=[broadcast_id], id=job_id)
                    logger.info(f"✅ Включена fixed-рассылка #{broadcast_id}: {b['hour']:02d}:{b['minute']:02d}")
                elif b['schedule_type'] == 'interval' and b['interval_minutes']:
                    trigger = IntervalTrigger(minutes=b['interval_minutes'])
                    scheduler.add_job(send_broadcast, trigger, args=[broadcast_id], id=job_id)
                    logger.info(f"✅ Включена interval-рассылка #{broadcast_id}")
            else:
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
                    logger.info(f"⏸ Выключена рассылка #{broadcast_id}")
            await callback.message.answer(f"🔄 Рассылка **{b['name']}** {'включена ✅' if new_status else 'отключена ⛔'}", parse_mode="Markdown")
            await show_broadcasts_list(callback.message)
    
    elif data.startswith("broadcast_delete_"):
        broadcast_id = int(data.split("_")[2])
        b = db.get_broadcast(broadcast_id)
        name = b['name'] if b else f"#{broadcast_id}"
        job_id = f"broadcast_{broadcast_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        db.delete_broadcast(broadcast_id)
        await callback.message.answer(f"🗑 Рассылка **{name}** удалена", parse_mode="Markdown")
        await show_broadcasts_list(callback.message)

async def show_broadcasts_list(message: types.Message):
    """Показать список всех рассылок"""
    broadcasts = db.get_all_broadcasts()
    
    if not broadcasts:
        await message.answer("📭 **Нет созданных рассылок**\n\nИспользуйте /admin → Создать рассылку", parse_mode="Markdown")
        return
    
    text = "📋 **Список рассылок**\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    for b in broadcasts:
        status = "✅" if b['is_active'] else "⛔"
        if b['schedule_type'] == 'fixed':
            schedule = f"{b['hour']:02d}:{b['minute']:02d}"
        else:
            mins = b['interval_minutes']
            hours = mins // 60
            minutes = mins % 60
            if hours > 0:
                schedule = f"каждые {hours}ч {minutes}мин" if minutes > 0 else f"каждые {hours}ч"
            else:
                schedule = f"каждые {minutes}мин"
        
        last_sent = f"\n   📨 Последняя: {b['last_sent_at']}" if b['last_sent_at'] else ""
        
        text += f"{status} **{b['name']}**\n   ⏰ {schedule}{last_sent}\n   📝 ID: {b['id']}\n\n"
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text=f"{status} {b['name'][:20]}", callback_data=f"broadcast_toggle_{b['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"broadcast_delete_{b['id']}")
        ])
    
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад в меню", callback_data="admin")])
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)

async def toggle_all_broadcasts(message: types.Message):
    """Включить/отключить все рассылки"""
    broadcasts = db.get_all_broadcasts()
    active = [b for b in broadcasts if b['is_active']]
    
    if active:
        for b in broadcasts:
            if b['is_active']:
                db.update_broadcast(b['id'], is_active=False)
                if scheduler.get_job(f"broadcast_{b['id']}"):
                    scheduler.remove_job(f"broadcast_{b['id']}")
        await message.answer("⛔ **Все рассылки отключены**", parse_mode="Markdown")
        logger.info("⏸ Все рассылки отключены администратором")
    else:
        for b in broadcasts:
            db.update_broadcast(b['id'], is_active=True)
            job_id = f"broadcast_{b['id']}"
            if b['schedule_type'] == 'fixed':
                trigger = CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE)
                scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
            elif b['schedule_type'] == 'interval' and b['interval_minutes']:
                trigger = IntervalTrigger(minutes=b['interval_minutes'])
                scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
        await message.answer("✅ **Все рассылки включены**", parse_mode="Markdown")
        logger.info("✅ Все рассылки включены администратором")

# ==================== СОЗДАНИЕ РАССЫЛКИ (ПОШАГОВЫЙ МАСТЕР) ====================
@dp.message()
async def handle_input(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    if message.from_user.id not in admin_states:
        return
    
    state = admin_states[message.from_user.id]
    step = state.get("step")
    
    # Шаг 1: Название
    if step == "name":
        state["name"] = message.text
        state["step"] = "type"
        await message.answer("📝 **Тип контента**\n\nОтправьте `текст` или `фото`:", parse_mode="Markdown")
    
    # Шаг 2: Тип контента
    elif step == "type":
        if message.text.lower() in ["текст", "text"]:
            state["content_type"] = "text"
            state["step"] = "text_content"
            await message.answer("📝 Отправьте **текст** рассылки:", parse_mode="Markdown")
        elif message.text.lower() in ["фото", "photo"]:
            state["content_type"] = "photo"
            state["step"] = "photo_content"
            await message.answer("🖼 Отправьте **фото** (можно с подписью):", parse_mode="Markdown")
        else:
            await message.answer("❌ Отправьте 'текст' или 'фото'")
    
    # Шаг 3a: Текст
    elif step == "text_content":
        state["text"] = message.text
        state["step"] = "schedule_type"
        await message.answer(
            "⏰ **Тип расписания**\n\n"
            "Отправьте:\n"
            "`1` - В определённое время (ежедневно)\n"
            "`2` - Каждый час / с интервалом",
            parse_mode="Markdown"
        )
    
    # Шаг 3b: Фото
    elif step == "photo_content":
        if message.photo:
            state["photo_file_id"] = message.photo[-1].file_id
            state["text"] = message.caption or ""
            state["step"] = "schedule_type"
            await message.answer(
                "⏰ **Тип расписания**\n\n"
                "Отправьте:\n"
                "`1` - В определённое время (ежедневно)\n"
                "`2` - Каждый час / с интервалом",
                parse_mode="Markdown"
            )
        else:
            await message.answer("❌ Отправьте фото")
    
    # Шаг 4: Выбор типа расписания
    elif step == "schedule_type":
        if message.text == "1":
            state["schedule_type"] = "fixed"
            state["step"] = "fixed_time"
            await message.answer(f"⏰ Введите **время** в формате `HH:MM` (например, 09:30)\n\n🕐 Часовой пояс: `{TIMEZONE}`", parse_mode="Markdown")
        elif message.text == "2":
            state["schedule_type"] = "interval"
            state["step"] = "interval_minutes"
            await message.answer(
                "⏰ **Интервал в минутах**\n\n"
                "Примеры:\n"
                "`60` - каждый час\n"
                "`30` - каждые 30 минут\n"
                "`120` - каждые 2 часа\n\n"
                "Отправьте число:",
                parse_mode="Markdown"
            )
        else:
            await message.answer("❌ Отправьте 1 или 2")
    
    # Шаг 5a: Фиксированное время
    elif step == "fixed_time":
        try:
            hour, minute = map(int, message.text.split(':'))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                state["hour"] = hour
                state["minute"] = minute
                
                broadcast_id = db.add_broadcast(
                    name=state["name"],
                    content_type=state["content_type"],
                    schedule_type="fixed",
                    text=state.get("text"),
                    photo_file_id=state.get("photo_file_id"),
                    hour=hour,
                    minute=minute
                )
                
                trigger = CronTrigger(hour=hour, minute=minute, timezone=TIMEZONE)
                scheduler.add_job(
                    send_broadcast,
                    trigger,
                    args=[broadcast_id],
                    id=f"broadcast_{broadcast_id}"
                )
                
                await message.answer(
                    f"✅ **Рассылка создана!**\n\n"
                    f"📢 Название: **{state['name']}**\n"
                    f"⏰ Время: **{hour:02d}:{minute:02d}** ({TIMEZONE})\n"
                    f"📢 Отправляется в группу: `{GROUP_ID}`\n\n"
                    f"Используйте /admin для управления",
                    parse_mode="Markdown"
                )
                del admin_states[message.from_user.id]
            else:
                raise ValueError
        except:
            await message.answer("❌ Неверный формат. Пример: 09:30")
    
    # Шаг 5b: Интервал
    elif step == "interval_minutes":
        try:
            interval = int(message.text)
            if interval <= 0:
                raise ValueError
            
            broadcast_id = db.add_broadcast(
                name=state["name"],
                content_type=state["content_type"],
                schedule_type="interval",
                text=state.get("text"),
                photo_file_id=state.get("photo_file_id"),
                interval_minutes=interval
            )
            
            scheduler.add_job(
                send_broadcast,
                IntervalTrigger(minutes=interval),
                args=[broadcast_id],
                id=f"broadcast_{broadcast_id}"
            )
            
            hours = interval // 60
            minutes = interval % 60
            if hours > 0:
                schedule_text = f"каждые {hours}ч {minutes}мин" if minutes > 0 else f"каждые {hours}ч"
            else:
                schedule_text = f"каждые {minutes}мин"
            
            await message.answer(
                f"✅ **Рассылка создана!**\n\n"
                f"📢 Название: **{state['name']}**\n"
                f"⏰ Расписание: **{schedule_text}**\n"
                f"📢 Отправляется в группу: `{GROUP_ID}`\n\n"
                f"Используйте /admin для управления",
                parse_mode="Markdown"
            )
            del admin_states[message.from_user.id]
        except:
            await message.answer("❌ Введите положительное число (минуты)")

# ==================== ЗАПУСК БОТА ====================
async def main():
    logger.info(f"🚀 Бот запускается...")
    logger.info(f"📅 Часовой пояс: {TIMEZONE}")
    logger.info(f"📢 Группа для рассылок: {GROUP_ID}")
    logger.info(f"👑 Админ ID: {ADMIN_ID}")
    
    await load_broadcasts()
    scheduler.start()
    
    logger.info("✅ Бот готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
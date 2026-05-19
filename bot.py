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
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID")  # ID админской группы
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# Проверка наличия обязательных переменных
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден! Добавь переменную в Railway")
if not ADMIN_ID:
    raise ValueError("❌ ADMIN_ID не найден! Добавь переменную в Railway")

# Преобразуем ADMIN_GROUP_ID в int если он задан
ADMIN_GROUP_ID = int(ADMIN_GROUP_ID) if ADMIN_GROUP_ID else None

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Временные хранилища для состояний админа
admin_states = {}

# ==================== БАЗА ДАННЫХ ====================
import sqlite3
import json

class Database:
    def __init__(self):
        self.conn = sqlite3.connect('bot_database.db', check_same_thread=False)
        self.init_tables()
    
    def init_tables(self):
        cursor = self.conn.cursor()
        
        # Таблица групп/каналов для рассылок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS target_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE NOT NULL,
                name TEXT,
                chat_type TEXT,
                is_active INTEGER DEFAULT 1,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица рассылок (с привязкой к группе)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER,
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (group_id) REFERENCES target_groups(id)
            )
        ''')
        
        # Таблица пользователей (подписчики на уведомления)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        ''')
        
        # Таблица логов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broadcast_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broadcast_id INTEGER,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT,
                error TEXT
            )
        ''')
        
        self.conn.commit()
        logger.info("✅ База данных инициализирована")
    
    # === РАБОТА С ГРУППАМИ ДЛЯ РАССЫЛОК ===
    def add_target_group(self, chat_id: str, name: str = None, chat_type: str = None):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO target_groups (chat_id, name, chat_type, is_active)
            VALUES (?, ?, ?, 1)
        ''', (chat_id, name, chat_type))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_all_target_groups(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT id, chat_id, name, chat_type, is_active, added_at FROM target_groups ORDER BY id')
        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                'id': row[0],
                'chat_id': row[1],
                'name': row[2] or f"Группа {row[1]}",
                'chat_type': row[3],
                'is_active': bool(row[4]),
                'added_at': row[5]
            })
        return result
    
    def get_target_group(self, group_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT id, chat_id, name, chat_type, is_active FROM target_groups WHERE id = ?', (group_id,))
        row = cursor.fetchone()
        if row:
            return {
                'id': row[0],
                'chat_id': row[1],
                'name': row[2] or f"Группа {row[1]}",
                'chat_type': row[3],
                'is_active': bool(row[4])
            }
        return None
    
    def get_target_group_by_chat_id(self, chat_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT id, chat_id, name, chat_type, is_active FROM target_groups WHERE chat_id = ?', (chat_id,))
        row = cursor.fetchone()
        if row:
            return {
                'id': row[0],
                'chat_id': row[1],
                'name': row[2] or f"Группа {row[1]}",
                'chat_type': row[3],
                'is_active': bool(row[4])
            }
        return None
    
    def update_target_group_name(self, group_id, name):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE target_groups SET name = ? WHERE id = ?', (name, group_id))
        self.conn.commit()
    
    def delete_target_group(self, group_id):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM broadcasts WHERE group_id = ?', (group_id,))
        cursor.execute('DELETE FROM target_groups WHERE id = ?', (group_id,))
        self.conn.commit()
    
    def toggle_target_group(self, group_id, is_active):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE target_groups SET is_active = ? WHERE id = ?', (is_active, group_id))
        self.conn.commit()
    
    # === РАБОТА С РАССЫЛКАМИ ===
    def add_broadcast(self, group_id, name, content_type, schedule_type, 
                      text=None, photo_file_id=None, hour=None, minute=None, 
                      interval_minutes=None, days=None):
        cursor = self.conn.cursor()
        days_json = json.dumps(days) if days else None
        cursor.execute('''
            INSERT INTO broadcasts (group_id, name, content_type, text, photo_file_id, 
                                    schedule_type, hour, minute, interval_minutes, days, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ''', (group_id, name, content_type, text, photo_file_id, 
              schedule_type, hour, minute, interval_minutes, days_json))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_all_broadcasts(self, group_id=None):
        cursor = self.conn.cursor()
        if group_id:
            cursor.execute('''
                SELECT id, group_id, name, content_type, schedule_type, hour, minute, 
                       interval_minutes, days, is_active, last_sent_at, created_at 
                FROM broadcasts WHERE group_id = ? ORDER BY created_at DESC
            ''', (group_id,))
        else:
            cursor.execute('''
                SELECT id, group_id, name, content_type, schedule_type, hour, minute, 
                       interval_minutes, days, is_active, last_sent_at, created_at 
                FROM broadcasts ORDER BY created_at DESC
            ''')
        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                'id': row[0],
                'group_id': row[1],
                'name': row[2],
                'content_type': row[3],
                'schedule_type': row[4],
                'hour': row[5],
                'minute': row[6],
                'interval_minutes': row[7],
                'days': json.loads(row[8]) if row[8] else None,
                'is_active': bool(row[9]),
                'last_sent_at': row[10],
                'created_at': row[11]
            })
        return result
    
    def get_broadcast(self, broadcast_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT id, group_id, name, content_type, text, photo_file_id, schedule_type, 
                   hour, minute, interval_minutes, days, is_active, last_sent_at 
            FROM broadcasts WHERE id = ?
        ''', (broadcast_id,))
        row = cursor.fetchone()
        if row:
            return {
                'id': row[0],
                'group_id': row[1],
                'name': row[2],
                'content_type': row[3],
                'text': row[4],
                'photo_file_id': row[5],
                'schedule_type': row[6],
                'hour': row[7],
                'minute': row[8],
                'interval_minutes': row[9],
                'days': json.loads(row[10]) if row[10] else None,
                'is_active': bool(row[11]),
                'last_sent_at': row[12]
            }
        return None
    
    def update_broadcast(self, broadcast_id, **kwargs):
        cursor = self.conn.cursor()
        allowed = ['name', 'content_type', 'text', 'photo_file_id', 'hour', 
                   'minute', 'interval_minutes', 'days', 'is_active']
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
    
    def log_broadcast(self, broadcast_id, status, error=None):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO broadcast_logs (broadcast_id, status, error)
            VALUES (?, ?, ?)
        ''', (broadcast_id, status, error))
        self.conn.commit()
    
    # === РАБОТА С ПОЛЬЗОВАТЕЛЯМИ ===
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
    
    def get_user_count(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users WHERE is_active = 1')
        return cursor.fetchone()[0]

db = Database()

# ==================== ФУНКЦИЯ ПРОВЕРКИ АДМИНА ====================
def is_admin(user_id):
    return user_id == ADMIN_ID

def is_admin_chat(chat_id):
    """Проверяет, является ли чат админской группой"""
    return ADMIN_GROUP_ID and chat_id == ADMIN_GROUP_ID

async def send_to_admin(text, parse_mode="Markdown"):
    """Отправляет сообщение в админскую группу (если она настроена)"""
    if ADMIN_GROUP_ID:
        try:
            await bot.send_message(ADMIN_GROUP_ID, text, parse_mode=parse_mode)
        except Exception as e:
            logger.error(f"Ошибка отправки в админскую группу: {e}")

# ==================== ОТПРАВКА РАССЫЛКИ ====================
async def send_broadcast(broadcast_id: int):
    """Отправляет рассылку в привязанную группу"""
    logger.info(f"🚀 Запуск рассылки #{broadcast_id} в {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    broadcast = db.get_broadcast(broadcast_id)
    if not broadcast:
        logger.warning(f"❌ Рассылка #{broadcast_id} не найдена")
        return
    
    if not broadcast['is_active']:
        logger.info(f"⏸ Рассылка #{broadcast_id} отключена")
        return
    
    group = db.get_target_group(broadcast['group_id'])
    if not group or not group['is_active']:
        logger.warning(f"❌ Группа для рассылки #{broadcast_id} не найдена или отключена")
        await send_to_admin(f"❌ **Ошибка рассылки**\n\nРассылка: {broadcast['name']}\nГруппа не найдена или отключена")
        return
    
    chat_id = int(group['chat_id'])
    
    try:
        if broadcast['content_type'] == 'text' and broadcast['text']:
            await bot.send_message(chat_id, broadcast['text'])
            logger.info(f"✅ Текст отправлен в {group['name']} ({chat_id})")
        
        elif broadcast['content_type'] == 'photo' and broadcast['photo_file_id']:
            await bot.send_photo(chat_id, broadcast['photo_file_id'], 
                                 caption=broadcast['text'] or "")
            logger.info(f"✅ Фото отправлено в {group['name']} ({chat_id})")
        
        db.update_last_sent(broadcast_id)
        db.log_broadcast(broadcast_id, "success")
        logger.info(f"✅ Рассылка #{broadcast_id} ('{broadcast['name']}') успешно отправлена в {group['name']}")
        
        # Уведомление в админскую группу
        await send_to_admin(
            f"✅ **Рассылка выполнена**\n\n"
            f"📢 Название: {broadcast['name']}\n"
            f"📬 Группа: {group['name']}\n"
            f"🕐 Время: {datetime.now().strftime('%H:%M:%S')}"
        )
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ Ошибка отправки рассылки #{broadcast_id}: {error_msg}")
        db.log_broadcast(broadcast_id, "error", error_msg)
        
        await send_to_admin(
            f"❌ **Ошибка рассылки**\n\n"
            f"📢 Название: {broadcast['name']}\n"
            f"📬 Группа: {group['name']}\n"
            f"❌ Ошибка: {error_msg[:200]}"
        )
        
        if "chat not found" in error_msg.lower():
            logger.error(f"❌ Группа {chat_id} не найдена!")
        elif "bot is not a member" in error_msg.lower():
            logger.error(f"❌ Бот не является участником группы {chat_id}!")

# ==================== ЗАГРУЗКА РАССЫЛОК ПРИ СТАРТЕ ====================
async def load_all_broadcasts():
    """Загружает все активные рассылки из БД в планировщик"""
    broadcasts = db.get_all_broadcasts()
    logger.info(f"📋 Загрузка {len(broadcasts)} рассылок из БД")
    
    for b in broadcasts:
        job_id = f"broadcast_{b['id']}"
        
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        
        if not b['is_active']:
            logger.info(f"⏸ Рассылка #{b['id']} ('{b['name']}') неактивна")
            continue
        
        if b['schedule_type'] == 'fixed' and b['hour'] is not None:
            trigger = CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE)
            scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
            logger.info(f"📅 Загружена fixed-рассылка #{b['id']}: '{b['name']}' в {b['hour']:02d}:{b['minute']:02d}")
        
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
    
    # Если команда отправлена из группы
    if message.chat.type in ['group', 'supergroup']:
        chat_id = str(message.chat.id)
        chat_name = message.chat.title or f"Группа {chat_id}"
        
        # Проверяем, есть ли уже такая группа в БД
        existing = db.get_target_group_by_chat_id(chat_id)
        if not existing:
            db.add_target_group(chat_id, chat_name, message.chat.type)
            if is_admin(message.from_user.id):
                await message.answer(
                    f"✅ **Группа добавлена!**\n\n"
                    f"📢 Название: {chat_name}\n"
                    f"🆔 ID: `{chat_id}`\n\n"
                    f"Теперь ты можешь создавать рассылки для этой группы.\n"
                    f"Используй /admin в админской группе или личке.",
                    parse_mode="Markdown"
                )
            else:
                await message.answer(
                    f"✅ **Группа добавлена в базу бота!**\n\n"
                    f"📢 Название: {chat_name}\n\n"
                    f"Администратор бота теперь может настраивать рассылки для этой группы.",
                    parse_mode="Markdown"
                )
        else:
            await message.answer(f"✅ **Группа уже добавлена**\n\n📢 {chat_name}\n\nДля управления используй /admin в админской группе.", parse_mode="Markdown")
    else:
        await message.answer(
            "✅ **Бот для рассылок в группах!**\n\n"
            "📢 **Как использовать:**\n"
            "1. Добавь бота в группу для рассылок\n"
            "2. Назначь бота администратором\n"
            "3. Отправь в группе команду `/start`\n"
            "4. Используй /admin в этой админской группе для управления\n\n"
            "📌 **Админская группа:**\n"
            f"{'Уже настроена ✅' if ADMIN_GROUP_ID else 'Не настроена. Добавь переменную ADMIN_GROUP_ID в Railway'}\n\n"
            "Доступные команды:\n"
            "/start - начать\n"
            "/id - узнать ID чата\n"
            "/admin - панель управления (только для админа)",
            parse_mode="Markdown"
        )

@dp.message(Command("id"))
async def cmd_id(message: Message):
    chat_id = message.chat.id
    chat_type = message.chat.type
    chat_title = message.chat.title or "Личный чат"
    
    await message.answer(
        f"🆔 **Информация об этом чате**\n\n"
        f"📝 Название: `{chat_title}`\n"
        f"📝 Тип: `{chat_type}`\n"
        f"🆔 ID: `{chat_id}`\n\n"
        f"💡 Скопируй этот ID, если хочешь:\n"
        f"• Добавить группу для рассылок\n"
        f"• Настроить админскую группу (ADMIN_GROUP_ID)",
        parse_mode="Markdown"
    )

@dp.message(Command("info"))
async def cmd_info(message: Message):
    groups = db.get_all_target_groups()
    broadcasts = db.get_all_broadcasts()
    active_broadcasts = len([b for b in broadcasts if b['is_active']])
    
    await message.answer(
        f"📊 **Информация о боте**\n\n"
        f"📢 Групп для рассылок: `{len(groups)}`\n"
        f"📋 Всего рассылок: `{len(broadcasts)}`\n"
        f"✅ Активных рассылок: `{active_broadcasts}`\n"
        f"🕐 Часовой пояс: `{TIMEZONE}`\n"
        f"👑 Админская группа: `{ADMIN_GROUP_ID if ADMIN_GROUP_ID else 'Не настроена'}`\n\n"
        f"👨‍💻 Для управления используй /admin",
        parse_mode="Markdown"
    )

# ==================== КОМАНДЫ ДЛЯ АДМИНИСТРАТОРА ====================
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    # Проверяем, что команду отправил админ
    if not is_admin(message.from_user.id):
        await message.answer("⛔ **У вас нет доступа к этой команде!**", parse_mode="Markdown")
        return
    
    # Проверяем, что команда отправлена из админской группы (если она настроена)
    if ADMIN_GROUP_ID and message.chat.id != ADMIN_GROUP_ID and message.chat.type != 'private':
        await message.answer(
            f"⛔ **Управление ботом доступно только в админской группе!**\n\n"
            f"📢 ID админской группы: `{ADMIN_GROUP_ID}`\n\n"
            f"Пожалуйста, перейдите в админскую группу для управления.",
            parse_mode="Markdown"
        )
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Группы для рассылок", callback_data="admin_groups")],
        [InlineKeyboardButton(text="➕ Создать рассылку", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Все рассылки", callback_data="admin_list")],
        [InlineKeyboardButton(text="⏸ Все рассылки", callback_data="admin_all_toggle")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🕐 Время сервера", callback_data="admin_time")]
    ])
    
    groups = db.get_all_target_groups()
    broadcasts = db.get_all_broadcasts()
    active_broadcasts = len([b for b in broadcasts if b['is_active']])
    
    await message.answer(
        f"🔧 **Панель администратора**\n\n"
        f"📢 Групп для рассылок: `{len(groups)}`\n"
        f"📋 Всего рассылок: `{len(broadcasts)}`\n"
        f"✅ Активных: `{active_broadcasts}`\n"
        f"🕐 Часовой пояс: `{TIMEZONE}`\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    groups = db.get_all_target_groups()
    broadcasts = db.get_all_broadcasts()
    active_broadcasts = len([b for b in broadcasts if b['is_active']])
    
    text = f"📊 **Статистика бота**\n\n"
    text += f"📢 Групп для рассылок: `{len(groups)}`\n"
    text += f"📋 Всего рассылок: `{len(broadcasts)}`\n"
    text += f"✅ Активных рассылок: `{active_broadcasts}`\n"
    text += f"🕐 Часовой пояс: `{TIMEZONE}`\n\n"
    text += f"**Группы для рассылок:**\n"
    
    for g in groups:
        group_broadcasts = [b for b in broadcasts if b['group_id'] == g['id']]
        status = "✅" if g['is_active'] else "⛔"
        text += f"{status} {g['name']}: {len(group_broadcasts)} рассылок\n"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("time"))
async def cmd_time(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    
    await message.answer(
        f"🕐 **Время на сервере**\n\n"
        f"Часовой пояс: `{TIMEZONE}`\n"
        f"Текущее время: `{now.strftime('%Y-%m-%d %H:%M:%S')}`\n"
        f"День недели: `{now.strftime('%A')}`",
        parse_mode="Markdown"
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    if message.from_user.id in admin_states:
        del admin_states[message.from_user.id]
        await message.answer("❌ Действие отменено")
    else:
        await message.answer("Нет активных действий")

# ==================== УПРАВЛЕНИЕ ГРУППАМИ ====================
@dp.callback_query()
async def handle_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    data = callback.data
    await callback.answer()
    
    # Главные меню
    if data == "admin_groups":
        await show_groups_menu(callback.message)
    
    elif data == "admin_create":
        await start_create_broadcast(callback.message)
    
    elif data == "admin_list":
        await show_broadcasts_list(callback.message)
    
    elif data == "admin_all_toggle":
        await toggle_all_broadcasts(callback.message)
    
    elif data == "admin_stats":
        await cmd_stats(callback.message)
    
    elif data == "admin_time":
        await cmd_time(callback.message)
    
    # Действия с группами
    elif data == "group_add":
        admin_states[callback.from_user.id] = {"step": "add_group_chat_id"}
        await callback.message.answer(
            "📢 **Добавление группы для рассылок**\n\n"
            "Отправьте ID группы/канала (можно узнать через команду /id в группе):\n\n"
            "Пример: `-1001234567890`\n\n"
            "Или отправьте /cancel для отмены",
            parse_mode="Markdown"
        )
    
    elif data.startswith("group_toggle_"):
        group_id = int(data.split("_")[2])
        group = db.get_target_group(group_id)
        if group:
            new_status = not group['is_active']
            db.toggle_target_group(group_id, 1 if new_status else 0)
            
            # Включаем/выключаем все рассылки этой группы в планировщике
            broadcasts = db.get_all_broadcasts(group_id)
            for b in broadcasts:
                job_id = f"broadcast_{b['id']}"
                if new_status and b['is_active']:
                    if b['schedule_type'] == 'fixed':
                        trigger = CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE)
                        scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
                    elif b['schedule_type'] == 'interval' and b['interval_minutes']:
                        trigger = IntervalTrigger(minutes=b['interval_minutes'])
                        scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
                else:
                    if scheduler.get_job(job_id):
                        scheduler.remove_job(job_id)
            
            await callback.message.answer(f"🔄 Группа **{group['name']}** {'включена ✅' if new_status else 'отключена ⛔'}")
            await show_groups_menu(callback.message)
    
    elif data.startswith("group_delete_"):
        group_id = int(data.split("_")[2])
        group = db.get_target_group(group_id)
        if group:
            # Удаляем все рассылки этой группы из планировщика
            broadcasts = db.get_all_broadcasts(group_id)
            for b in broadcasts:
                if scheduler.get_job(f"broadcast_{b['id']}"):
                    scheduler.remove_job(f"broadcast_{b['id']}")
            db.delete_target_group(group_id)
            await callback.message.answer(f"🗑 Группа **{group['name']}** и все её рассылки удалены")
            await show_groups_menu(callback.message)
    
    elif data.startswith("group_rename_"):
        group_id = int(data.split("_")[2])
        admin_states[callback.from_user.id] = {"step": "rename_group", "group_id": group_id}
        await callback.message.answer("✏️ Введите новое название для группы:")
    
    elif data.startswith("group_broadcasts_"):
        group_id = int(data.split("_")[2])
        await show_group_broadcasts(callback.message, group_id)
    
    # Действия с рассылками
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
                elif b['schedule_type'] == 'interval' and b['interval_minutes']:
                    trigger = IntervalTrigger(minutes=b['interval_minutes'])
                    scheduler.add_job(send_broadcast, trigger, args=[broadcast_id], id=job_id)
            else:
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
            await callback.message.answer(f"🔄 Рассылка **{b['name']}** {'включена ✅' if new_status else 'отключена ⛔'}")
            await show_broadcasts_list(callback.message)
    
    elif data.startswith("broadcast_delete_"):
        broadcast_id = int(data.split("_")[2])
        b = db.get_broadcast(broadcast_id)
        if b:
            if scheduler.get_job(f"broadcast_{broadcast_id}"):
                scheduler.remove_job(f"broadcast_{broadcast_id}")
            db.delete_broadcast(broadcast_id)
            await callback.message.answer(f"🗑 Рассылка **{b['name']}** удалена")
            await show_broadcasts_list(callback.message)
    
    # Выбор группы для создания рассылки
    elif data.startswith("select_group_"):
        group_id = int(data.split("_")[2])
        group = db.get_target_group(group_id)
        if group:
            admin_states[callback.from_user.id] = {
                "step": "create_name",
                "group_id": group_id,
                "group_name": group['name']
            }
            await callback.message.answer(
                f"📝 **Создание рассылки для группы:** {group['name']}\n\n"
                f"Введите **название** рассылки:",
                parse_mode="Markdown"
            )

async def show_groups_menu(message: types.Message):
    """Показать меню управления группами для рассылок"""
    groups = db.get_all_target_groups()
    
    text = "📢 **Группы для рассылок**\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    if not groups:
        text += "Нет добавленных групп.\n\n"
        text += "**Как добавить:**\n"
        text += "1. Добавь бота в группу\n"
        text += "2. Назначь администратором\n"
        text += "3. Отправь в группе /start\n"
        text += "4. Либо добавь вручную по ID через кнопку ниже\n"
    else:
        for g in groups:
            status = "✅" if g['is_active'] else "⛔"
            text += f"{status} **{g['name']}**\n"
            text += f"   🆔 ID: `{g['chat_id']}`\n"
            
            # Считаем рассылки для этой группы
            broadcasts = db.get_all_broadcasts(g['id'])
            text += f"   📋 Рассылок: {len(broadcasts)}\n\n"
            
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(text=f"{
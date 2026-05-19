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
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# Преобразуем ADMIN_GROUP_ID в int если он задан
ADMIN_GROUP_ID = int(ADMIN_GROUP_ID) if ADMIN_GROUP_ID else None

# Проверка наличия обязательных переменных
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден!")
if not ADMIN_ID:
    raise ValueError("❌ ADMIN_ID не найден!")

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
        
        # Таблица групп для рассылок
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
        
        # Таблица рассылок
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
        
        # Таблица пользователей
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

# ==================== ФУНКЦИИ ПРОВЕРКИ ====================
def is_admin(user_id):
    return user_id == ADMIN_ID

def is_admin_chat(chat_id):
    """Проверяет, является ли чат админской группой"""
    return ADMIN_GROUP_ID and chat_id == ADMIN_GROUP_ID

async def send_to_admin(text, parse_mode="Markdown"):
    """Отправляет сообщение в админскую группу"""
    if ADMIN_GROUP_ID:
        try:
            await bot.send_message(ADMIN_GROUP_ID, text, parse_mode=parse_mode)
        except Exception as e:
            logger.error(f"Ошибка отправки в админскую группу: {e}")

# ==================== ОТПРАВКА РАССЫЛКИ ====================
async def send_broadcast(broadcast_id: int):
    logger.info(f"🚀 Запуск рассылки #{broadcast_id}")
    
    broadcast = db.get_broadcast(broadcast_id)
    if not broadcast or not broadcast['is_active']:
        return
    
    group = db.get_target_group(broadcast['group_id'])
    if not group or not group['is_active']:
        return
    
    chat_id = int(group['chat_id'])
    
    try:
        if broadcast['content_type'] == 'text' and broadcast['text']:
            await bot.send_message(chat_id, broadcast['text'])
        elif broadcast['content_type'] == 'photo' and broadcast['photo_file_id']:
            await bot.send_photo(chat_id, broadcast['photo_file_id'], 
                                 caption=broadcast['text'] or "")
        
        db.update_last_sent(broadcast_id)
        db.log_broadcast(broadcast_id, "success")
        logger.info(f"✅ Рассылка #{broadcast_id} отправлена в {group['name']}")
        
        await send_to_admin(f"✅ **{broadcast['name']}**\n➡️ Отправлено в {group['name']}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        db.log_broadcast(broadcast_id, "error", str(e))
        await send_to_admin(f"❌ **{broadcast['name']}**\n➡️ {group['name']}\nОшибка: {str(e)[:100]}")

# ==================== ЗАГРУЗКА РАССЫЛОК ====================
async def load_all_broadcasts():
    broadcasts = db.get_all_broadcasts()
    logger.info(f"📋 Загрузка {len(broadcasts)} рассылок")
    
    for b in broadcasts:
        if not b['is_active']:
            continue
        
        job_id = f"broadcast_{b['id']}"
        
        if b['schedule_type'] == 'fixed' and b['hour'] is not None:
            trigger = CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE)
            scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
            logger.info(f"📅 Загружена: {b['name']} в {b['hour']:02d}:{b['minute']:02d}")
        
        elif b['schedule_type'] == 'interval' and b['interval_minutes']:
            trigger = IntervalTrigger(minutes=b['interval_minutes'])
            scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
            logger.info(f"⏱ Загружена: {b['name']} каждые {b['interval_minutes']} мин")

# ==================== КОМАНДЫ ДЛЯ ВСЕХ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    db.add_user(user.id, user.username, user.first_name)
    
    if message.chat.type in ['group', 'supergroup']:
        chat_id = str(message.chat.id)
        chat_name = message.chat.title or f"Группа {chat_id}"
        
        existing = db.get_target_group_by_chat_id(chat_id)
        if not existing:
            db.add_target_group(chat_id, chat_name, message.chat.type)
            await message.answer(
                f"✅ **Группа добавлена!**\n\n"
                f"📢 {chat_name}\n"
                f"🆔 ID: `{chat_id}`\n\n"
                f"Теперь администратор может создавать рассылки для этой группы.",
                parse_mode="Markdown"
            )
            await send_to_admin(f"➕ **Новая группа**\n{chat_name}\nID: `{chat_id}`")
        else:
            await message.answer(f"✅ Группа уже добавлена: {chat_name}")
    else:
        await message.answer(
            "✅ **Бот для рассылок в группах!**\n\n"
            "📢 **Как использовать:**\n"
            "1. Добавь бота в группу\n"
            "2. Сделай бота администратором\n"
            "3. Отправь в группе /start\n"
            "4. Используй /admin для управления\n\n"
            f"📌 Админская группа: {'✅ настроена' if ADMIN_GROUP_ID else '❌ не настроена'}\n\n"
            "Команды:\n"
            "/start - информация\n"
            "/id - ID чата\n"
            "/admin - панель (только для админа)",
            parse_mode="Markdown"
        )

@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(
        f"🆔 **Информация**\n\n"
        f"📝 Название: {message.chat.title or 'Личный чат'}\n"
        f"📝 Тип: {message.chat.type}\n"
        f"🆔 ID: `{message.chat.id}`",
        parse_mode="Markdown"
    )

# ==================== КОМАНДЫ ДЛЯ АДМИНА ====================
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа!")
        return
    
    # Проверяем, что команда из админской группы (если она настроена)
    if ADMIN_GROUP_ID and message.chat.id != ADMIN_GROUP_ID and message.chat.type != 'private':
        await message.answer(
            f"⛔ **Управление только в админской группе!**\n\n"
            f"ID админской группы: `{ADMIN_GROUP_ID}`",
            parse_mode="Markdown"
        )
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Группы для рассылок", callback_data="admin_groups")],
        [InlineKeyboardButton(text="➕ Создать рассылку", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Все рассылки", callback_data="admin_list")],
        [InlineKeyboardButton(text="⏸ Все рассылки", callback_data="admin_all_toggle")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🕐 Время", callback_data="admin_time")]
    ])
    
    groups = db.get_all_target_groups()
    broadcasts = db.get_all_broadcasts()
    active = len([b for b in broadcasts if b['is_active']])
    
    await message.answer(
        f"🔧 **Панель администратора**\n\n"
        f"📢 Групп: `{len(groups)}`\n"
        f"📋 Рассылок: `{len(broadcasts)}` (активных: `{active}`)\n"
        f"🕐 Часовой пояс: `{TIMEZONE}`",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    groups = db.get_all_target_groups()
    broadcasts = db.get_all_broadcasts()
    active = len([b for b in broadcasts if b['is_active']])
    
    text = f"📊 **Статистика**\n\n"
    text += f"📢 Групп: `{len(groups)}`\n"
    text += f"📋 Рассылок: `{len(broadcasts)}`\n"
    text += f"✅ Активных: `{active}`\n\n"
    text += f"**Группы:**\n"
    
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
        f"Текущее время: `{now.strftime('%Y-%m-%d %H:%M:%S')}`",
        parse_mode="Markdown"
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    if message.from_user.id in admin_states:
        del admin_states[message.from_user.id]
        await message.answer("❌ Отменено")
    else:
        await message.answer("Нет активных действий")

# ==================== CALLBACK ОБРАБОТЧИК ====================
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
        await show_all_broadcasts(callback.message)
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
            "📢 **Добавление группы**\n\n"
            "Отправьте ID группы (можно узнать через /id в группе):\n"
            "Пример: `-1001234567890`\n\n"
            "Или /cancel",
            parse_mode="Markdown"
        )
    
    elif data.startswith("group_toggle_"):
        group_id = int(data.split("_")[2])
        group = db.get_target_group(group_id)
        if group:
            new_status = not group['is_active']
            db.toggle_target_group(group_id, 1 if new_status else 0)
            
            # Обновляем рассылки в планировщике
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
            broadcasts = db.get_all_broadcasts(group_id)
            for b in broadcasts:
                if scheduler.get_job(f"broadcast_{b['id']}"):
                    scheduler.remove_job(f"broadcast_{b['id']}")
            db.delete_target_group(group_id)
            await callback.message.answer(f"🗑 Группа **{group['name']}** удалена")
            await show_groups_menu(callback.message)
    
    elif data.startswith("group_rename_"):
        group_id = int(data.split("_")[2])
        admin_states[callback.from_user.id] = {"step": "rename_group", "group_id": group_id}
        await callback.message.answer("✏️ Введите новое название:")
    
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
            await show_all_broadcasts(callback.message)
    
    elif data.startswith("broadcast_delete_"):
        broadcast_id = int(data.split("_")[2])
        b = db.get_broadcast(broadcast_id)
        if b:
            if scheduler.get_job(f"broadcast_{broadcast_id}"):
                scheduler.remove_job(f"broadcast_{broadcast_id}")
            db.delete_broadcast(broadcast_id)
            await callback.message.answer(f"🗑 Рассылка **{b['name']}** удалена")
            await show_all_broadcasts(callback.message)
    
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
                f"📝 **Создание рассылки для:** {group['name']}\n\n"
                f"Введите **название** рассылки:",
                parse_mode="Markdown"
            )

# ==================== МЕНЮ ПОКАЗА ====================
async def show_groups_menu(message: types.Message):
    groups = db.get_all_target_groups()
    
    text = "📢 **Группы для рассылок**\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    if not groups:
        text += "Нет добавленных групп.\n\n"
        text += "**Как добавить:**\n"
        text += "1. Добавь бота в группу\n"
        text += "2. Сделай бота администратором\n"
        text += "3. Отправь в группе /start\n"
    else:
        for g in groups:
            status = "✅" if g['is_active'] else "⛔"
            broadcasts_count = len(db.get_all_broadcasts(g['id']))
            text += f"{status} **{g['name']}**\n"
            text += f"   🆔 `{g['chat_id']}`\n"
            text += f"   📋 {broadcasts_count} рассылок\n\n"
            
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"{status} {g['name'][:15]}", 
                    callback_data=f"group_broadcasts_{g['id']}"
                ),
                InlineKeyboardButton(text="✏️", callback_data=f"group_rename_{g['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"group_delete_{g['id']}")
            ])
    
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="➕ Добавить группу", callback_data="group_add")])
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin")])
    
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)

async def show_group_broadcasts(message: types.Message, group_id: int):
    group = db.get_target_group(group_id)
    if not group:
        await message.answer("❌ Группа не найдена")
        return
    
    broadcasts = db.get_all_broadcasts(group_id)
    
    if not broadcasts:
        await message.answer(
            f"📭 **{group['name']}** - нет рассылок\n\n"
            f"Используйте кнопку ниже для создания",
            parse_mode="Markdown"
        )
    else:
        text = f"📋 **Рассылки:** {group['name']}\n\n"
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
            text += f"{status} **{b['name']}** - {schedule}\n"
        
        await message.answer(text, parse_mode="Markdown")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать рассылку", callback_data=f"select_group_{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад к группам", callback_data="admin_groups")]
    ])
    await message.answer("Выберите действие:", reply_markup=keyboard)

async def show_all_broadcasts(message: types.Message):
    broadcasts = db.get_all_broadcasts()
    
    if not broadcasts:
        await message.answer("📭 **Нет созданных рассылок**\n\nИспользуйте кнопку 'Создать рассылку'", parse_mode="Markdown")
        return
    
    text = "📋 **Все рассылки**\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    for b in broadcasts:
        group = db.get_target_group(b['group_id'])
        group_name = group['name'] if group else "❓ Неизвестно"
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
        
        text += f"{status} **{b['name']}**\n"
        text += f"   📬 {group_name}\n"
        text += f"   ⏰ {schedule}\n\n"
        
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text=f"{status} {b['name'][:20]}", callback_data=f"broadcast_toggle_{b['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"broadcast_delete_{b['id']}")
        ])
    
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin")])
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)

async def toggle_all_broadcasts(message: types.Message):
    broadcasts = db.get_all_broadcasts()
    active = [b for b in broadcasts if b['is_active']]
    
    if active:
        for b in broadcasts:
            if b['is_active']:
                db.update_broadcast(b['id'], is_active=False)
                if scheduler.get_job(f"broadcast_{b['id']}"):
                    scheduler.remove_job(f"broadcast_{b['id']}")
        await message.answer("⛔ **Все рассылки отключены**")
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
        await message.answer("✅ **Все рассылки включены**")

async def start_create_broadcast(message: types.Message):
    groups = db.get_all_target_groups()
    
    if not groups:
        await message.answer(
            "❌ **Нет доступных групп!**\n\n"
            "Сначала добавьте группы для рассылок:\n"
            "1. Добавьте бота в группу\n"
            "2. Сделайте бота администратором\n"
            "3. Отправьте в группе команду /start",
            parse_mode="Markdown"
        )
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for g in groups:
        if g['is_active']:
                        keyboard.inline_keyboard.append([
                InlineKeyboardButton(text=f"📢 {g['name']}", callback_data=f"select_group_{g['id']}")
            ])
    
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin")])
    
    await message.answer(
        "📝 **Выберите группу для рассылки:**",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# ==================== СОЗДАНИЕ РАССЫЛКИ (ПОШАГОВО) ====================
@dp.message()
async def handle_input(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    if message.from_user.id not in admin_states:
        return
    
    state = admin_states[message.from_user.id]
    step = state.get("step")
    
    # Шаг 1: Название
    if step == "create_name":
        state["name"] = message.text
        state["step"] = "create_type"
        await message.answer(
            "📝 **Тип контента**\n\n"
            "Отправьте:\n"
            "`текст` - текстовая рассылка\n"
            "`фото` - рассылка с фото",
            parse_mode="Markdown"
        )
    
    # Шаг 2: Тип контента
    elif step == "create_type":
        if message.text.lower() in ["текст", "text"]:
            state["content_type"] = "text"
            state["step"] = "create_text"
            await message.answer("📝 Отправьте **текст** рассылки:", parse_mode="Markdown")
        elif message.text.lower() in ["фото", "photo"]:
            state["content_type"] = "photo"
            state["step"] = "create_photo"
            await message.answer("🖼 Отправьте **фото** (можно с подписью):", parse_mode="Markdown")
        else:
            await message.answer("❌ Отправьте 'текст' или 'фото'")
    
    # Шаг 3a: Текст
    elif step == "create_text":
        state["text"] = message.text
        state["step"] = "create_schedule_type"
        await message.answer(
            "⏰ **Тип расписания**\n\n"
            "Отправьте:\n"
            "`1` - В определённое время (ежедневно)\n"
            "`2` - Каждый час / с интервалом",
            parse_mode="Markdown"
        )
    
    # Шаг 3b: Фото
    elif step == "create_photo":
        if message.photo:
            state["photo_file_id"] = message.photo[-1].file_id
            state["text"] = message.caption or ""
            state["step"] = "create_schedule_type"
            await message.answer(
                "⏰ **Тип расписания**\n\n"
                "Отправьте:\n"
                "`1` - В определённое время\n"
                "`2` - Каждый час / с интервалом",
                parse_mode="Markdown"
            )
        else:
            await message.answer("❌ Отправьте фото")
    
    # Шаг 4: Выбор типа расписания
    elif step == "create_schedule_type":
        if message.text == "1":
            state["schedule_type"] = "fixed"
            state["step"] = "create_fixed_time"
            await message.answer(
                f"⏰ Введите **время** в формате `HH:MM`\n\n"
                f"🕐 Часовой пояс: `{TIMEZONE}`\n"
                f"Пример: `09:30` или `18:00`",
                parse_mode="Markdown"
            )
        elif message.text == "2":
            state["schedule_type"] = "interval"
            state["step"] = "create_interval"
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
    elif step == "create_fixed_time":
        try:
            hour, minute = map(int, message.text.split(':'))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                state["hour"] = hour
                state["minute"] = minute
                
                # Сохраняем рассылку
                broadcast_id = db.add_broadcast(
                    group_id=state["group_id"],
                    name=state["name"],
                    content_type=state["content_type"],
                    schedule_type="fixed",
                    text=state.get("text"),
                    photo_file_id=state.get("photo_file_id"),
                    hour=hour,
                    minute=minute
                )
                
                # Добавляем в планировщик
                trigger = CronTrigger(hour=hour, minute=minute, timezone=TIMEZONE)
                scheduler.add_job(send_broadcast, trigger, args=[broadcast_id], id=f"broadcast_{broadcast_id}")
                
                group = db.get_target_group(state["group_id"])
                
                await message.answer(
                    f"✅ **Рассылка создана!**\n\n"
                    f"📢 Название: **{state['name']}**\n"
                    f"📬 Группа: {group['name']}\n"
                    f"⏰ Время: {hour:02d}:{minute:02d} ({TIMEZONE})\n\n"
                    f"Используйте /admin для управления",
                    parse_mode="Markdown"
                )
                
                await send_to_admin(
                    f"➕ **Новая рассылка**\n"
                    f"📢 {state['name']}\n"
                    f"📬 {group['name']}\n"
                    f"⏰ {hour:02d}:{minute:02d}"
                )
                
                del admin_states[message.from_user.id]
            else:
                raise ValueError
        except:
            await message.answer("❌ Неверный формат. Пример: 09:30")
    
    # Шаг 5b: Интервал
    elif step == "create_interval":
        try:
            interval = int(message.text)
            if interval <= 0:
                raise ValueError
            
            state["interval_minutes"] = interval
            
            # Сохраняем рассылку
            broadcast_id = db.add_broadcast(
                group_id=state["group_id"],
                name=state["name"],
                content_type=state["content_type"],
                schedule_type="interval",
                text=state.get("text"),
                photo_file_id=state.get("photo_file_id"),
                interval_minutes=interval
            )
            
            # Добавляем в планировщик
            trigger = IntervalTrigger(minutes=interval)
            scheduler.add_job(send_broadcast, trigger, args=[broadcast_id], id=f"broadcast_{broadcast_id}")
            
            group = db.get_target_group(state["group_id"])
            
            hours = interval // 60
            minutes = interval % 60
            if hours > 0:
                schedule_text = f"каждые {hours}ч {minutes}мин" if minutes > 0 else f"каждые {hours}ч"
            else:
                schedule_text = f"каждые {minutes}мин"
            
            await message.answer(
                f"✅ **Рассылка создана!**\n\n"
                f"📢 Название: **{state['name']}**\n"
                f"📬 Группа: {group['name']}\n"
                f"⏰ Расписание: {schedule_text}\n\n"
                f"Используйте /admin для управления",
                parse_mode="Markdown"
            )
            
            await send_to_admin(
                f"➕ **Новая рассылка**\n"
                f"📢 {state['name']}\n"
                f"📬 {group['name']}\n"
                f"⏰ {schedule_text}"
            )
            
            del admin_states[message.from_user.id]
        except:
            await message.answer("❌ Введите положительное число (минуты)")
    
    # Переименование группы
    elif step == "rename_group":
        group_id = state["group_id"]
        db.update_target_group_name(group_id, message.text)
        await message.answer(f"✅ Группа переименована в: {message.text}")
        del admin_states[message.from_user.id]
        await show_groups_menu(message)
    
    # Добавление группы по ID
    elif step == "add_group_chat_id":
        chat_id = message.text.strip()
        try:
            # Проверяем, что ID корректный
            int(chat_id)
            existing = db.get_target_group_by_chat_id(chat_id)
            if existing:
                await message.answer(f"❌ Группа с ID {chat_id} уже добавлена!")
            else:
                db.add_target_group(chat_id, f"Группа {chat_id}", "manual")
                await message.answer(f"✅ Группа с ID `{chat_id}` добавлена!\n\nТеперь можно создавать рассылки.", parse_mode="Markdown")
                await send_to_admin(f"➕ **Группа добавлена вручную**\nID: `{chat_id}`")
            del admin_states[message.from_user.id]
            await show_groups_menu(message)
        except:
            await message.answer("❌ Неверный формат ID. ID должен быть числом.\nПример: `-1001234567890`")

# ==================== ЗАПУСК БОТА ====================
async def main():
    logger.info("🚀 Бот запускается...")
    logger.info(f"📅 Часовой пояс: {TIMEZONE}")
    logger.info(f"👑 Админ ID: {ADMIN_ID}")
    logger.info(f"📢 Админская группа: {ADMIN_GROUP_ID if ADMIN_GROUP_ID else 'Не настроена'}")
    
    await load_all_broadcasts()
    scheduler.start()
    
    logger.info("✅ Бот готов к работе!")
    
    # Уведомление в админскую группу
    if ADMIN_GROUP_ID:
        await send_to_admin("✅ **Бот перезапущен и готов к работе!**")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
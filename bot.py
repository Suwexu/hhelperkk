import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import BOT_TOKEN, ADMIN_ID, TIMEZONE
from database import db

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Временные хранилища для состояний админа
admin_states = {}

# === ДЕКОРАТОР ДЛЯ ПРОВЕРКИ АДМИНА ===
def admin_only(func):
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id != ADMIN_ID:
            await message.answer("⛔ У вас нет доступа к этой команде.")
            logger.warning(f"Неавторизованная попытка от {message.from_user.id}")
            return
        return await func(message, *args, **kwargs)
    return wrapper

# === ФУНКЦИЯ ОТПРАВКИ РАССЫЛКИ ===
async def send_broadcast(broadcast_id: int):
    logger.info(f"Запуск рассылки #{broadcast_id}")
    
    broadcast = db.get_broadcast(broadcast_id)
    if not broadcast or not broadcast['is_active']:
        return
    
    users = db.get_active_users()
    if not users:
        logger.info("Нет активных подписчиков")
        return
    
    success_count = 0
    for user_id in users:
        try:
            if broadcast['content_type'] == "text" and broadcast['text']:
                await bot.send_message(user_id, broadcast['text'])
                success_count += 1
            elif broadcast['content_type'] == "photo" and broadcast['photo_file_id']:
                await bot.send_photo(user_id, broadcast['photo_file_id'], 
                                     caption=broadcast['text'] or "")
                success_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Ошибка {user_id}: {e}")
            if "bot was blocked" in str(e).lower():
                db.remove_user(user_id)
    
    db.update_last_sent(broadcast_id)
    db.log_broadcast(broadcast_id, success_count)
    logger.info(f"Рассылка #{broadcast_id}: {success_count}/{len(users)}")

# === ЗАГРУЗКА РАССЫЛОК ===
async def load_all_broadcasts():
    broadcasts = db.get_all_broadcasts()
    for broadcast in broadcasts:
        if broadcast['is_active']:
            await add_broadcast_to_scheduler(broadcast)
    logger.info(f"Загружено {len([b for b in broadcasts if b['is_active']])} активных рассылок")

async def add_broadcast_to_scheduler(broadcast: dict):
    broadcast_id = broadcast['id']
    schedule_type = broadcast['schedule_type']
    job_id = f"broadcast_{broadcast_id}"
    
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    
    if schedule_type == 'interval':
        interval_minutes = broadcast['interval_minutes']
        if interval_minutes:
            trigger = IntervalTrigger(minutes=interval_minutes)
        else:
            return
    elif schedule_type == 'fixed':
        hour = broadcast['hour']
        minute = broadcast['minute']
        days = broadcast['days']
        
        if days:
            day_map = {'mon': 'mon', 'tue': 'tue', 'wed': 'wed', 'thu': 'thu', 
                       'fri': 'fri', 'sat': 'sat', 'sun': 'sun'}
            trigger = CronTrigger(
                hour=hour, 
                minute=minute, 
                day_of_week=','.join([day_map[d] for d in days])
            )
        else:
            trigger = CronTrigger(hour=hour, minute=minute)
    else:
        return
    
    scheduler.add_job(send_broadcast, trigger, args=[broadcast_id], id=job_id, replace_existing=True)

# === КОМАНДЫ ПОЛЬЗОВАТЕЛЕЙ ===
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    db.add_user(user.id, user.username, user.first_name, user.last_name)
    await message.answer("✅ Вы подписались на рассылку!\n\n/stop - отписаться\n/info - информация")

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    db.remove_user(message.from_user.id)
    await message.answer("❌ Вы отписались от рассылки.")

@dp.message(Command("info"))
async def cmd_info(message: Message):
    is_active = message.from_user.id in db.get_active_users()
    if is_active:
        await message.answer("📊 Вы активный подписчик.")
    else:
        await message.answer("📊 Вы не подписаны. Нажмите /start")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"🆔 Ваш ID: `{message.from_user.id}`", parse_mode="Markdown")

# === АДМИН КОМАНДЫ ===
@dp.message(Command("admin"))
@admin_only
async def admin_menu(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать рассылку", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Список рассылок", callback_data="admin_list")],
        [InlineKeyboardButton(text="⏸ Все рассылки", callback_data="admin_all_toggle")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👥 Подписчики", callback_data="admin_users")]
    ])
    await message.answer("🔧 **Панель администратора**", reply_markup=keyboard, parse_mode="Markdown")

# === ОБРАБОТЧИК CALLBACK ===
@dp.callback_query()
async def handle_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    data = callback.data
    
    if data == "admin_create":
        admin_states[ADMIN_ID] = {"step": "create_name"}
        await callback.message.answer("📝 Введите **название** рассылки:", parse_mode="Markdown")
    
    elif data == "admin_list":
        await show_broadcasts_list(callback.message)
    
    elif data == "admin_stats":
        await admin_stats_command(callback.message)
    
    elif data == "admin_users":
        await admin_users_command(callback.message)
    
    elif data == "admin_all_toggle":
        await toggle_all_broadcasts(callback.message)
    
    elif data.startswith("broadcast_edit_"):
        broadcast_id = int(data.split("_")[2])
        await show_broadcast_actions(callback.message, broadcast_id)
    
    elif data.startswith("broadcast_toggle_"):
        broadcast_id = int(data.split("_")[2])
        broadcast = db.get_broadcast(broadcast_id)
        if broadcast:
            new_status = not broadcast['is_active']
            db.update_broadcast(broadcast_id, is_active=new_status)
            if new_status:
                await add_broadcast_to_scheduler(db.get_broadcast(broadcast_id))
            else:
                job_id = f"broadcast_{broadcast_id}"
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
            await callback.answer(f"Рассылка {'включена ✅' if new_status else 'отключена ⛔'}")
            await show_broadcasts_list(callback.message)
    
    elif data.startswith("broadcast_delete_"):
        broadcast_id = int(data.split("_")[2])
        job_id = f"broadcast_{broadcast_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        db.delete_broadcast(broadcast_id)
        await callback.answer("🗑 Рассылка удалена")
        await show_broadcasts_list(callback.message)
    
    await callback.answer()

async def toggle_all_broadcasts(message: types.Message):
    broadcasts = db.get_all_broadcasts()
    active_broadcasts = [b for b in broadcasts if b['is_active']]
    
    if active_broadcasts:
        for b in broadcasts:
            if b['is_active']:
                db.update_broadcast(b['id'], is_active=False)
                job_id = f"broadcast_{b['id']}"
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
        await message.answer("⛔ **Все рассылки отключены**", parse_mode="Markdown")
    else:
        for b in broadcasts:
            db.update_broadcast(b['id'], is_active=True)
            await add_broadcast_to_scheduler(db.get_broadcast(b['id']))
        await message.answer("✅ **Все рассылки включены**", parse_mode="Markdown")

async def show_broadcasts_list(message: types.Message):
    broadcasts = db.get_all_broadcasts()
    
    if not broadcasts:
        await message.answer("📭 **Нет созданных рассылок**\n\nИспользуйте /admin", parse_mode="Markdown")
        return
    
    text = "📋 **Список рассылок**\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    for b in broadcasts:
        status = "✅" if b['is_active'] else "⛔"
        
        if b['schedule_type'] == 'fixed':
            days_str = ", ".join(b['days']) if b['days'] else "ежедневно"
            schedule_str = f"{b['hour']:02d}:{b['minute']:02d} ({days_str})"
        elif b['schedule_type'] == 'interval':
            mins = b['interval_minutes']
            hours = mins // 60
            minutes = mins % 60
            if hours > 0:
                schedule_str = f"каждые {hours}ч {minutes}мин" if minutes > 0 else f"каждые {hours}ч"
            else:
                schedule_str = f"каждые {minutes}мин"
        else:
            schedule_str = "неизвестно"
        
        text += f"{status} **{b['name']}**\n"
        text += f"   ⏰ {schedule_str}\n"
        text += f"   📝 ID: {b['id']}\n\n"
        
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text=f"{status} {b['name'][:25]}", callback_data=f"broadcast_edit_{b['id']}")
        ])
    
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin")])
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)

async def show_broadcast_actions(message: types.Message, broadcast_id: int):
    broadcast = db.get_broadcast(broadcast_id)
    if not broadcast:
        await message.answer("❌ Рассылка не найдена")
        return
    
    status_text = "Включена ✅" if broadcast['is_active'] else "Отключена ⛔"
    
    if broadcast['schedule_type'] == 'fixed':
        days_str = ", ".join(broadcast['days']) if broadcast['days'] else "ежедневно"
        schedule_text = f"{broadcast['hour']:02d}:{broadcast['minute']:02d} ({days_str})"
    elif broadcast['schedule_type'] == 'interval':
        mins = broadcast['interval_minutes']
        hours = mins // 60
        minutes = mins % 60
        if hours > 0:
            schedule_text = f"каждые {hours}ч {minutes}мин" if minutes > 0 else f"каждые {hours}ч"
        else:
            schedule_text = f"каждые {minutes}мин"
    else:
        schedule_text = "неизвестно"
    
    text = f"📢 **{broadcast['name']}**\n\n"
    text += f"🕐 Расписание: {schedule_text}\n"
    text += f"🔘 Статус: {status_text}\n"
    text += f"📝 Тип: {'📷 Фото' if broadcast['content_type'] == 'photo' else '📝 Текст'}\n"
    
    if broadcast['last_sent_at']:
        text += f"📨 Последняя отправка: {broadcast['last_sent_at']}\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Вкл/Выкл", callback_data=f"broadcast_toggle_{broadcast_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"broadcast_delete_{broadcast_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_list")]
    ])
    
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)

@dp.message(Command("stats"))
@admin_only
async def admin_stats_command(message: Message):
    users_count = db.get_user_count()
    broadcasts = db.get_all_broadcasts()
    active_count = len([b for b in broadcasts if b['is_active']])
    
    await message.answer(
        f"📊 **Статистика**\n\n"
        f"👥 Подписчиков: {users_count}\n"
        f"📢 Всего рассылок: {len(broadcasts)}\n"
        f"✅ Активных: {active_count}",
        parse_mode="Markdown"
    )

@dp.message(Command("users"))
@admin_only
async def admin_users_command(message: Message):
    users = db.get_all_users()
    if not users:
        await message.answer("📭 Нет подписчиков")
        return
    
    text = "📋 **Подписчики:**\n\n"
    for user in users[:30]:
        name = user[2] or user[1] or "Аноним"
        text += f"• {name} - ID: `{user[0]}`\n"
    
    if len(users) > 30:
        text += f"\n...и ещё {len(users) - 30}"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("cancel"))
@admin_only
async def admin_cancel(message: Message):
    if ADMIN_ID in admin_states:
        del admin_states[ADMIN_ID]
        await message.answer("❌ Действие отменено")
    else:
        await message.answer("Нет активных действий")

# === ОСНОВНАЯ ЛОГИКА СОЗДАНИЯ РАССЫЛОК ===
async def save_broadcast(message: Message, state: dict):
    """Сохраняет рассылку в БД"""
    broadcast_id = db.add_broadcast(
        name=state["name"],
        content_type=state["content_type"],
        schedule_type=state["schedule_type"],
        hour=state.get("hour"),
        minute=state.get("minute"),
        interval_minutes=state.get("interval_minutes"),
        text=state.get("text"),
        photo_file_id=state.get("photo_file_id"),
        days=state.get("days")
    )
    
    broadcast = db.get_broadcast(broadcast_id)
    await add_broadcast_to_scheduler(broadcast)
    
    if state["schedule_type"] == 'fixed':
        days_str = ", ".join(state["days"]) if state.get("days") else "ежедневно"
        schedule_info = f"{state['hour']:02d}:{state['minute']:02d} ({days_str})"
    else:
        mins = state["interval_minutes"]
        hours = mins // 60
        minutes = mins % 60
        if hours > 0:
            schedule_info = f"каждые {hours}ч {minutes}мин" if minutes > 0 else f"каждые {hours}ч"
        else:
            schedule_info = f"каждые {minutes}мин"
    
    await message.answer(
        f"✅ **Рассылка создана!**\n\n"
        f"📢 {state['name']}\n"
        f"⏰ {schedule_info}\n\n"
        f"Используйте /admin для управления",
        parse_mode="Markdown"
    )
    
    del admin_states[ADMIN_ID]

@dp.message()
async def handle_admin_input(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    if ADMIN_ID not in admin_states:
        return
    
    state = admin_states[ADMIN_ID]
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
        state["step"] = "choose_schedule"
        await message.answer(
            "⏰ **Тип расписания**\n\n"
            "Отправьте:\n"
            "`1` - В определённое время (например, 09:30)\n"
            "`2` - Каждый час / с интервалом",
            parse_mode="Markdown"
        )
    
    # Шаг 3b: Фото
    elif step == "create_photo":
        if message.photo:
            state["photo_file_id"] = message.photo[-1].file_id
            state["text"] = message.caption or ""
            state["step"] = "choose_schedule"
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
    elif step == "choose_schedule":
        if message.text == "1":
            state["schedule_type"] = "fixed"
            state["step"] = "fixed_time"
            await message.answer("⏰ Введите время в формате `HH:MM` (например, 09:30):", parse_mode="Markdown")
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
    
    # Шаг 5: Фиксированное время
    elif step == "fixed_time":
        try:
            hour, minute = map(int, message.text.split(':'))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                state["hour"] = hour
                state["minute"] = minute
                state["step"] = "fixed_days"
                await message.answer(
                    "📅 **Дни недели**\n\n"
                    "Отправьте номера через пробел:\n"
                    "`1 2 3 4 5` - будни\n"
                    "`6 7` - выходные\n"
                    "`1 3 5` - ПН, СР, ПТ\n\n"
                    "Или отправьте `ежедневно`",
                    parse_mode="Markdown"
                )
            else:
                raise ValueError
        except:
            await message.answer("❌ Неверный формат. Пример: 09:30")
    
    # Шаг 6: Дни для fixed
    elif step == "fixed_days":
        days_map = {'1': 'mon', '2': 'tue', '3': 'wed', '4': 'thu', 
                    '5': 'fri', '6': 'sat', '7': 'sun'}
        
        if message.text.lower() == "ежедневно":
            state["days"] = None
        else:
            day_numbers = message.text.split()
            days = [days_map[d] for d in day_numbers if d in days_map]
            if not days:
                await message.answer("❌ Не выбрано ни одного дня. Попробуйте снова:")
                return
            state["days"] = days
        
        await save_broadcast(message, state)
    
    # Шаг 5b: Интервал
    elif step == "interval_minutes":
        try:
            interval_minutes = int(message.text)
            if interval_minutes <= 0:
                raise ValueError
            state["interval_minutes"] = interval_minutes
            await save_broadcast(message, state)
        except:
            await message.answer("❌ Введите положительное число (минуты)")

# === ЗАПУСК ===
async def on_startup():
    logger.info("Бот запускается...")
    await load_all_broadcasts()
    logger.info(f"Бот готов! Админ ID: {ADMIN_ID}")

async def main():
    await on_startup()
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
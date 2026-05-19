import asyncio
import logging
from datetime import datetime, timedelta
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
    """Декоратор для проверки, что команду выполняет только админ"""
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id != ADMIN_ID:
            await message.answer("⛔ У вас нет доступа к этой команде. Только администратор бота может управлять рассылками.")
            logger.warning(f"Неавторизованная попытка доступа от пользователя {message.from_user.id}")
            return
        return await func(message, *args, **kwargs)
    return wrapper

# === ФУНКЦИЯ ОТПРАВКИ ОДНОЙ РАССЫЛКИ ===
async def send_broadcast(broadcast_id: int):
    """Отправить конкретную рассылку всем пользователям"""
    logger.info(f"Запуск рассылки #{broadcast_id}")
    
    broadcast = db.get_broadcast(broadcast_id)
    if not broadcast:
        logger.warning(f"Рассылка #{broadcast_id} не найдена")
        return
    
    if not broadcast['is_active']:
        logger.info(f"Рассылка #{broadcast_id} отключена, пропускаем")
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
            logger.error(f"Ошибка отправки пользователю {user_id} из рассылки #{broadcast_id}: {e}")
            if "bot was blocked" in str(e).lower():
                db.remove_user(user_id)
                logger.info(f"Пользователь {user_id} деактивирован")
    
    # Логируем результат
    db.update_last_sent(broadcast_id)
    db.log_broadcast(broadcast_id, success_count)
    logger.info(f"Рассылка #{broadcast_id} завершена. Отправлено: {success_count}/{len(users)}")

# === ЗАГРУЗКА ВСЕХ РАССЫЛОК ПРИ СТАРТЕ ===
async def load_all_broadcasts():
    """Загружает все активные рассылки из БД в планировщик"""
    broadcasts = db.get_all_broadcasts()
    
    for broadcast in broadcasts:
        if broadcast['is_active']:
            await add_broadcast_to_scheduler(broadcast)
    
    logger.info(f"Загружено {len([b for b in broadcasts if b['is_active']])} активных рассылок")

async def add_broadcast_to_scheduler(broadcast: dict):
    """Добавить рассылку в планировщик с учётом типа расписания"""
    broadcast_id = broadcast['id']
    schedule_type = broadcast['schedule_type']
    job_id = f"broadcast_{broadcast_id}"
    
    # Удаляем старую задачу, если есть
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    
    # Создаём триггер в зависимости от типа расписания
    if schedule_type == 'interval':
        # Интервальная рассылка (каждый час, каждые 30 минут и т.д.)
        interval_minutes = broadcast['interval_minutes']
        if interval_minutes:
            trigger = IntervalTrigger(minutes=interval_minutes)
            logger.info(f"Рассылка #{broadcast_id} (интервальная): каждые {interval_minutes} минут")
        else:
            logger.warning(f"Рассылка #{broadcast_id}: не указан интервал")
            return
    
    elif schedule_type == 'fixed':
        # Фиксированное время (ежедневно или по дням недели)
        hour = broadcast['hour']
        minute = broadcast['minute']
        days = broadcast['days']
        
        if days:
            # Определённые дни недели
            day_map = {'mon': 'mon', 'tue': 'tue', 'wed': 'wed', 'thu': 'thu', 
                       'fri': 'fri', 'sat': 'sat', 'sun': 'sun'}
            trigger = CronTrigger(
                hour=hour, 
                minute=minute, 
                day_of_week=','.join([day_map[d] for d in days])
            )
        else:
            # Ежедневно
            trigger = CronTrigger(hour=hour, minute=minute)
        
        logger.info(f"Рассылка #{broadcast_id} (фиксированная): {hour:02d}:{minute:02d}")
    
    elif schedule_type == 'cron':
        # Продвинутое cron-выражение (для сложных сценариев)
        cron_string = broadcast['cron_string']
        if cron_string:
            parts = cron_string.split()
            if len(parts) == 5:
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4]
                )
            else:
                logger.warning(f"Рассылка #{broadcast_id}: неверный cron формат")
                return
        else:
            return
    else:
        logger.warning(f"Рассылка #{broadcast_id}: неизвестный тип расписания {schedule_type}")
        return
    
    scheduler.add_job(
        send_broadcast,
        trigger,
        args=[broadcast_id],
        id=job_id,
        replace_existing=True
    )

# === КОМАНДЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ ===
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    db.add_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )
    await message.answer(
        "✅ Вы подписались на рассылку!\n\n"
        "Администратор может настроить рассылки:\n"
        "• в определённое время\n"
        "• каждый час\n"
        "• с любым интервалом\n\n"
        "Команды:\n"
        "/start - подписаться\n"
        "/stop - отписаться\n"
        "/info - информация о подписке"
    )

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    db.remove_user(message.from_user.id)
    await message.answer("❌ Вы отписались от рассылки. Чтобы подписаться снова, нажмите /start")

@dp.message(Command("info"))
async def cmd_info(message: Message):
    is_active = message.from_user.id in db.get_active_users()
    if is_active:
        await message.answer("📊 Вы активный подписчик рассылки.")
    else:
        await message.answer("📊 Вы не подписаны на рассылку. Нажмите /start чтобы подписаться.")
    
    broadcasts = db.get_all_broadcasts()
    active_broadcasts = [b for b in broadcasts if b['is_active']]
    
    if active_broadcasts:
        text = "\n\n📅 **Активные рассылки:**\n"
        for b in active_broadcasts:
            if b['schedule_type'] == 'fixed':
                days_str = ", ".join(b['days']) if b['days'] else "ежедневно"
                text += f"• {b['name']}: {b['hour']:02d}:{b['minute']:02d} ({days_str})\n"
            elif b['schedule_type'] == 'interval':
                text += f"• {b['name']}: каждые {b['interval_minutes']} минут\n"
        await message.answer(text, parse_mode="Markdown")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"🆔 Ваш ID: `{message.from_user.id}`", parse_mode="Markdown")

# === КОМАНДЫ ДЛЯ АДМИНА ===
@dp.message(Command("admin"))
@admin_only
async def admin_menu(message: Message):
    """Главное меню администратора"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать рассылку", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 Список рассылок", callback_data="admin_list")],
        [InlineKeyboardButton(text="⏸ Все рассылки", callback_data="admin_all_toggle")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👥 Подписчики", callback_data="admin_users")]
    ])
    
    users_count = db.get_user_count()
    broadcasts = db.get_all_broadcasts()
    active_count = len([b for b in broadcasts if b['is_active']])
    
    menu_text = (
        f"🔧 **Панель администратора**\n\n"
        f"👥 Подписчиков: {users_count}\n"
        f"📢 Всего рассылок: {len(broadcasts)}\n"
        f"✅ Активных: {active_count}\n\n"
        f"Выберите действие:"
    )
    
    await message.answer(menu_text, reply_markup=keyboard, parse_mode="Markdown")

# === ОБРАБОТКА CALLBACK'ОВ ===
@dp.callback_query()
async def handle_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ У вас нет доступа к управлению ботом!", show_alert=True)
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
            updated_broadcast = db.get_broadcast(broadcast_id)
            if new_status:
                await add_broadcast_to_scheduler(updated_broadcast)
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
    """Включить/отключить все рассылки"""
    broadcasts = db.get_all_broadcasts()
    active_broadcasts = [b for b in broadcasts if b['is_active']]
    
    if active_broadcasts:
        for b in broadcasts:
            if b['is_active']:
                db.update_broadcast(b['id'], is_active=False)
                job_id = f"broadcast_{b['id']}"
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
        await message.answer("⛔ **Все рассылки отключены**")
    else:
        for b in broadcasts:
            db.update_broadcast(b['id'], is_active=True)
            await add_broadcast_to_scheduler(db.get_broadcast(b['id']))
        await message.answer("✅ **Все рассылки включены**")

async def show_broadcasts_list(message: types.Message):
    """Показать список всех рассылок"""
    broadcasts = db.get_all_broadcasts()
    
    if not broadcasts:
        await message.answer("📭 **Нет созданных рассылок**\n\nИспользуйте /admin для создания.", 
                            parse_mode="Markdown")
        return
    
    text = "📋 **Список рассылок**\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    for b in broadcasts:
        status = "✅" if b['is_active'] else "⛔"
        
        if b['schedule_type'] == 'fixed':
            days_str = ", ".join(b['days']) if b['days'] else "ежедневно"
            schedule_str = f"{b['hour']:02d}:{b['minute']:02d} ({days_str})"
        elif b['schedule_type'] == 'interval':
            hours = b['interval_minutes'] // 60
            mins = b['interval_minutes'] % 60
            if hours > 0:
                schedule_str = f"каждые {hours}ч {mins}мин" if mins > 0 else f"каждые {hours}ч"
            else:
                schedule_str = f"каждые {mins}мин"
        else:
            schedule_str = "неизвестно"
        
        text += f"{status} **{b['name']}**\n"
        text += f"   ⏰ {schedule_str}\n"
        text += f"   📝 Тип: {'📷' if b['content_type'] == 'photo' else '📝'} | ID: {b['id']}\n\n"
        
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"{status} {b['name'][:25]}", 
                callback_data=f"broadcast_edit_{b['id']}"
            )
        ])
    
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад в меню", callback_data="admin")])
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)

async def show_broadcast_actions(message: types.Message, broadcast_id: int):
    """Показать действия с конкретной рассылкой"""
    broadcast = db.get_broadcast(broadcast_id)
    if not broadcast:
        await message.answer("❌ Рассылка не найдена")
        return
    
    status_text = "Включена ✅" if broadcast['is_active'] else "Отключена ⛔"
    
    if broadcast['schedule_type'] == 'fixed':
        days_str = ", ".join(broadcast['days']) if broadcast['days'] else "ежедневно"
        schedule_text = f"{broadcast['hour']:02d}:{broadcast['minute']:02d} ({days_str})"
    elif broadcast['schedule_type'] == 'interval':
        hours = broadcast['interval_minutes'] // 60
        mins = broadcast['interval_minutes'] % 60
        if hours > 0:
            schedule_text = f"каждые {hours}ч {mins}мин" if mins > 0 else f"каждые {hours}ч"
        else:
            schedule_text = f"каждые {mins}мин"
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
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="admin_list")]
    ])
    
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)

# === АДМИНСКИЕ КОМАНДЫ ===
@dp.message(Command("stats"))
@admin_only
async def admin_stats_command(message: Message):
    users_count = db.get_user_count()
    broadcasts = db.get_all_broadcasts()
    active_broadcasts = len([b for b in broadcasts if b['is_active']])
    
    stats_text = f"📊 **Статистика бота**\n\n"
    stats_text += f"👥 Подписчиков: {users_count}\n"
    stats_text += f"📢 Всего рассылок: {len(broadcasts)}\n"
    stats_text += f"✅ Активных рассылок: {active_broadcasts}\n"
    
    await message.answer(stats_text, parse_mode="Markdown")

@dp.message(Command("users"))
@admin_only
async def admin_users_command(message: Message):
    users = db.get_all_users()
    if not users:
        await message.answer("📭 Нет активных подписчиков")
        return
    
    text = "📋 **Активные подписчики:**\n\n"
    for user in users[:30]:
        name = user[2] or user[1] or "Аноним"
        text += f"• {name} (@{user[1] or 'нет_username'}) - ID: `{user[0]}`\n"
    
    if len(users) > 30:
        text += f"\n...и ещё {len(users) - 30} подписчиков"
    
    await message.answer(text, parse_mode="Markdown")

# === СОЗДАНИЕ НОВОЙ РАССЫЛКИ ===
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
            "📝 **Выберите тип контента**\n\n"
            "Отправьте:\n"
            "`текст` - для текстовой рассылки\n"
            "`фото` - для рассылки с фото",
            parse_mode="Markdown"
        )
    
    # Шаг 2: Тип контента
    elif step == "create_type":
        if message.text.lower() in ["текст", "text"]:
            state["content_type"] = "text"
            state["step"] = "create_text"
            await message.answer("📝 Отправьте **текст** для рассылки:", parse_mode="Markdown")
        elif message.text.lower() in ["фото", "photo"]:
            state["content_type"] = "photo"
            state["step"] = "create_photo"
            await message.answer("🖼 Отправьте **фото** (можно с подписью):", parse_mode="Markdown")
        else:
            await message.answer("❌ Пожалуйста, отправьте 'текст' или 'фото'")
    
    # Шаг 3a: Текст
    elif step == "create_text":
        state["text"] = message.text
        state["step"] = "choose_schedule_type"
        await message.answer(
            "⏰ **Выберите тип расписания**\n\n"
            "Отправьте:\n"
            "`1` - В определённое время (например, 09:30)\n"
            "`2` - Каждый час или с интервалом\n\n"
            "Пример: отправьте `2` для выбора интервала",
            parse_mode="Markdown"
        )
    
    # Шаг 3b: Фото
    elif step == "create_photo":
        if message.photo:
            state["photo_file_id"] = message.photo[-1].file_id
            state["text"] = message.caption or ""
            state["step"] = "choose_schedule_type"
            await message.answer(
                "⏰ **Выберите тип расписания**\n\n"
                "Отправьте:\n"
                "`1` - В определённое время (например, 09:30)\n"
                "`2` - Каждый час или с интервалом",
                parse_mode="Markdown"
            )
        else:
            await message.answer("❌ Пожалуйста, отправьте именно фото")
    
    # Шаг 4: Выбор типа расписания
    elif step == "choose_schedule_type":
        if message.text == "1":
            state["schedule_type"] = "fixed"
            state["step"] = "create_fixed_time"
            await message.answer("⏰ Введите **время** в формате `HH:MM` (например, 09:30 или 18:00):", 
                                parse_mode="Markdown")
        elif message.text == "2":
            state["schedule_type"] = "interval"
            state["step"] = "create_interval"
            await message.answer(
                "⏰ **Выберите интервал**\n\n"
                "Отправьте число в **минутах**:\n"
                "• `60` - каждый час\n"
                "• `30` - каждые 30 минут\n"
                "• `120` - каждые 2 часа\n"
                "• `1440` - каждый день (но для этого лучше fixed)\n\n"
                "Или отправьте любое другое число",
                parse_mode="Markdown"
            )
        else:
            await message.answer("❌ Отправьте `1` или `2`")
    
    # Шаг 5a: Фиксированное время
    elif step == "create_fixed_time":
        try:
            hour, minute = map(int, message.text.split(':'))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
            state["hour"] = hour
            state["minute"] = minute
            state["step"] = "create_days"
            await message.answer(
                "📅 **Выберите дни недели**\n\n"
                "Отправьте номера дней через пробел:\n"
                "`1` - ПН, `2` - ВТ, `3` - СР, `4` - ЧТ, `5` - ПТ, `6` - СБ, `7` - ВС\n\n"
                "Или отправьте `ежедневно`",
                parse_mode="Markdown"
            )
        except:
            await message.answer("❌ Неверный формат. Используйте `HH:MM`, например 09:30", parse_mode="Markdown")
    
    # Шаг 5b: Интервал
    elif step == "create_interval":
        try:
            interval_minutes = int(message.text)
            if interval_minutes <= 0:
                raise ValueError
            state["interval_minutes"] = interval_minutes
            # Интервальные рассылки не требуют выбора дней
            await save_broadcast(message, state)
        except:
            await message.answer("❌ Введите положительное число (минуты)", parse_mode="Markdown
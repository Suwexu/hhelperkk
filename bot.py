import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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

# Временное хранилище для состояний админа
admin_states = {}

# === ФУНКЦИЯ РАССЫЛКИ ===
async def send_daily_newsletter():
    """Отправляет сохранённый контент всем активным пользователям"""
    logger.info("Запуск плановой рассылки")
    
    # Получаем контент из БД
    content = db.get_content()
    if not content:
        logger.warning("Нет сохранённого контента для рассылки")
        return
    
    content_type, text, photo_file_id = content
    users = db.get_active_users()
    
    if not users:
        logger.info("Нет активных подписчиков")
        return
    
    success_count = 0
    for user_id in users:
        try:
            if content_type == "text" and text:
                await bot.send_message(user_id, text)
                success_count += 1
            elif content_type == "photo" and photo_file_id:
                await bot.send_photo(user_id, photo_file_id, caption=text or "")
                success_count += 1
            await asyncio.sleep(0.05)  # Защита от rate limit
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {user_id}: {e}")
            # Если бот заблокирован, можно деактивировать пользователя
            if "bot was blocked" in str(e).lower():
                db.remove_user(user_id)
                logger.info(f"Пользователь {user_id} деактивирован (бот заблокирован)")
    
    # Логируем результат
    db.log_broadcast(success_count)
    logger.info(f"Рассылка завершена. Отправлено: {success_count}/{len(users)}")

# === ЗАГРУЗКА РАСПИСАНИЯ ПРИ СТАРТЕ ===
async def load_schedule_from_db():
    """Загружает расписание из БД при запуске бота"""
    schedule = db.get_schedule()
    if schedule:
        hour, minute = schedule
        scheduler.add_job(
            send_daily_newsletter,
            CronTrigger(hour=hour, minute=minute),
            id="daily_job",
            replace_existing=True
        )
        logger.info(f"Загружено расписание из БД: {hour:02d}:{minute:02d}")
    else:
        logger.info("Расписание не найдено в БД")

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
        "Каждый день в установленное время я буду присылать новости.\n\n"
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
    
    # Показываем время следующей рассылки, если есть
    schedule = db.get_schedule()
    if schedule:
        hour, minute = schedule
        await message.answer(f"⏰ Следующая рассылка запланирована на {hour:02d}:{minute:02d}")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"🆔 Ваш ID: `{message.from_user.id}`", parse_mode="Markdown")

# === КОМАНДЫ ДЛЯ АДМИНА ===
@dp.message(Command("add_text"))
async def admin_add_text(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    admin_states[ADMIN_ID] = "waiting_text"
    await message.answer("📝 Отправь мне текст для будущих рассылок.\n\n(Отправь /cancel чтобы отменить)")

@dp.message(Command("add_photo"))
async def admin_add_photo(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    admin_states[ADMIN_ID] = "waiting_photo"
    await message.answer("🖼 Отправь мне ФОТО (можно с подписью).\n\n(Отправь /cancel чтобы отменить)")

@dp.message(Command("schedule"))
async def admin_schedule(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Формат: `/schedule 09:30` или `/schedule 18:00`", parse_mode="Markdown")
        return
    
    time_str = args[1]
    try:
        hour, minute = map(int, time_str.split(':'))
        
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Неверный диапазон времени")
        
        # Сохраняем в БД
        db.set_schedule(hour, minute)
        
        # Обновляем планировщик
        scheduler.add_job(
            send_daily_newsletter,
            CronTrigger(hour=hour, minute=minute),
            id="daily_job",
            replace_existing=True
        )
        
        await message.answer(f"✅ Рассылка установлена на ежедневно в {time_str}")
        logger.info(f"Админ установил расписание: {time_str}")
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("test"))
async def admin_test(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    await message.answer("🔔 Запускаю тестовую рассылку...")
    await send_daily_newsletter()
    await message.answer("✅ Тестовая рассылка выполнена")

@dp.message(Command("stats"))
async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    users_count = db.get_user_count()
    schedule = db.get_schedule()
    last_broadcast = db.get_last_broadcast_time()
    
    stats_text = f"📊 **Статистика бота**\n\n"
    stats_text += f"👥 Подписчиков: {users_count}\n"
    
    if schedule:
        stats_text += f"⏰ Расписание: {schedule[0]:02d}:{schedule[1]:02d}\n"
    else:
        stats_text += f"⏰ Расписание: не установлено\n"
    
    if last_broadcast:
        stats_text += f"📨 Последняя рассылка: {last_broadcast[0]}\n"
        stats_text += f"👤 Получили: {last_broadcast[1]} чел.\n"
    
    await message.answer(stats_text, parse_mode="Markdown")

@dp.message(Command("users"))
async def admin_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    users = db.get_all_users()
    if not users:
        await message.answer("Нет активных подписчиков")
        return
    
    # Показываем только первых 20, чтобы не спамить
    text = "📋 **Активные подписчики:**\n\n"
    for user in users[:20]:
        text += f"• {user[2] or 'No name'} (@{user[1] or 'no_username'}) - ID: {user[0]}\n"
    
    if len(users) > 20:
        text += f"\n...и ещё {len(users) - 20} подписчиков"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("cancel"))
async def admin_cancel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    if ADMIN_ID in admin_states:
        del admin_states[ADMIN_ID]
        await message.answer("❌ Действие отменено")
    else:
        await message.answer("Нет активных действий для отмены")

# === ОБРАБОТЧИК ВВОДА ОТ АДМИНА ===
@dp.message()
async def handle_admin_input(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    if ADMIN_ID not in admin_states:
        return
    
    state = admin_states[ADMIN_ID]
    
    if state == "waiting_text":
        db.save_content(content_type="text", text=message.text)
        del admin_states[ADMIN_ID]
        await message.answer("✅ Текст сохранён! Теперь установи время командой /schedule HH:MM")
        logger.info("Админ сохранил текстовый контент")
        
    elif state == "waiting_photo":
        if message.photo:
            photo = message.photo[-1]
            db.save_content(
                content_type="photo",
                text=message.caption or "",
                photo_file_id=photo.file_id
            )
            del admin_states[ADMIN_ID]
            await message.answer("✅ Фото сохранено! Теперь установи время /schedule HH:MM")
            logger.info("Админ сохранил фото-контент")
        else:
            await message.answer("❌ Пожалуйста, отправь именно фото (можешь добавить подпись).")

# === ЗАПУСК ===
async def on_startup():
    """Действия при запуске бота"""
    logger.info("Бот запускается...")
    await load_schedule_from_db()
    logger.info(f"Бот готов к работе! Админ ID: {ADMIN_ID}")

async def main():
    await on_startup()
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
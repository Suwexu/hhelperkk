import os
from dotenv import load_dotenv

# Загружаем переменные из файла .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Настройки базы данных
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot_database.db")

# Часовой пояс для рассылки (по умолчанию Москва)
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# Проверка наличия токена
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения!")
if not ADMIN_ID:
    raise ValueError("ADMIN_ID не найден в переменных окружения!")
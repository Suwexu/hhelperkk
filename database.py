import sqlite3
import json
from typing import List, Tuple, Optional
from config import DATABASE_PATH

class Database:
    def __init__(self):
        self.db_path = DATABASE_PATH
        self.init_tables()
    
    def get_connection(self):
        """Получить соединение с БД"""
        return sqlite3.connect(self.db_path)
    
    def init_tables(self):
        """Создание всех необходимых таблиц"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Таблица пользователей
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 1
                )
            """)
            
            # Таблица контента для рассылки
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS broadcast_content (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_type TEXT NOT NULL, -- 'text' или 'photo'
                    text TEXT,
                    photo_file_id TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Таблица расписания
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schedule (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hour INTEGER NOT NULL,
                    minute INTEGER NOT NULL,
                    is_active INTEGER DEFAULT 1
                )
            """)
            
            # Таблица логов рассылок
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS broadcast_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    recipients_count INTEGER,
                    content_id INTEGER
                )
            """)
            
            conn.commit()
    
    # === РАБОТА С ПОЛЬЗОВАТЕЛЯМИ ===
    def add_user(self, user_id: int, username: str = None, first_name: str = None, last_name: str = None):
        """Добавить или обновить пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, is_active)
                VALUES (?, ?, ?, ?, 1)
            """, (user_id, username, first_name, last_name))
            conn.commit()
    
    def remove_user(self, user_id: int):
        """Отписать пользователя (деактивировать)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
            conn.commit()
    
    def get_active_users(self) -> List[int]:
        """Получить список активных пользователей"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users WHERE is_active = 1")
            return [row[0] for row in cursor.fetchall()]
    
    def get_all_users(self) -> List[Tuple]:
        """Получить всех пользователей (с данными)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE is_active = 1 ORDER BY subscribed_at DESC")
            return cursor.fetchall()
    
    def get_user_count(self) -> int:
        """Количество активных подписчиков"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_active = 1")
            return cursor.fetchone()[0]
    
    # === РАБОТА С КОНТЕНТОМ ===
    def save_content(self, content_type: str, text: str = None, photo_file_id: str = None):
        """Сохранить контент для рассылки (заменяет старый)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Удаляем старый контент
            cursor.execute("DELETE FROM broadcast_content")
            # Добавляем новый
            cursor.execute("""
                INSERT INTO broadcast_content (content_type, text, photo_file_id)
                VALUES (?, ?, ?)
            """, (content_type, text, photo_file_id))
            conn.commit()
    
    def get_content(self) -> Optional[Tuple]:
        """Получить текущий контент для рассылки"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT content_type, text, photo_file_id 
                FROM broadcast_content 
                ORDER BY id DESC LIMIT 1
            """)
            return cursor.fetchone()
    
    # === РАБОТА С РАСПИСАНИЕМ ===
    def set_schedule(self, hour: int, minute: int):
        """Установить расписание (заменяет старое)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM schedule")
            cursor.execute("""
                INSERT INTO schedule (hour, minute, is_active)
                VALUES (?, ?, 1)
            """, (hour, minute))
            conn.commit()
    
    def get_schedule(self) -> Optional[Tuple[int, int]]:
        """Получить текущее расписание"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT hour, minute FROM schedule WHERE is_active = 1 LIMIT 1")
            result = cursor.fetchone()
            return result if result else None
    
    # === ЛОГИ ===
    def log_broadcast(self, recipients_count: int, content_id: int = None):
        """Записать лог рассылки"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO broadcast_logs (recipients_count, content_id)
                VALUES (?, ?)
            """, (recipients_count, content_id))
            conn.commit()
    
    def get_last_broadcast_time(self):
        """Время последней рассылки"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT sent_at, recipients_count 
                FROM broadcast_logs 
                ORDER BY sent_at DESC LIMIT 1
            """)
            return cursor.fetchone()

# Создаём глобальный экземпляр
db = Database()
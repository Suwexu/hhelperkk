import sqlite3
import json
from typing import List, Tuple, Optional, Dict
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
            
            # НОВАЯ ТАБЛИЦА: Рассылки (расширенная)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    content_type TEXT NOT NULL, -- 'text' или 'photo'
                    text TEXT,
                    photo_file_id TEXT,
                    schedule_type TEXT DEFAULT 'fixed', -- 'fixed', 'interval', 'cron'
                    hour INTEGER,  -- для fixed типа
                    minute INTEGER, -- для fixed типа
                    interval_minutes INTEGER, -- для interval типа (например, 60, 30, 120)
                    cron_string TEXT, -- для сложных cron выражений (опционально)
                    days TEXT,  -- JSON массив: ["mon","tue","wed"] или NULL для ежедневно
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_sent_at TIMESTAMP
                )
            """)
            
            # Таблица логов рассылок
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS broadcast_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    broadcast_id INTEGER,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    recipients_count INTEGER,
                    FOREIGN KEY (broadcast_id) REFERENCES broadcasts(id)
                )
            """)
            
            # Создаём индексы
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_broadcasts_time 
                ON broadcasts(hour, minute, is_active)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_broadcasts_interval 
                ON broadcasts(interval_minutes, is_active)
            """)
            
            conn.commit()
    
    # === РАБОТА С ПОЛЬЗОВАТЕЛЯМИ ===
    def add_user(self, user_id: int, username: str = None, first_name: str = None, last_name: str = None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, is_active)
                VALUES (?, ?, ?, ?, 1)
            """, (user_id, username, first_name, last_name))
            conn.commit()
    
    def remove_user(self, user_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
            conn.commit()
    
    def get_active_users(self) -> List[int]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users WHERE is_active = 1")
            return [row[0] for row in cursor.fetchall()]
    
    def get_all_users(self) -> List[Tuple]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE is_active = 1 ORDER BY subscribed_at DESC")
            return cursor.fetchall()
    
    def get_user_count(self) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_active = 1")
            return cursor.fetchone()[0]
    
    # === НОВЫЕ МЕТОДЫ ДЛЯ РАБОТЫ С РАССЫЛКАМИ ===
    
    def add_broadcast(self, name: str, content_type: str, 
                      schedule_type: str = 'fixed',
                      hour: int = None, minute: int = None,
                      interval_minutes: int = None,
                      cron_string: str = None,
                      text: str = None, photo_file_id: str = None, 
                      days: List[str] = None) -> int:
        """Добавить новую рассылку с поддержкой разных типов расписания"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            days_json = json.dumps(days) if days else None
            cursor.execute("""
                INSERT INTO broadcasts 
                (name, content_type, text, photo_file_id, 
                 schedule_type, hour, minute, interval_minutes, cron_string,
                 days, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (name, content_type, text, photo_file_id, 
                  schedule_type, hour, minute, interval_minutes, cron_string, days_json))
            conn.commit()
            return cursor.lastrowid
    
    def update_broadcast(self, broadcast_id: int, **kwargs):
        """Обновить рассылку"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            allowed_fields = ['name', 'content_type', 'text', 'photo_file_id', 
                             'schedule_type', 'hour', 'minute', 'interval_minutes', 
                             'cron_string', 'days', 'is_active']
            updates = []
            values = []
            
            for key, value in kwargs.items():
                if key in allowed_fields:
                    if key == 'days' and value is not None:
                        value = json.dumps(value)
                    updates.append(f"{key} = ?")
                    values.append(value)
            
            if updates:
                values.append(broadcast_id)
                query = f"UPDATE broadcasts SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                cursor.execute(query, values)
                conn.commit()
    
    def delete_broadcast(self, broadcast_id: int):
        """Удалить рассылку"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM broadcasts WHERE id = ?", (broadcast_id,))
            conn.commit()
    
    def get_all_broadcasts(self) -> List[Dict]:
        """Получить все рассылки"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, name, content_type, text, photo_file_id, 
                       schedule_type, hour, minute, interval_minutes, cron_string,
                       days, is_active, created_at, updated_at, last_sent_at
                FROM broadcasts 
                ORDER BY created_at DESC
            """)
            rows = cursor.fetchall()
            
            broadcasts = []
            for row in rows:
                broadcasts.append({
                    'id': row[0],
                    'name': row[1],
                    'content_type': row[2],
                    'text': row[3],
                    'photo_file_id': row[4],
                    'schedule_type': row[5],
                    'hour': row[6],
                    'minute': row[7],
                    'interval_minutes': row[8],
                    'cron_string': row[9],
                    'days': json.loads(row[10]) if row[10] else None,
                    'is_active': bool(row[11]),
                    'created_at': row[12],
                    'updated_at': row[13],
                    'last_sent_at': row[14]
                })
            return broadcasts
    
    def get_broadcast(self, broadcast_id: int) -> Optional[Dict]:
        """Получить одну рассылку по ID"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, name, content_type, text, photo_file_id, 
                       schedule_type, hour, minute, interval_minutes, cron_string,
                       days, is_active, created_at, updated_at, last_sent_at
                FROM broadcasts WHERE id = ?
            """, (broadcast_id,))
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
                    'cron_string': row[9],
                    'days': json.loads(row[10]) if row[10] else None,
                    'is_active': bool(row[11]),
                    'created_at': row[12],
                    'updated_at': row[13],
                    'last_sent_at': row[14]
                }
            return None
    
    def update_last_sent(self, broadcast_id: int):
        """Обновить время последней отправки"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE broadcasts SET last_sent_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """, (broadcast_id,))
            conn.commit()
    
    def log_broadcast(self, broadcast_id: int, recipients_count: int):
        """Записать лог отправки"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO broadcast_logs (broadcast_id, recipients_count)
                VALUES (?, ?)
            """, (broadcast_id, recipients_count))
            conn.commit()

# Создаём глобальный экземпляр
db = Database()
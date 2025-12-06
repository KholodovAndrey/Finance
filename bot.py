import asyncio
import logging
import os
import json
import csv
import io
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum
import sqlite3

# Загружаем переменные окружения из .env файла
from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Настройка логирования
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('finance_bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Загрузка конфигурации из переменных окружения
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TAX_RATE = float(os.getenv('TAX_RATE', 0.15))
PROFIT_RATE = float(os.getenv('PROFIT_RATE', 0.85))
DB_PATH = os.getenv('DB_PATH', 'finance_bot.db')
ADMIN_USER_ID = os.getenv('ADMIN_USER_ID')

# Отладочная информация
print("=" * 50)
print("Проверка загрузки переменных окружения:")
print(f"Токен загружен: {'ДА' if TOKEN else 'НЕТ'}")
if TOKEN:
    print(f"Длина токена: {len(TOKEN)} символов")
    print(f"Первые 20 символов: {TOKEN[:20]}...")
    print(f"Токен выглядит так: {TOKEN}")
print("=" * 50)

# Проверка обязательных переменных
if not TOKEN:
    logger.error("❌ TELEGRAM_BOT_TOKEN не найден в .env файле!")
    logger.info("Создайте файл .env в корне проекта со следующим содержимым:")
    logger.info("TELEGRAM_BOT_TOKEN=ваш_токен_бота_здесь")
    logger.info("TAX_RATE=0.15")
    logger.info("PROFIT_RATE=0.85")
    logger.info("DB_PATH=finance_bot.db")
    logger.info("LOG_LEVEL=INFO")
    exit(1)

logger.info(f"✅ Конфигурация загружена")
logger.info(f"   • База данных: {DB_PATH}")
logger.info(f"   • Налог: {TAX_RATE*100}%")
logger.info(f"   • Прибыль: {PROFIT_RATE*100}%")

# SQLite хранилище
class SQLiteStorage:
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self.conn = None
        logger.info(f"Инициализация БД: {self.db_path}")
        self.init_db()
    
    def get_connection(self):
        """Получить соединение с БД"""
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            # Включаем поддержку внешних ключей
            self.conn.execute("PRAGMA foreign_keys = ON")
        return self.conn
    
    def init_db(self):
        """Инициализация базы данных"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Таблица проектов
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            budget REAL NOT NULL,
            income REAL DEFAULT 0,
            status TEXT NOT NULL,
            created TEXT NOT NULL,
            last_updated TEXT NOT NULL,
            user_id INTEGER,
            archived INTEGER DEFAULT 0,
            archived_at TEXT
        )
        ''')
        
        # Таблица платежей проектов
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS project_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            date TEXT NOT NULL,
            tax REAL NOT NULL,
            profit REAL NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE
        )
        ''')
        
        # Таблица доходов
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            tax REAL NOT NULL,
            profit REAL NOT NULL,
            description TEXT,
            project_id TEXT,
            project_name TEXT,
            date TEXT NOT NULL,
            user_id INTEGER
        )
        ''')
        
        # Таблица долгов
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            person TEXT NOT NULL,
            amount REAL NOT NULL,
            remaining REAL NOT NULL,
            description TEXT,
            created TEXT NOT NULL,
            last_updated TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            user_id INTEGER
        )
        ''')
        
        # Таблица платежей по долгам
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS debt_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            debt_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            date TEXT NOT NULL,
            description TEXT,
            FOREIGN KEY (debt_id) REFERENCES debts (id) ON DELETE CASCADE
        )
        ''')
        
        # Таблица пользователей
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created TEXT NOT NULL,
            last_active TEXT NOT NULL
        )
        ''')
        
        # Создаем индексы для ускорения запросов
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_incomes_date ON incomes(date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_incomes_user ON incomes(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_projects_archived ON projects(archived)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_debts_type_status ON debts(type, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_project_payments_project ON project_payments(project_id)')
        
        conn.commit()
        logger.info("✅ База данных инициализирована")
    
    # ========== ПРОЕКТЫ ==========
    
    def add_project(self, project_data: Dict) -> bool:
        """Добавить проект"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
            INSERT INTO projects (id, name, description, budget, income, status, created, last_updated, user_id, archived)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                project_data['id'],
                project_data['name'],
                project_data['description'],
                project_data['budget'],
                project_data.get('income', 0),
                project_data['status'],
                project_data['created'],
                project_data['last_updated'],
                project_data.get('user_id'),
                project_data.get('archived', 0)
            ))
            
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка добавления проекта: {e}")
            return False
    
    def get_project(self, project_id: str) -> Optional[Dict]:
        """Получить проект по ID"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT * FROM projects WHERE id = ?', (project_id,))
            row = cursor.fetchone()
            
            if row:
                return dict(row)
            return None
        except Exception as e:
            logger.error(f"Ошибка получения проекта: {e}")
            return None
    
    def get_all_projects(self, include_archived: bool = False) -> List[Dict]:
        """Получить все проекты"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            if include_archived:
                cursor.execute('SELECT * FROM projects ORDER BY created DESC')
            else:
                cursor.execute('SELECT * FROM projects WHERE archived = 0 ORDER BY created DESC')
            
            rows = cursor.fetchall()
            
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Ошибка получения проектов: {e}")
            return []
    
    def get_archived_projects(self) -> List[Dict]:
        """Получить архивные проекты"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT * FROM projects WHERE archived = 1 ORDER BY archived_at DESC')
            rows = cursor.fetchall()
            
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Ошибка получения архивных проектов: {e}")
            return []
    
    def update_project(self, project_id: str, update_data: Dict) -> bool:
        """Обновить проект"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            set_clause = ', '.join([f"{k} = ?" for k in update_data.keys()])
            values = list(update_data.values())
            values.append(project_id)
            
            cursor.execute(f'UPDATE projects SET {set_clause} WHERE id = ?', values)
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка обновления проекта: {e}")
            return False
    
    def archive_project(self, project_id: str) -> bool:
        """Архивировать проект"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
            UPDATE projects 
            SET archived = 1, archived_at = ?, last_updated = ?
            WHERE id = ?
            ''', (
                datetime.now().isoformat(),
                datetime.now().isoformat(),
                project_id
            ))
            
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка архивации проекта: {e}")
            return False
    
    def unarchive_project(self, project_id: str) -> bool:
        """Восстановить проект из архива"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
            UPDATE projects 
            SET archived = 0, archived_at = NULL, last_updated = ?
            WHERE id = ?
            ''', (
                datetime.now().isoformat(),
                project_id
            ))
            
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка восстановления проекта: {e}")
            return False
    
    def delete_project(self, project_id: str) -> bool:
        """Удалить проект"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM projects WHERE id = ?', (project_id,))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка удаления проекта: {e}")
            return False
    
    def add_project_payment(self, payment_data: Dict) -> bool:
        """Добавить платеж по проекту"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
            INSERT INTO project_payments (project_id, amount, description, date, tax, profit)
            VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                payment_data['project_id'],
                payment_data['amount'],
                payment_data['description'],
                payment_data['date'],
                payment_data['tax'],
                payment_data['profit']
            ))
            
            # Обновляем income в проекте
            cursor.execute('''
            UPDATE projects 
            SET income = income + ?, last_updated = ?
            WHERE id = ?
            ''', (
                payment_data['amount'],
                payment_data['date'],
                payment_data['project_id']
            ))
            
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка добавления платежа проекта: {e}")
            return False
    
    def get_project_payments(self, project_id: str) -> List[Dict]:
        """Получить платежи по проекту"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT * FROM project_payments WHERE project_id = ? ORDER BY date DESC', (project_id,))
            rows = cursor.fetchall()
            
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Ошибка получения платежей проекта: {e}")
            return []
    
    # ========== ДОХОДЫ ==========
    
    def add_income(self, income_data: Dict) -> bool:
        """Добавить доход"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
            INSERT INTO incomes (amount, tax, profit, description, project_id, project_name, date, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                income_data['amount'],
                income_data['tax'],
                income_data['profit'],
                income_data['description'],
                income_data.get('project_id'),
                income_data.get('project_name'),
                income_data['date'],
                income_data.get('user_id')
            ))
            
            # Если доход привязан к проекту, обновляем доход проекта
            if income_data.get('project_id'):
                cursor.execute('''
                UPDATE projects 
                SET income = income + ?, last_updated = ?
                WHERE id = ?
                ''', (
                    income_data['amount'],
                    income_data['date'],
                    income_data['project_id']
                ))
            
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка добавления дохода: {e}")
            return False
    
    def get_incomes(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[Dict]:
        """Получить доходы за период"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = 'SELECT * FROM incomes'
            params = []
            
            if start_date:
                query += ' WHERE date >= ?'
                params.append(start_date)
                if end_date:
                    query += ' AND date <= ?'
                    params.append(end_date)
            elif end_date:
                query += ' WHERE date <= ?'
                params.append(end_date)
            
            query += ' ORDER BY date DESC'
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Ошибка получения доходов: {e}")
            return []
    
    def get_total_income_stats(self) -> Dict:
        """Получить общую статистику по доходам"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
            SELECT 
                COUNT(*) as count,
                SUM(amount) as total_amount,
                SUM(tax) as total_tax,
                SUM(profit) as total_profit
            FROM incomes
            ''')
            
            row = cursor.fetchone()
            return dict(row) if row else {
                'count': 0,
                'total_amount': 0,
                'total_tax': 0,
                'total_profit': 0
            }
        except Exception as e:
            logger.error(f"Ошибка получения статистики доходов: {e}")
            return {'count': 0, 'total_amount': 0, 'total_tax': 0, 'total_profit': 0}
    
    # ========== ДОЛГИ ==========
    
    def add_debt(self, debt_data: Dict) -> int:
        """Добавить долг, возвращает ID созданного долга"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
            INSERT INTO debts (type, person, amount, remaining, description, created, last_updated, status, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                debt_data['type'],
                debt_data['person'],
                debt_data['amount'],
                debt_data['remaining'],
                debt_data['description'],
                debt_data['created'],
                debt_data['last_updated'],
                debt_data.get('status', 'active'),
                debt_data.get('user_id')
            ))
            
            debt_id = cursor.lastrowid
            conn.commit()
            return debt_id
        except Exception as e:
            logger.error(f"Ошибка добавления долга: {e}")
            return -1
    
    def get_debt(self, debt_id: int) -> Optional[Dict]:
        """Получить долг по ID"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT * FROM debts WHERE id = ?', (debt_id,))
            row = cursor.fetchone()
            
            if row:
                return dict(row)
            return None
        except Exception as e:
            logger.error(f"Ошибка получения долга: {e}")
            return None
    
    def get_debts_by_type(self, debt_type: str, status: str = 'active') -> List[Dict]:
        """Получить долги по типу"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
            SELECT * FROM debts 
            WHERE type = ? AND status = ?
            ORDER BY created DESC
            ''', (debt_type, status))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Ошибка получения долгов по типу: {e}")
            return []
    
    def update_debt(self, debt_id: int, update_data: Dict) -> bool:
        """Обновить долг"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            set_clause = ', '.join([f"{k} = ?" for k in update_data.keys()])
            values = list(update_data.values())
            values.append(debt_id)
            
            cursor.execute(f'UPDATE debts SET {set_clause} WHERE id = ?', values)
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка обновления долга: {e}")
            return False
    
    def delete_debt(self, debt_id: int) -> bool:
        """Удалить долг"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM debts WHERE id = ?', (debt_id,))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка удаления долга: {e}")
            return False
    
    def add_debt_payment(self, payment_data: Dict) -> bool:
        """Добавить платеж по долгу"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
            INSERT INTO debt_payments (debt_id, amount, date, description)
            VALUES (?, ?, ?, ?)
            ''', (
                payment_data['debt_id'],
                payment_data['amount'],
                payment_data['date'],
                payment_data.get('description', '')
            ))
            
            # Обновляем остаток долга
            cursor.execute('''
            UPDATE debts 
            SET remaining = remaining - ?, last_updated = ?
            WHERE id = ?
            ''', (
                payment_data['amount'],
                payment_data['date'],
                payment_data['debt_id']
            ))
            
            # Проверяем, погашен ли долг полностью
            cursor.execute('SELECT remaining FROM debts WHERE id = ?', (payment_data['debt_id'],))
            remaining = cursor.fetchone()[0]
            
            if remaining <= 0:
                cursor.execute('UPDATE debts SET status = "paid", remaining = 0 WHERE id = ?', (payment_data['debt_id'],))
            
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка добавления платежа по долгу: {e}")
            return False
    
    def get_debt_payments(self, debt_id: int) -> List[Dict]:
        """Получить платежи по долгу"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT * FROM debt_payments WHERE debt_id = ? ORDER BY date DESC', (debt_id,))
            rows = cursor.fetchall()
            
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Ошибка получения платежей по долгу: {e}")
            return []
    
    def get_debt_stats(self) -> Dict:
        """Получить статистику по долгам"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Статистика по долгам которые вам должны
            cursor.execute('''
            SELECT 
                COUNT(*) as gave_count,
                SUM(remaining) as gave_total
            FROM debts 
            WHERE type = ? AND status = 'active'
            ''', ('GAVE',))
            
            gave_stats = cursor.fetchone()
            
            # Статистика по долгам которые вы должны
            cursor.execute('''
            SELECT 
                COUNT(*) as took_count,
                SUM(remaining) as took_total
            FROM debts 
            WHERE type = ? AND status = 'active'
            ''', ('TOOK',))
            
            took_stats = cursor.fetchone()
            
            return {
                'gave_count': gave_stats[0] if gave_stats and gave_stats[0] else 0,
                'gave_total': gave_stats[1] if gave_stats and gave_stats[1] else 0,
                'took_count': took_stats[0] if took_stats and took_stats[0] else 0,
                'took_total': took_stats[1] if took_stats and took_stats[1] else 0
            }
        except Exception as e:
            logger.error(f"Ошибка получения статистики долгов: {e}")
            return {'gave_count': 0, 'gave_total': 0, 'took_count': 0, 'took_total': 0}
    
    # ========== ПОЛЬЗОВАТЕЛИ ==========
    
    def add_or_update_user(self, user_data: Dict) -> bool:
        """Добавить или обновить пользователя"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, created, last_active)
            VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                user_data['user_id'],
                user_data.get('username'),
                user_data.get('first_name'),
                user_data.get('last_name'),
                user_data.get('created', datetime.now().isoformat()),
                user_data.get('last_active', datetime.now().isoformat())
            ))
            
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка добавления пользователя: {e}")
            return False
    
    def update_user_last_active(self, user_id: int) -> bool:
        """Обновить время последней активности пользователя"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
            UPDATE users SET last_active = ? WHERE user_id = ?
            ''', (datetime.now().isoformat(), user_id))
            
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка обновления пользователя: {e}")
            return False
    
    # ========== ЭКСПОРТ/ИМПОРТ ==========
    
    def export_data(self, format_type: str = 'json') -> Dict:
        """Экспортировать все данные"""
        try:
            data = {
                'export_date': datetime.now().isoformat(),
                'version': '1.0',
                'data': {
                    'projects': self.get_all_projects(include_archived=True),
                    'incomes': self.get_incomes(),
                    'debts': [],
                    'debt_payments': [],
                    'project_payments': [],
                    'users': []
                }
            }
            
            # Получаем долги
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT * FROM debts')
            data['data']['debts'] = [dict(row) for row in cursor.fetchall()]
            
            cursor.execute('SELECT * FROM debt_payments')
            data['data']['debt_payments'] = [dict(row) for row in cursor.fetchall()]
            
            cursor.execute('SELECT * FROM project_payments')
            data['data']['project_payments'] = [dict(row) for row in cursor.fetchall()]
            
            cursor.execute('SELECT * FROM users')
            data['data']['users'] = [dict(row) for row in cursor.fetchall()]
            
            return data
            
        except Exception as e:
            logger.error(f"Ошибка экспорта данных: {e}")
            return {}
    
    def import_data(self, data: Dict) -> bool:
        """Импортировать данные"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Начинаем транзакцию
            cursor.execute('BEGIN TRANSACTION')
            
            # Очищаем все таблицы (кроме пользователей, чтобы сохранить активность)
            cursor.execute('DELETE FROM projects')
            cursor.execute('DELETE FROM incomes')
            cursor.execute('DELETE FROM debts')
            cursor.execute('DELETE FROM debt_payments')
            cursor.execute('DELETE FROM project_payments')
            
            # Импортируем проекты
            for project in data.get('data', {}).get('projects', []):
                cursor.execute('''
                INSERT INTO projects (id, name, description, budget, income, status, created, last_updated, user_id, archived, archived_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    project.get('id', f"proj_{uuid.uuid4().hex[:8]}"),
                    project['name'],
                    project.get('description', ''),
                    project['budget'],
                    project.get('income', 0),
                    project['status'],
                    project.get('created', datetime.now().isoformat()),
                    project.get('last_updated', datetime.now().isoformat()),
                    project.get('user_id'),
                    project.get('archived', 0),
                    project.get('archived_at')
                ))
            
            # Импортируем доходы
            for income in data.get('data', {}).get('incomes', []):
                cursor.execute('''
                INSERT INTO incomes (amount, tax, profit, description, project_id, project_name, date, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    income['amount'],
                    income['tax'],
                    income['profit'],
                    income.get('description', ''),
                    income.get('project_id'),
                    income.get('project_name', ''),
                    income.get('date', datetime.now().isoformat()),
                    income.get('user_id')
                ))
            
            # Импортируем долги
            for debt in data.get('data', {}).get('debts', []):
                cursor.execute('''
                INSERT INTO debts (type, person, amount, remaining, description, created, last_updated, status, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    debt['type'],
                    debt['person'],
                    debt['amount'],
                    debt.get('remaining', debt['amount']),
                    debt.get('description', ''),
                    debt.get('created', datetime.now().isoformat()),
                    debt.get('last_updated', datetime.now().isoformat()),
                    debt.get('status', 'active'),
                    debt.get('user_id')
                ))
            
            # Импортируем платежи по долгам
            for payment in data.get('data', {}).get('debt_payments', []):
                cursor.execute('''
                INSERT INTO debt_payments (debt_id, amount, date, description)
                VALUES (?, ?, ?, ?)
                ''', (
                    payment['debt_id'],
                    payment['amount'],
                    payment.get('date', datetime.now().isoformat()),
                    payment.get('description', '')
                ))
            
            # Импортируем платежи по проектам
            for payment in data.get('data', {}).get('project_payments', []):
                cursor.execute('''
                INSERT INTO project_payments (project_id, amount, description, date, tax, profit)
                VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    payment['project_id'],
                    payment['amount'],
                    payment.get('description', ''),
                    payment.get('date', datetime.now().isoformat()),
                    payment['tax'],
                    payment['profit']
                ))
            
            conn.commit()
            logger.info("✅ Данные успешно импортированы")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка импорта данных: {e}")
            conn.rollback()
            return False
    
    def close(self):
        """Закрыть соединение с БД"""
        if self.conn:
            self.conn.close()
            self.conn = None

# Инициализация хранилища
storage = SQLiteStorage()

# Состояния для FSM
class ProjectStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_description = State()
    waiting_for_amount = State()
    waiting_for_edit_choice = State()
    waiting_for_edit_value = State()
    waiting_for_payment_amount = State()
    waiting_for_payment_description = State()

class IncomeStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_description = State()

class DebtStates(StatesGroup):
    waiting_for_type = State()
    waiting_for_person = State()
    waiting_for_amount = State()
    waiting_for_description = State()
    waiting_for_payment = State()

class ImportStates(StatesGroup):
    waiting_for_import_file = State()

# Enum для статусов проекта
class ProjectStatus(Enum):
    PLANNED = ("📋 Запланирован", "Проект запланирован, но еще не начат")
    IN_PROGRESS = ("⚡ В работе", "Работа над проектом активно ведется")
    PAYMENT_PENDING = ("💰 Ожидает оплаты", "Работа завершена, ожидается оплата")
    PARTIALLY_PAID = ("✅ Частично оплачен", "Получена частичная оплата")
    COMPLETED = ("🎉 Завершен", "Проект полностью завершен и оплачен")
    CANCELLED = ("❌ Отменен", "Проект отменен")
    ARCHIVED = ("📁 В архиве", "Проект перемещен в архив")

# Enum для типов долгов
class DebtType(Enum):
    GAVE = ("📤 Давал в долг", "Вы дали деньги в долг")
    TOOK = ("📥 Брал в долг", "Вы взяли деньги в долг")

# Основной класс бота
class FinanceBot:
    def __init__(self, token: str):
        self.bot = Bot(token=token)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.setup_handlers()
        logger.info("✅ Бот инициализирован")
    
    def setup_handlers(self):
        """Настройка всех обработчиков"""
        
        # Команды
        self.dp.message(Command("start"))(self.start_command)
        self.dp.message(Command("menu"))(self.show_main_menu)
        self.dp.message(Command("help"))(self.help_command)
        self.dp.message(Command("stats"))(self.show_quick_stats_command)
        self.dp.message(Command("projects"))(self.show_projects_command)
        self.dp.message(Command("debts"))(self.show_debts_command)
        self.dp.message(Command("export"))(self.export_data_command)
        self.dp.message(Command("import"))(self.import_data_start)
        
        # Главное меню
        self.dp.callback_query(F.data == "main_menu")(self.show_main_menu_callback)
        self.dp.callback_query(F.data == "add_income")(self.add_income_start)
        self.dp.callback_query(F.data == "add_project")(self.add_project_start)
        self.dp.callback_query(F.data == "projects_list")(self.show_projects_list)
        self.dp.callback_query(F.data == "projects_archive")(self.show_archive_projects)
        self.dp.callback_query(F.data == "analytics")(self.show_analytics_menu)
        self.dp.callback_query(F.data == "quick_stats")(self.show_quick_stats)
        self.dp.callback_query(F.data == "help_menu")(self.help_menu_callback)
        self.dp.callback_query(F.data == "debts_menu")(self.show_debts_menu)
        self.dp.callback_query(F.data == "add_debt")(self.add_debt_start)
        self.dp.callback_query(F.data == "my_debts")(self.show_my_debts)
        self.dp.callback_query(F.data == "owed_to_me")(self.show_owed_to_me)
        self.dp.callback_query(F.data == "data_menu")(self.show_data_menu)
        self.dp.callback_query(F.data == "export_data")(self.export_data_callback)
        self.dp.callback_query(F.data == "import_data")(self.import_data_start_callback)
        
        # Выбор проекта для дохода
        self.dp.callback_query(F.data.startswith("select_project_"))(self.handle_project_selection)
        
        # Доходы
        self.dp.message(IncomeStates.waiting_for_amount)(self.process_income_amount)
        self.dp.message(IncomeStates.waiting_for_description)(self.process_income_description)
        
        # Проекты
        self.dp.message(ProjectStates.waiting_for_name)(self.process_project_name)
        self.dp.message(ProjectStates.waiting_for_description)(self.process_project_description)
        self.dp.message(ProjectStates.waiting_for_amount)(self.process_project_amount)
        self.dp.message(ProjectStates.waiting_for_edit_value)(self.process_project_edit)
        self.dp.message(ProjectStates.waiting_for_payment_amount)(self.process_payment_amount)
        self.dp.message(ProjectStates.waiting_for_payment_description)(self.process_payment_description)
        
        # Долги
        self.dp.message(DebtStates.waiting_for_person)(self.process_debt_person)
        self.dp.message(DebtStates.waiting_for_amount)(self.process_debt_amount)
        self.dp.message(DebtStates.waiting_for_description)(self.process_debt_description)
        self.dp.message(DebtStates.waiting_for_payment)(self.process_debt_payment)
        
        # Импорт
        self.dp.message(ImportStates.waiting_for_import_file)(self.process_import_file)
        
        # Callback-запросы
        self.dp.callback_query(F.data.startswith("project_"))(self.handle_project_action)
        self.dp.callback_query(F.data.startswith("edit_"))(self.handle_edit_action)
        self.dp.callback_query(F.data.startswith("payment_"))(self.handle_payment_action)
        self.dp.callback_query(F.data.startswith("delete_"))(self.handle_delete_project)
        self.dp.callback_query(F.data.startswith("confirm_delete_"))(self.confirm_delete_project)
        self.dp.callback_query(F.data.startswith("setstatus_"))(self.handle_status_change)
        self.dp.callback_query(F.data.startswith("archive_"))(self.handle_archive_project)
        self.dp.callback_query(F.data.startswith("unarchive_"))(self.handle_unarchive_project)
        
        # Долги callback
        self.dp.callback_query(F.data.startswith("debt_type_"))(self.handle_debt_type)
        self.dp.callback_query(F.data.startswith("debt_detail_"))(self.handle_debt_detail)
        self.dp.callback_query(F.data.startswith("pay_debt_"))(self.handle_pay_debt)
        self.dp.callback_query(F.data.startswith("delete_debt_"))(self.handle_delete_debt)
        self.dp.callback_query(F.data.startswith("confirm_delete_debt_"))(self.confirm_delete_debt)
        
        # Аналитика
        self.dp.callback_query(F.data.startswith("analytics_"))(self.handle_analytics)
        
        # Обработка текстовых сообщений
        self.dp.message()(self.handle_text_messages)
        
        logger.info("✅ Обработчики настроены")
    
    async def start_command(self, message: types.Message):
        """Обработка команды /start"""
        # Сохраняем/обновляем пользователя
        user_data = {
            'user_id': message.from_user.id,
            'username': message.from_user.username,
            'first_name': message.from_user.first_name,
            'last_name': message.from_user.last_name
        }
        storage.add_or_update_user(user_data)
        
        welcome_text = """
🚀 *Добро пожаловать в Finance Master Bot!*

*Возможности бота:*
📊 Учет доходов с автоматическим расчетом налогов (15%) и прибыли (85%)
🏢 Управление проектами с этапами оплаты
📈 Аналитика и статистика за период
💰 Отслеживание платежей по проектам
💳 Учет долгов (давал/брал в долг)
📁 Архивация завершенных проектов
📤 Экспорт/импорт данных

*Основные команды:*
/menu - Главное меню
/help - Помощь
/stats - Быстрая статистика
/projects - Список проектов
/debts - Управление долгами
/export - Экспорт данных
/import - Импорт данных

Нажмите /menu чтобы начать работу!
        """
        
        keyboard = InlineKeyboardBuilder()
        keyboard.add(InlineKeyboardButton(text="📱 Главное меню", callback_data="main_menu"))
        keyboard.add(InlineKeyboardButton(text="💰 Добавить доход", callback_data="add_income"))
        keyboard.add(InlineKeyboardButton(text="💳 Долги", callback_data="debts_menu"))
        
        await message.answer(
            welcome_text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
    
    async def show_main_menu(self, message: types.Message):
        """Показать главное меню"""
        await self._show_main_menu(message.chat.id)
    
    async def show_main_menu_callback(self, callback: types.CallbackQuery):
        """Показать главное меню из callback"""
        await self._show_main_menu(callback.message.chat.id)
        await callback.answer()
    
    async def _show_main_menu(self, chat_id: int):
        """Внутренний метод для отображения главного меню"""
        menu_text = "📱 *Главное меню*\nВыберите действие:"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="💰 Добавить доход", callback_data="add_income"),
            InlineKeyboardButton(text="🏢 Добавить проект", callback_data="add_project")
        )
        keyboard.row(
            InlineKeyboardButton(text="📋 Список проектов", callback_data="projects_list"),
            InlineKeyboardButton(text="💳 Долги", callback_data="debts_menu")
        )
        keyboard.row(
            InlineKeyboardButton(text="📊 Аналитика", callback_data="analytics"),
            InlineKeyboardButton(text="⚡ Быстрая статистика", callback_data="quick_stats")
        )
        keyboard.row(
            InlineKeyboardButton(text="📁 Архив проектов", callback_data="projects_archive"),
            InlineKeyboardButton(text="📤 Данные", callback_data="data_menu")
        )
        keyboard.row(
            InlineKeyboardButton(text="❓ Помощь", callback_data="help_menu")
        )
        
        await self.bot.send_message(
            chat_id,
            menu_text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
    
    async def show_data_menu(self, callback: types.CallbackQuery):
        """Показать меню работы с данными"""
        menu_text = "📊 *Работа с данными*\nВыберите действие:"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="📤 Экспорт данных", callback_data="export_data"),
            InlineKeyboardButton(text="📥 Импорт данных", callback_data="import_data")
        )
        keyboard.row(
            InlineKeyboardButton(text="📱 В главное меню", callback_data="main_menu")
        )
        
        await callback.message.edit_text(
            menu_text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def export_data_command(self, message: types.Message):
        """Обработка команды /export"""
        await self._export_data(message.chat.id)
    
    async def export_data_callback(self, callback: types.CallbackQuery):
        """Экспорт данных из callback"""
        await self._export_data(callback.message.chat.id)
        await callback.answer()
    
    async def _export_data(self, chat_id: int):
        """Экспорт данных"""
        try:
            # Получаем данные
            data = storage.export_data()
            
            if not data:
                await self.bot.send_message(
                    chat_id,
                    "❌ Ошибка при экспорте данных!",
                    parse_mode="Markdown"
                )
                return
            
            # Создаем JSON файл
            json_data = json.dumps(data, ensure_ascii=False, indent=2, default=str)
            json_bytes = json_data.encode('utf-8')
            
            # Отправляем файл
            await self.bot.send_document(
                chat_id,
                types.BufferedInputFile(json_bytes, filename=f"finance_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"),
                caption="📤 *Экспорт данных завершен*\n\nФайл содержит все данные в формате JSON. Вы можете использовать его для импорта.",
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logger.error(f"Ошибка экспорта: {e}")
            await self.bot.send_message(
                chat_id,
                f"❌ Ошибка при экспорте данных: {str(e)}",
                parse_mode="Markdown"
            )
    
    async def import_data_start(self, message: types.Message, state: FSMContext):
        """Начало импорта данных из команды"""
        await self._import_data_start(message.chat.id, state)
    
    async def import_data_start_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало импорта данных из callback"""
        await self._import_data_start(callback.message.chat.id, state)
        await callback.answer()
    
    async def _import_data_start(self, chat_id: int, state: FSMContext):
        """Начало импорта данных"""
        await self.bot.send_message(
            chat_id,
            "📥 *Импорт данных*\n\n"
            "Пожалуйста, отправьте JSON файл с данными для импорта.\n\n"
            "⚠️ *Внимание:* Все текущие данные будут заменены!",
            parse_mode="Markdown"
        )
        await state.set_state(ImportStates.waiting_for_import_file)
    
    async def process_import_file(self, message: types.Message, state: FSMContext):
        """Обработка файла для импорта"""
        if not message.document:
            await message.answer("❌ Пожалуйста, отправьте файл в формате JSON.")
            await state.clear()
            return
        
        # Проверяем расширение файла
        file_name = message.document.file_name
        if not file_name.endswith('.json'):
            await message.answer("❌ Файл должен быть в формате JSON (.json)")
            await state.clear()
            return
        
        try:
            # Скачиваем файл
            file_info = await self.bot.get_file(message.document.file_id)
            downloaded_file = await self.bot.download_file(file_info.file_path)
            
            # Читаем JSON
            json_data = downloaded_file.read().decode('utf-8')
            data = json.loads(json_data)
            
            # Проверяем структуру данных
            if 'data' not in data or 'export_date' not in data:
                await message.answer("❌ Неверный формат файла. Убедитесь, что это файл экспорта из Finance Bot.")
                await state.clear()
                return
            
            # Импортируем данные
            success = storage.import_data(data)
            
            if success:
                await message.answer(
                    f"✅ *Данные успешно импортированы!*\n\n"
                    f"📅 Дата экспорта: {datetime.fromisoformat(data['export_date']).strftime('%d.%m.%Y %H:%M')}\n"
                    f"📊 Версия: {data.get('version', '1.0')}\n"
                    f"🏢 Проектов: {len(data['data'].get('projects', []))}\n"
                    f"💰 Доходов: {len(data['data'].get('incomes', []))}\n"
                    f"💳 Долгов: {len(data['data'].get('debts', []))}",
                    parse_mode="Markdown"
                )
            else:
                await message.answer("❌ Ошибка при импорте данных. Проверьте формат файла.")
            
        except json.JSONDecodeError:
            await message.answer("❌ Ошибка чтения JSON файла. Проверьте корректность файла.")
        except Exception as e:
            logger.error(f"Ошибка импорта: {e}")
            await message.answer(f"❌ Ошибка при импорте данных: {str(e)}")
        
        await state.clear()
    
    async def help_command(self, message: types.Message):
        """Показать помощь"""
        await self._show_help(message.chat.id)
    
    async def help_menu_callback(self, callback: types.CallbackQuery):
        """Показать помощь из callback"""
        await self._show_help(callback.message.chat.id)
        await callback.answer()
    
    async def _show_help(self, chat_id: int):
        """Показать справку"""
        help_text = """
📚 *Помощь по Finance Master Bot*

*Основные команды:*
/menu - Главное меню
/stats - Быстрая статистика
/projects - Список проектов
/debts - Управление долгами
/export - Экспорт данных (JSON)
/import - Импорт данных

*Как добавить доход:*
1. Нажмите "💰 Добавить доход"
2. Введите сумму дохода
3. Выберите проект (или оставьте без проекта)
4. Добавьте описание

*Как работать с проектами:*
• *Добавление:* Укажите название, описание и бюджет
• *Статусы:* Отслеживайте прогресс проекта
• *Оплата:* Добавляйте платежи по этапам
• *Архивация:* Завершенные проекты можно отправить в архив
• *Статусы проектов:*
  📋 Запланирован - проект запланирован, но не начат
  ⚡ В работе - работа активно ведется
  💰 Ожидает оплаты - работа завершена, ждет оплаты
  ✅ Частично оплачен - получена частичная оплата
  🎉 Завершен - полностью завершен и оплачен
  📁 В архиве - проект перемещен в архив
  ❌ Отменен - проект отменен

*Учет долгов:*
• 📤 Давал в долг - вы дали деньги
• 📥 Брал в долг - вы взяли деньги
• Можно вносить частичные платежи
• Долг автоматически закрывается при полной выплате

*Архивация проектов:*
• Завершенные проекты можно архивировать
• Архивные проекты не отображаются в основном списке
• Можно просмотреть архив и восстановить проекты

*Экспорт/импорт:*
• 📤 Экспорт - скачать все данные в JSON
• 📥 Импорт - загрузить данные из JSON файла
        """
        
        keyboard = InlineKeyboardBuilder()
        keyboard.add(InlineKeyboardButton(text="📱 В главное меню", callback_data="main_menu"))
        
        await self.bot.send_message(
            chat_id,
            help_text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
    
    # ========== ДОЛГИ ==========
    
    async def show_debts_menu(self, callback: types.CallbackQuery):
        """Показать меню долгов"""
        menu_text = "💳 *Управление долгами*\nВыберите действие:"
        
        # Подсчет статистики
        debt_stats = storage.get_debt_stats()
        
        menu_text += f"\n\n📊 *Статистика:*"
        menu_text += f"\n📤 Вам должны: {debt_stats['gave_total']:,.2f} ₽ ({debt_stats['gave_count']} долгов)"
        menu_text += f"\n📥 Вы должны: {debt_stats['took_total']:,.2f} ₽ ({debt_stats['took_count']} долгов)"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="📤 Давал в долг", callback_data="owed_to_me"),
            InlineKeyboardButton(text="📥 Брал в долг", callback_data="my_debts")
        )
        keyboard.row(
            InlineKeyboardButton(text="➕ Добавить долг", callback_data="add_debt"),
            InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
        )
        
        if callback.message.text != menu_text:
            await callback.message.edit_text(
                menu_text,
                parse_mode="Markdown",
                reply_markup=keyboard.as_markup()
            )
        else:
            await callback.message.answer(
                menu_text,
                parse_mode="Markdown",
                reply_markup=keyboard.as_markup()
            )
        await callback.answer()
    
    async def show_debts_command(self, message: types.Message):
        """Обработка команды /debts"""
        class FakeCallback:
            def __init__(self, message):
                self.message = message
                self.data = "debts_menu"
            
            async def answer(self):
                pass
        
        await self.show_debts_menu(FakeCallback(message))
    
    async def add_debt_start(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало добавления долга"""
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="📤 Я дал в долг", callback_data="debt_type_GAVE"),
            InlineKeyboardButton(text="📥 Я взял в долг", callback_data="debt_type_TOOK")
        )
        keyboard.row(
            InlineKeyboardButton(text="↩️ Назад", callback_data="debts_menu")
        )
        
        await callback.message.edit_text(
            "💳 *Выберите тип долга:*\n\n"
            "📤 *Я дал в долг* - кто-то должен вам деньги\n"
            "📥 *Я взял в долг* - вы должны кому-то деньги",
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def handle_debt_type(self, callback: types.CallbackQuery, state: FSMContext):
        """Обработка выбора типа долга"""
        debt_type_name = callback.data.replace("debt_type_", "")
        
        try:
            debt_type = DebtType[debt_type_name]
            await state.update_data(debt_type=debt_type_name)
            
            text = "📝 *Введите ФИО человека:*\n"
            if debt_type == DebtType.GAVE:
                text += "Кому вы дали деньги в долг?"
            else:
                text += "У кого вы взяли деньги в долг?"
            
            await callback.message.edit_text(
                text,
                parse_mode="Markdown"
            )
            await state.set_state(DebtStates.waiting_for_person)
            await callback.answer()
        except KeyError:
            await callback.answer("❌ Неверный тип долга!")
    
    async def process_debt_person(self, message: types.Message, state: FSMContext):
        """Обработка ФИО для долга"""
        await state.update_data(person=message.text)
        await message.answer(
            "💰 *Введите сумму долга:*\n"
            "Например: 10000 или 5000.50",
            parse_mode="Markdown"
        )
        await state.set_state(DebtStates.waiting_for_amount)
    
    async def process_debt_amount(self, message: types.Message, state: FSMContext):
        """Обработка суммы долга"""
        try:
            amount = float(message.text.replace(',', '.'))
            if amount <= 0:
                await message.answer("❌ Сумма должна быть положительной!")
                return
            
            await state.update_data(amount=amount, remaining=amount)
            await message.answer(
                "📝 *Введите описание/причину долга:*\n"
                "Например: 'На ремонт машины' или 'Взаймы на время'",
                parse_mode="Markdown"
            )
            await state.set_state(DebtStates.waiting_for_description)
                
        except ValueError:
            await message.answer("❌ Пожалуйста, введите корректную сумму!")
    
    async def process_debt_description(self, message: types.Message, state: FSMContext):
        """Обработка описания долга"""
        data = await state.get_data()
        
        debt_data = {
            'type': data['debt_type'],
            'person': data['person'],
            'amount': data['amount'],
            'remaining': data['amount'],
            'description': message.text,
            'created': datetime.now().isoformat(),
            'last_updated': datetime.now().isoformat(),
            'user_id': message.from_user.id
        }
        
        debt_id = storage.add_debt(debt_data)
        
        if debt_id == -1:
            await message.answer("❌ Ошибка при добавлении долга!")
            await state.clear()
            return
        
        debt_type = DebtType[data['debt_type']]
        type_text = debt_type.value[0]
        
        result_text = f"""
✅ *Долг успешно добавлен!*

💳 *Детали:*
• Тип: {type_text}
• Кому/от кого: {data['person']}
• Сумма: {data['amount']:,.2f} ₽
• Остаток: {data['amount']:,.2f} ₽
• Описание: {message.text}
• Дата: {datetime.now().strftime('%d.%m.%Y')}
• ID: #{debt_id}
        """
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="💳 Добавить еще долг", callback_data="add_debt"),
            InlineKeyboardButton(text="📋 Список долгов", callback_data="debts_menu")
        )
        keyboard.row(
            InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
        )
        
        await message.answer(
            result_text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        
        await state.clear()
    
    async def show_my_debts(self, callback: types.CallbackQuery):
        """Показать мои долги (я должен)"""
        my_debts = storage.get_debts_by_type('TOOK', 'active')
        
        if not my_debts:
            await callback.message.edit_text(
                "📭 *У вас нет долгов, которые вы взяли*\n"
                "Вы никому не должны денег! 🎉",
                parse_mode="Markdown"
            )
            
            keyboard = InlineKeyboardBuilder()
            keyboard.row(
                InlineKeyboardButton(text="➕ Добавить долг", callback_data="add_debt"),
                InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
            )
            await callback.message.answer(
                "Выберите действия:",
                reply_markup=keyboard.as_markup()
            )
            await callback.answer()
            return
        
        text = "📥 *Мои долги (я должен):*\n\n"
        total_owed = 0
        
        for debt in my_debts:
            remaining = debt['remaining']
            total_owed += remaining
            progress = ((debt['amount'] - remaining) / debt['amount'] * 100) if debt['amount'] > 0 else 0
            
            text += f"👤 *{debt.get('person', 'Неизвестно')}*\n"
            text += f"   Сумма: {debt['amount']:,.2f} ₽\n"
            text += f"   Осталось: {remaining:,.2f} ₽\n"
            text += f"   Оплачено: {progress:.1f}%\n"
            text += f"   Описание: {debt.get('description', '')}\n"
            text += f"   ID: #{debt['id']}\n\n"
        
        text += f"\n💸 *Всего должен: {total_owed:,.2f} ₽*"
        
        keyboard = InlineKeyboardBuilder()
        
        # Добавляем кнопки для быстрого перехода к каждому долгу
        for debt in my_debts:  # Убрано ограничение
            person = debt.get('person', 'Неизвестно')[:15]
            keyboard.row(
                InlineKeyboardButton(
                    text=f"🔍 {person} - {debt['remaining']:,.0f}₽",
                    callback_data=f"debt_detail_{debt['id']}"
                )
            )
        
        keyboard.row(
            InlineKeyboardButton(text="➕ Новый долг", callback_data="add_debt"),
            InlineKeyboardButton(text="📤 Кому я давал", callback_data="owed_to_me")
        )
        keyboard.row(
            InlineKeyboardButton(text="💳 В меню долгов", callback_data="debts_menu"),
            InlineKeyboardButton(text="📱 В главное меню", callback_data="main_menu")
        )
        
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def show_owed_to_me(self, callback: types.CallbackQuery):
        """Показать долги мне (я давал в долг)"""
        owed_to_me = storage.get_debts_by_type('GAVE', 'active')
        
        if not owed_to_me:
            await callback.message.edit_text(
                "📭 *Вам никто не должен*\n"
                "Все долги возвращены! 🎉",
                parse_mode="Markdown"
            )
            
            keyboard = InlineKeyboardBuilder()
            keyboard.row(
                InlineKeyboardButton(text="➕ Добавить долг", callback_data="add_debt"),
                InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
            )
            await callback.message.answer(
                "Выберите действия:",
                reply_markup=keyboard.as_markup()
            )
            await callback.answer()
            return
        
        text = "📤 *Долги мне (мне должны):*\n\n"
        total_owed = 0
        
        for debt in owed_to_me:
            remaining = debt['remaining']
            total_owed += remaining
            progress = ((debt['amount'] - remaining) / debt['amount'] * 100) if debt['amount'] > 0 else 0
            
            text += f"👤 *{debt.get('person', 'Неизвестно')}*\n"
            text += f"   Сумма: {debt['amount']:,.2f} ₽\n"
            text += f"   Осталось: {remaining:,.2f} ₽\n"
            text += f"   Возвращено: {progress:.1f}%\n"
            text += f"   Описание: {debt.get('description', '')}\n"
            text += f"   ID: #{debt['id']}\n\n"
        
        text += f"\n💰 *Всего должны мне: {total_owed:,.2f} ₽*"
        
        keyboard = InlineKeyboardBuilder()
        
        for debt in owed_to_me:  # Убрано ограничение
            person = debt.get('person', 'Неизвестно')[:15]
            keyboard.row(
                InlineKeyboardButton(
                    text=f"🔍 {person} - {debt['remaining']:,.0f}₽",
                    callback_data=f"debt_detail_{debt['id']}"
                )
            )
        
        keyboard.row(
            InlineKeyboardButton(text="➕ Новый долг", callback_data="add_debt"),
            InlineKeyboardButton(text="📥 Что я должен", callback_data="my_debts")
        )
        keyboard.row(
            InlineKeyboardButton(text="💳 В меню долгов", callback_data="debts_menu"),
            InlineKeyboardButton(text="📱 В главное меню", callback_data="main_menu")
        )
        
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def handle_debt_detail(self, callback: types.CallbackQuery):
        """Показать детали долга"""
        debt_id = int(callback.data.replace("debt_detail_", ""))
        
        debt = storage.get_debt(debt_id)
        
        if not debt:
            await callback.answer("❌ Долг не найден!")
            return
        
        debt_type = DebtType[debt['type']]
        type_text, type_desc = debt_type.value
        
        remaining = debt['remaining']
        paid = debt['amount'] - remaining
        progress = (paid / debt['amount'] * 100) if debt['amount'] > 0 else 0
        
        # Получаем платежи по долгу
        payments = storage.get_debt_payments(debt_id)
        
        text = f"""
💳 *Детали долга* #{debt_id}

*Тип:* {type_text}
*{type_desc}*

👤 *Кому/от кого:* {debt.get('person', 'Неизвестно')}
💰 *Изначальная сумма:* {debt['amount']:,.2f} ₽
📊 *Остаток к оплате:* {remaining:,.2f} ₽
✅ *Уже оплачено:* {paid:,.2f} ₽ ({progress:.1f}%)
📝 *Описание:* {debt.get('description', 'Нет описания')}
📅 *Создан:* {datetime.fromisoformat(debt['created']).strftime('%d.%m.%Y')}
🔄 *Обновлен:* {datetime.fromisoformat(debt['last_updated']).strftime('%d.%m.%Y %H:%M')}
📊 *Статус:* {debt['status'].capitalize()}

💸 *Платежи:* {len(payments)}
        """
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="💰 Внести платеж", callback_data=f"pay_debt_{debt_id}"),
            InlineKeyboardButton(text="🗑️ Удалить долг", callback_data=f"delete_debt_{debt_id}")
        )
        
        if debt['type'] == 'TOOK':
            keyboard.row(
                InlineKeyboardButton(text="📥 Мои долги", callback_data="my_debts"),
                InlineKeyboardButton(text="📤 Долги мне", callback_data="owed_to_me")
            )
        else:
            keyboard.row(
                InlineKeyboardButton(text="📤 Долги мне", callback_data="owed_to_me"),
                InlineKeyboardButton(text="📥 Мои долги", callback_data="my_debts")
            )
        
        keyboard.row(
            InlineKeyboardButton(text="💳 В меню долгов", callback_data="debts_menu"),
            InlineKeyboardButton(text="📱 В главное меню", callback_data="main_menu")
        )
        
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def handle_pay_debt(self, callback: types.CallbackQuery, state: FSMContext):
        """Обработка внесения платежа по долгу"""
        debt_id = int(callback.data.replace("pay_debt_", ""))
        
        debt = storage.get_debt(debt_id)
        
        if not debt:
            await callback.answer("❌ Долг не найден!")
            return
        
        remaining = debt['remaining']
        
        if remaining <= 0:
            await callback.answer("✅ Долг уже полностью погашен!")
            return
        
        await state.update_data(debt_id=debt_id, max_amount=remaining)
        
        debt_type = DebtType[debt['type']]
        if debt_type == DebtType.GAVE:
            action = "получения"
        else:
            action = "оплаты"
        
        await callback.message.answer(
            f"💰 *Введите сумму {action}:*\n"
            f"Максимально можно: {remaining:,.2f} ₽\n"
            f"Для полного погашения введите: {remaining:,.2f}",
            parse_mode="Markdown"
        )
        await state.set_state(DebtStates.waiting_for_payment)
        await callback.answer()
    
    async def process_debt_payment(self, message: types.Message, state: FSMContext):
        """Обработка платежа по долгу"""
        try:
            amount = float(message.text.replace(',', '.'))
            data = await state.get_data()
            debt_id = data['debt_id']
            max_amount = data['max_amount']
            
            if amount <= 0:
                await message.answer("❌ Сумма должна быть положительной!")
                return
            
            if amount > max_amount:
                await message.answer(f"❌ Сумма не может превышать остаток долга ({max_amount:,.2f} ₽)")
                return
            
            # Находим долг
            debt = storage.get_debt(debt_id)
            
            if not debt:
                await message.answer("❌ Долг не найден!")
                await state.clear()
                return
            
            # Добавляем платеж
            payment_data = {
                'debt_id': debt_id,
                'amount': amount,
                'date': datetime.now().isoformat(),
                'description': f"Частичный платеж"
            }
            
            success = storage.add_debt_payment(payment_data)
            
            if not success:
                await message.answer("❌ Ошибка при добавлении платежа!")
                await state.clear()
                return
            
            # Получаем обновленный долг
            updated_debt = storage.get_debt(debt_id)
            
            if updated_debt['remaining'] <= 0:
                debt_paid_text = "\n🎉 *Долг полностью погашен!*"
            else:
                debt_paid_text = f"\n📊 *Остаток долга: {updated_debt['remaining']:,.2f} ₽*"
            
            debt_type = DebtType[debt['type']]
            if debt_type == DebtType.GAVE:
                action = "получен"
                direction = "от"
            else:
                action = "внесен"
                direction = "для"
            
            result_text = f"""
✅ *Платеж успешно {action}!*

💳 *Детали платежа:*
• Сумма: {amount:,.2f} ₽
• {direction}: {debt.get('person', 'Неизвестно')}
• Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}
{debt_paid_text}
            """
        
            keyboard = InlineKeyboardBuilder()
            keyboard.row(
                InlineKeyboardButton(text="💳 Еще платеж", callback_data=f"pay_debt_{debt_id}"),
                InlineKeyboardButton(text="🔍 К долгу", callback_data=f"debt_detail_{debt_id}")
            )
            
            if debt_type == DebtType.TOOK:
                keyboard.row(
                    InlineKeyboardButton(text="📥 Мои долги", callback_data="my_debts"),
                    InlineKeyboardButton(text="📤 Долги мне", callback_data="owed_to_me")
                )
            else:
                keyboard.row(
                    InlineKeyboardButton(text="📤 Долги мне", callback_data="owed_to_me"),
                    InlineKeyboardButton(text="📥 Мои долги", callback_data="my_debts")
                )
            
            await message.answer(
                result_text,
                parse_mode="Markdown",
                reply_markup=keyboard.as_markup()
            )
            
            await state.clear()
            
        except ValueError:
            await message.answer("❌ Пожалуйста, введите корректную сумму!")
    
    async def handle_delete_debt(self, callback: types.CallbackQuery):
        """Обработка удаления долга"""
        debt_id = int(callback.data.replace("delete_debt_", ""))
        
        debt = storage.get_debt(debt_id)
        
        if not debt:
            await callback.answer("❌ Долг не найден!")
            return
        
        person = debt.get('person', 'Неизвестно')
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(
                text="✅ Да, удалить",
                callback_data=f"confirm_delete_debt_{debt_id}"
            ),
            InlineKeyboardButton(
                text="❌ Нет, отмена",
                callback_data=f"debt_detail_{debt_id}"
            )
        )
        
        await callback.message.edit_text(
            f"⚠️ *Вы уверены, что хотите удалить долг?*\n\n"
            f"👤 *{person}*\n"
            f"💰 Сумма: {debt['amount']:,.2f} ₽\n"
            f"📊 Остаток: {debt['remaining']:,.2f} ₽\n\n"
            f"*Это действие нельзя отменить!*",
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def confirm_delete_debt(self, callback: types.CallbackQuery):
        """Подтверждение удаления долга"""
        debt_id = int(callback.data.replace("confirm_delete_debt_", ""))
        
        success = storage.delete_debt(debt_id)
        
        if not success:
            await callback.answer("❌ Ошибка при удалении долга!")
            return
        
        await callback.message.edit_text(
            "🗑️ *Долг успешно удален!*",
            parse_mode="Markdown"
        )
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="💳 В меню долгов", callback_data="debts_menu"),
            InlineKeyboardButton(text="📱 В главное меню", callback_data="main_menu")
        )
        
        await callback.message.answer(
            "Выберите действие:",
            reply_markup=keyboard.as_markup()
        )
        
        await callback.answer()
    
    # ========== ДОХОДЫ ==========
    
    async def add_income_start(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало добавления дохода"""
        await callback.message.answer(
            "💵 *Введите сумму дохода:*\n"
            "Например: 100000 или 50000.50",
            parse_mode="Markdown"
        )
        await state.set_state(IncomeStates.waiting_for_amount)
        await callback.answer()
    
    async def process_income_amount(self, message: types.Message, state: FSMContext):
        """Обработка суммы дохода"""
        try:
            amount = float(message.text.replace(',', '.'))
            if amount <= 0:
                await message.answer("❌ Сумма должна быть положительной!")
                return
            
            tax = amount * TAX_RATE
            profit = amount * PROFIT_RATE
            
            await state.update_data(amount=amount, tax=tax, profit=profit)
            
            # Предложить выбрать проект
            projects = storage.get_all_projects()
            if projects:
                keyboard = InlineKeyboardBuilder()
                for project in projects:
                    keyboard.row(
                        InlineKeyboardButton(
                            text=f"🏢 {project.get('name', 'Без названия')}",
                            callback_data=f"select_project_{project['id']}"
                        )
                    )
                keyboard.row(
                    InlineKeyboardButton(
                        text="🚫 Без проекта",
                        callback_data="select_project_none"
                    )
                )
                
                await message.answer(
                    "🏢 *Выберите проект для этого дохода:*",
                    parse_mode="Markdown",
                    reply_markup=keyboard.as_markup()
                )
            else:
                await message.answer(
                    "📝 *Введите описание дохода:*\n"
                    "Например: 'Оплата за проект X'",
                    parse_mode="Markdown"
                )
                await state.set_state(IncomeStates.waiting_for_description)
                
        except ValueError:
            await message.answer("❌ Пожалуйста, введите корректную сумму!")
    
    async def handle_project_selection(self, callback: types.CallbackQuery, state: FSMContext):
        """Обработка выбора проекта для дохода"""
        project_id = callback.data.replace("select_project_", "")
        
        if project_id == "none":
            await state.update_data(project_id=None, project_name="Не указан")
            await callback.message.answer(
                "📝 *Введите описание дохода:*\n"
                "Например: 'Оплата за проект X'",
                parse_mode="Markdown"
            )
            await state.set_state(IncomeStates.waiting_for_description)
        else:
            project = storage.get_project(project_id)
            if project:
                await state.update_data(
                    project_id=project_id,
                    project_name=project.get('name', 'Без названия')
                )
                await callback.message.answer(
                    "📝 *Введите описание дохода:*\n"
                    "Например: 'Оплата за проект X'",
                    parse_mode="Markdown"
                )
                await state.set_state(IncomeStates.waiting_for_description)
            else:
                await callback.answer("❌ Проект не найден!")
                return
        
        await callback.answer()
    
    async def process_income_description(self, message: types.Message, state: FSMContext):
        """Обработка описания дохода"""
        data = await state.get_data()
        
        income_data = {
            'amount': data['amount'],
            'tax': data['tax'],
            'profit': data['profit'],
            'description': message.text,
            'project_id': data.get('project_id'),
            'project_name': data.get('project_name', 'Не указан'),
            'date': datetime.now().isoformat(),
            'user_id': message.from_user.id
        }
        
        success = storage.add_income(income_data)
        
        if not success:
            await message.answer("❌ Ошибка при добавлении дохода!")
            await state.clear()
            return
        
        # Отправить результат
        result_text = f"""
✅ *Доход успешно добавлен!*

📊 *Детали:*
• Сумма: {data['amount']:,.2f} ₽
• Налог (15%): {data['tax']:,.2f} ₽
• Прибыль (85%): {data['profit']:,.2f} ₽
• Описание: {message.text}
• Проект: {data.get('project_name', 'Не указан')}
• Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}
        """
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="💰 Добавить еще доход", callback_data="add_income"),
            InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
        )
        
        await message.answer(
            result_text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        
        await state.clear()
    
    # ========== ПРОЕКТЫ ==========
    
    async def add_project_start(self, callback: types.CallbackQuery, state: FSMContext):
        """Начало добавления проекта"""
        await callback.message.answer(
            "🏢 *Введите название проекта:*",
            parse_mode="Markdown"
        )
        await state.set_state(ProjectStates.waiting_for_name)
        await callback.answer()
    
    async def process_project_name(self, message: types.Message, state: FSMContext):
        """Обработка названия проекта"""
        await state.update_data(name=message.text)
        await message.answer(
            "📝 *Введите описание проекта:*",
            parse_mode="Markdown"
        )
        await state.set_state(ProjectStates.waiting_for_description)
    
    async def process_project_description(self, message: types.Message, state: FSMContext):
        """Обработка описания проекта"""
        await state.update_data(description=message.text)
        await message.answer(
            "💰 *Введите бюджет проекта:*\n"
            "Например: 500000 или 75000.50",
            parse_mode="Markdown"
        )
        await state.set_state(ProjectStates.waiting_for_amount)
    
    async def process_project_amount(self, message: types.Message, state: FSMContext):
        """Обработка бюджета проекта"""
        try:
            amount = float(message.text.replace(',', '.'))
            data = await state.get_data()
            
            # Генерация уникального ID для проекта
            project_id = f"proj_{uuid.uuid4().hex[:8]}"
            
            project_data = {
                'id': project_id,
                'name': data['name'],
                'description': data['description'],
                'budget': amount,
                'income': 0,
                'status': ProjectStatus.PLANNED.value[0],
                'created': datetime.now().isoformat(),
                'last_updated': datetime.now().isoformat(),
                'user_id': message.from_user.id,
                'archived': 0
            }
            
            success = storage.add_project(project_data)
            
            if not success:
                await message.answer("❌ Ошибка при создании проекта!")
                await state.clear()
                return
            
            result_text = f"""
✅ *Проект успешно создан!*

🏢 *Детали проекта:*
• Название: {data['name']}
• Описание: {data['description']}
• Бюджет: {amount:,.2f} ₽
• Статус: {ProjectStatus.PLANNED.value[0]}
• ID: `{project_id}`

*Доступные действия:*
1. Добавить оплату
2. Изменить статус
3. Добавить заметки
4. Просмотреть детали
            """
            
            keyboard = InlineKeyboardBuilder()
            keyboard.row(
                InlineKeyboardButton(text="💳 Добавить оплату", callback_data=f"payment_{project_id}"),
                InlineKeyboardButton(text="🔍 Просмотреть", callback_data=f"project_{project_id}")
            )
            keyboard.row(
                InlineKeyboardButton(text="📋 Список проектов", callback_data="projects_list"),
                InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
            )
            
            await message.answer(
                result_text,
                parse_mode="Markdown",
                reply_markup=keyboard.as_markup()
            )
            
            await state.clear()
            
        except ValueError:
            await message.answer("❌ Пожалуйста, введите корректную сумму!")
    
    async def show_projects_list(self, callback: types.CallbackQuery):
        """Показать список активных проектов"""
        projects = storage.get_all_projects(include_archived=False)
        
        if not projects:
            await callback.message.answer(
                "📭 *Список проектов пуст*\n"
                "Добавьте первый проект!",
                parse_mode="Markdown"
            )
            await callback.answer()
            return
        
        text = "🏢 *Список активных проектов:*\n\n"
        
        for idx, project in enumerate(projects, 1):
            paid = project['income']
            budget = project['budget']
            progress = (paid / budget * 100) if budget > 0 else 0
            
            text += f"{idx}. *{project.get('name', 'Без названия')}*\n"
            text += f"   Статус: {project.get('status', 'Не указан')}\n"
            text += f"   Бюджет: {budget:,.2f} ₽ | Оплачено: {paid:,.2f} ₽\n"
            text += f"   Прогресс: {progress:.1f}%\n"
            text += f"   ID: `{project['id']}`\n\n"
        
        keyboard = InlineKeyboardBuilder()
        
        # Создаем кнопки для всех проектов (без ограничения)
        for project in projects:
            project_name = project.get('name', 'Проект')[:15]
            keyboard.row(
                InlineKeyboardButton(
                    text=f"🔍 {project_name}",
                    callback_data=f"project_{project['id']}"
                )
            )
        
        keyboard.row(
            InlineKeyboardButton(text="📁 Архив проектов", callback_data="projects_archive"),
            InlineKeyboardButton(text="➕ Новый проект", callback_data="add_project")
        )
        keyboard.row(
            InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
        )
        
        await callback.message.answer(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def show_archive_projects(self, callback: types.CallbackQuery):
        """Показать архивные проекты"""
        projects = storage.get_archived_projects()
        
        if not projects:
            await callback.message.edit_text(
                "📭 *Архив проектов пуст*\n"
                "Здесь будут отображаться завершенные проекты.",
                parse_mode="Markdown"
            )
            
            keyboard = InlineKeyboardBuilder()
            keyboard.row(
                InlineKeyboardButton(text="📋 Активные проекты", callback_data="projects_list"),
                InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
            )
            await callback.message.answer(
                "Выберите действие:",
                reply_markup=keyboard.as_markup()
            )
            await callback.answer()
            return
        
        text = "📁 *Архив проектов:*\n\n"
        
        for idx, project in enumerate(projects, 1):
            paid = project['income']
            budget = project['budget']
            progress = (paid / budget * 100) if budget > 0 else 0
            archived_at = project.get('archived_at')
            archived_date = datetime.fromisoformat(archived_at).strftime('%d.%m.%Y') if archived_at else "Неизвестно"
            
            text += f"{idx}. *{project.get('name', 'Без названия')}*\n"
            text += f"   Статус: {project.get('status', 'Не указан')}\n"
            text += f"   Бюджет: {budget:,.2f} ₽ | Оплачено: {paid:,.2f} ₽\n"
            text += f"   Прогресс: {progress:.1f}%\n"
            text += f"   Архивирован: {archived_date}\n"
            text += f"   ID: `{project['id']}`\n\n"
        
        keyboard = InlineKeyboardBuilder()
        
        # Создаем кнопки для всех архивных проектов
        for project in projects:
            project_name = project.get('name', 'Проект')[:15]
            keyboard.row(
                InlineKeyboardButton(
                    text=f"🔍 {project_name}",
                    callback_data=f"project_{project['id']}"
                )
            )
        
        keyboard.row(
            InlineKeyboardButton(text="📋 Активные проекты", callback_data="projects_list"),
            InlineKeyboardButton(text="➕ Новый проект", callback_data="add_project")
        )
        keyboard.row(
            InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
        )
        
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def show_projects_command(self, message: types.Message):
        """Обработка команды /projects"""
        class FakeCallback:
            def __init__(self, message):
                self.message = message
                self.data = "projects_list"
            
            async def answer(self):
                pass
        
        await self.show_projects_list(FakeCallback(message))
    
    async def handle_project_action(self, callback: types.CallbackQuery):
        """Обработка действий с проектом"""
        project_id = callback.data.replace("project_", "")
        
        project = storage.get_project(project_id)
        
        if not project:
            await callback.answer("❌ Проект не найден!")
            return
        
        paid = project['income']
        budget = project['budget']
        progress = (paid / budget * 100) if budget > 0 else 0
        archived = project.get('archived', 0)
        
        # Статус бар для прогресса
        progress_bar = "🟢" * int(progress / 20)
        progress_bar += "⚪" * (5 - int(progress / 20))
        
        # Получаем описание статуса
        status_text = project.get('status', 'Не указан')
        status_desc = ""
        for status in ProjectStatus:
            if status.value[0] == status_text:
                status_desc = status.value[1]
                break
        
        # Получаем платежи по проекту
        payments = storage.get_project_payments(project_id)
        
        text = f"""
🏢 *Детали проекта*

*Название:* {project.get('name', 'Без названия')}
*Описание:* {project.get('description', 'Нет описания')}
*Статус:* {status_text}
*{status_desc}*

📊 *Финансы:*
• Бюджет: {budget:,.2f} ₽
• Оплачено: {paid:,.2f} ₽
• Осталось: {budget - paid:,.2f} ₽
• Прогресс: {progress_bar} {progress:.1f}%

📅 *Даты:*
• Создан: {datetime.fromisoformat(project['created']).strftime('%d.%m.%Y')}
• Обновлен: {datetime.fromisoformat(project['last_updated']).strftime('%d.%m.%Y %H:%M')}
        """
        
        if archived:
            archived_at = project.get('archived_at')
            if archived_at:
                archived_date = datetime.fromisoformat(archived_at).strftime('%d.%m.%Y %H:%M')
                text += f"• Архивирован: {archived_date}\n"
        
        text += f"\n💳 *Платежи:* {len(payments)}"
        
        keyboard = InlineKeyboardBuilder()
        
        # Кнопки для архивных и неархивных проектов
        if archived:
            keyboard.row(
                InlineKeyboardButton(text="📁 Восстановить", callback_data=f"unarchive_{project_id}"),
            )
        else:
            keyboard.row(
                InlineKeyboardButton(text="📁 В архив", callback_data=f"archive_{project_id}"),
            )
        
        keyboard.row(
            InlineKeyboardButton(text="💳 Добавить оплату", callback_data=f"payment_{project_id}"),
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_{project_id}")
        )
        keyboard.row(
            InlineKeyboardButton(text="🔄 Изменить статус", callback_data=f"status_{project_id}"),
            InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"delete_{project_id}")
        )
        
        if archived:
            keyboard.row(
                InlineKeyboardButton(text="📁 Архив проектов", callback_data="projects_archive"),
                InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
            )
        else:
            keyboard.row(
                InlineKeyboardButton(text="📋 Список проектов", callback_data="projects_list"),
                InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
            )
        
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def handle_archive_project(self, callback: types.CallbackQuery):
        """Архивировать проект"""
        project_id = callback.data.replace("archive_", "")
        
        success = storage.archive_project(project_id)
        
        if not success:
            await callback.answer("❌ Ошибка при архивации проекта!")
            return
        
        await callback.answer("✅ Проект перемещен в архив!")
        await self.handle_project_action(callback)
    
    async def handle_unarchive_project(self, callback: types.CallbackQuery):
        """Восстановить проект из архива"""
        project_id = callback.data.replace("unarchive_", "")
        
        success = storage.unarchive_project(project_id)
        
        if not success:
            await callback.answer("❌ Ошибка при восстановлении проекта!")
            return
        
        await callback.answer("✅ Проект восстановлен из архива!")
        await self.handle_project_action(callback)
    
    async def handle_edit_action(self, callback: types.CallbackQuery):
        """Обработка редактирования проекта"""
        project_id = callback.data.replace("edit_", "")
        
        project = storage.get_project(project_id)
        
        if not project:
            await callback.answer("❌ Проект не найден!")
            return
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="✏️ Название", callback_data=f"edit_name_{project_id}"),
            InlineKeyboardButton(text="📝 Описание", callback_data=f"edit_desc_{project_id}")
        )
        keyboard.row(
            InlineKeyboardButton(text="💰 Бюджет", callback_data=f"edit_budget_{project_id}"),
            InlineKeyboardButton(text="📋 Статус", callback_data=f"status_{project_id}")
        )
        keyboard.row(
            InlineKeyboardButton(text="↩️ Назад", callback_data=f"project_{project_id}")
        )
        
        await callback.message.edit_text(
            "✏️ *Что вы хотите отредактировать?*",
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def handle_payment_action(self, callback: types.CallbackQuery, state: FSMContext):
        """Обработка добавления платежа"""
        project_id = callback.data.replace("payment_", "")
        
        project = storage.get_project(project_id)
        
        if not project:
            await callback.answer("❌ Проект не найден!")
            return
        
        await state.update_data(project_id=project_id)
        await callback.message.answer(
            "💰 *Введите сумму платежа:*",
            parse_mode="Markdown"
        )
        await state.set_state(ProjectStates.waiting_for_payment_amount)
        await callback.answer()
    
    async def process_payment_amount(self, message: types.Message, state: FSMContext):
        """Обработка суммы платежа"""
        try:
            amount = float(message.text.replace(',', '.'))
            data = await state.get_data()
            project_id = data['project_id']
            
            project = storage.get_project(project_id)
            if not project:
                await message.answer("❌ Проект не найден!")
                await state.clear()
                return
            
            current_paid = project['income']
            
            if current_paid + amount > project['budget']:
                await message.answer(
                    f"⚠️ *Внимание!* Сумма превышает бюджет проекта.\n"
                    f"Бюджет: {project['budget']:,.2f} ₽\n"
                    f"Уже оплачено: {current_paid:,.2f} ₽\n"
                    f"Максимально можно добавить: {project['budget'] - current_paid:,.2f} ₽\n\n"
                    f"*Введите корректную сумму:*",
                    parse_mode="Markdown"
                )
                return
            
            await state.update_data(payment_amount=amount)
            await message.answer(
                "📝 *Введите описание платежа:*\n"
                "Например: 'Аванс 30%' или 'Оплата за этап 1'",
                parse_mode="Markdown"
            )
            await state.set_state(ProjectStates.waiting_for_payment_description)
            
        except ValueError:
            await message.answer("❌ Пожалуйста, введите корректную сумму!")
    
    async def process_payment_description(self, message: types.Message, state: FSMContext):
        """Обработка описания платежа"""
        data = await state.get_data()
        project_id = data['project_id']
        amount = data['payment_amount']
        
        project = storage.get_project(project_id)
        if not project:
            await message.answer("❌ Проект не найден!")
            await state.clear()
            return
        
        # Добавляем платеж
        payment_data = {
            'project_id': project_id,
            'amount': amount,
            'description': message.text,
            'date': datetime.now().isoformat(),
            'tax': amount * TAX_RATE,
            'profit': amount * PROFIT_RATE
        }
        
        success = storage.add_project_payment(payment_data)
        
        if not success:
            await message.answer("❌ Ошибка при добавлении платежа!")
            await state.clear()
            return
        
        # Обновляем проект чтобы получить актуальные данные
        updated_project = storage.get_project(project_id)
        
        # Автоматически обновляем статус проекта
        new_status = updated_project['status']
        if updated_project['income'] >= updated_project['budget']:
            new_status = ProjectStatus.COMPLETED.value[0]
            storage.update_project(project_id, {'status': new_status})
        elif updated_project['income'] > 0 and updated_project['status'] == ProjectStatus.PLANNED.value[0]:
            new_status = ProjectStatus.IN_PROGRESS.value[0]
            storage.update_project(project_id, {'status': new_status})
        
        # Добавляем запись о доходе
        income_data = {
            'amount': amount,
            'tax': amount * TAX_RATE,
            'profit': amount * PROFIT_RATE,
            'description': f"Оплата по проекту: {message.text}",
            'project_id': project_id,
            'project_name': project['name'],
            'date': datetime.now().isoformat(),
            'user_id': message.from_user.id
        }
        storage.add_income(income_data)
        
        # Формируем ответ
        current_paid = updated_project['income']
        progress = (current_paid / updated_project['budget'] * 100) if updated_project['budget'] > 0 else 0
        
        result_text = f"""
✅ *Платеж успешно добавлен!*

📊 *Детали платежа:*
• Сумма: {amount:,.2f} ₽
• Налог (15%): {amount * TAX_RATE:,.2f} ₽
• Прибыль (85%): {amount * PROFIT_RATE:,.2f} ₽
• Описание: {message.text}

🏢 *Статус проекта:*
• Оплачено всего: {current_paid:,.2f} ₽ из {updated_project['budget']:,.2f} ₽
• Прогресс: {progress:.1f}%
• Статус: {new_status}
        """
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="💳 Добавить еще платеж", callback_data=f"payment_{project_id}"),
            InlineKeyboardButton(text="🔍 К проекту", callback_data=f"project_{project_id}")
        )
        keyboard.row(
            InlineKeyboardButton(text="📋 Список проектов", callback_data="projects_list"),
            InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
        )
        
        await message.answer(
            result_text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        
        await state.clear()
    
    async def process_project_edit(self, message: types.Message, state: FSMContext):
        """Обработка редактирования проекта"""
        data = await state.get_data()
        project_id = data.get('project_id')
        edit_type = data.get('edit_type')
        
        if not project_id:
            await message.answer("❌ Проект не найден!")
            await state.clear()
            return
        
        project = storage.get_project(project_id)
        if not project:
            await message.answer("❌ Проект не найден!")
            await state.clear()
            return
        
        update_data = {'last_updated': datetime.now().isoformat()}
        
        if edit_type == 'name':
            update_data['name'] = message.text
            update_text = "название"
        elif edit_type == 'description':
            update_data['description'] = message.text
            update_text = "описание"
        elif edit_type == 'budget':
            try:
                new_budget = float(message.text.replace(',', '.'))
                update_data['budget'] = new_budget
                update_text = "бюджет"
            except ValueError:
                await message.answer("❌ Пожалуйста, введите корректную сумму!")
                return
        else:
            await message.answer("❌ Неизвестный тип редактирования!")
            await state.clear()
            return
        
        success = storage.update_project(project_id, update_data)
        
        if not success:
            await message.answer("❌ Ошибка при обновлении проекта!")
            await state.clear()
            return
        
        await message.answer(
            f"✅ {update_text.capitalize()} проекта обновлено!",
            parse_mode="Markdown"
        )
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="🔍 К проекту", callback_data=f"project_{project_id}"),
            InlineKeyboardButton(text="📋 Список проектов", callback_data="projects_list")
        )
        
        await message.answer(
            "Выберите действие:",
            reply_markup=keyboard.as_markup()
        )
        
        await state.clear()
    
    async def handle_status_change(self, callback: types.CallbackQuery):
        """Обработка изменения статуса проекта"""
        if callback.data.startswith("status_"):
            project_id = callback.data.replace("status_", "")
            
            project = storage.get_project(project_id)
            
            if not project:
                await callback.answer("❌ Проект не найден!")
                return
            
            # Показать меню выбора статуса
            keyboard = InlineKeyboardBuilder()
            for status in ProjectStatus:
                keyboard.row(
                    InlineKeyboardButton(
                        text=status.value[0],
                        callback_data=f"setstatus_{project_id}_{status.name}"
                    )
                )
            keyboard.row(
                InlineKeyboardButton(text="↩️ Назад", callback_data=f"project_{project_id}")
            )
            
            await callback.message.edit_text(
                "🔄 *Выберите новый статус проекта:*",
                parse_mode="Markdown",
                reply_markup=keyboard.as_markup()
            )
        
        elif callback.data.startswith("setstatus_"):
            parts = callback.data.split("_")
            if len(parts) < 3:
                await callback.answer("❌ Ошибка!")
                return
            
            project_id = parts[1]
            status_name = parts[2]
            
            project = storage.get_project(project_id)
            
            if not project:
                await callback.answer("❌ Проект не найден!")
                return
            
            try:
                status = ProjectStatus[status_name]
                update_data = {
                    'status': status.value[0],
                    'last_updated': datetime.now().isoformat()
                }
                
                success = storage.update_project(project_id, update_data)
                
                if success:
                    await callback.answer(f"✅ Статус изменен на: {status.value[0]}")
                    await self.handle_project_action(callback)
                else:
                    await callback.answer("❌ Ошибка при обновлении статуса!")
                
            except KeyError:
                await callback.answer("❌ Неверный статус!")
        
        await callback.answer()
    
    async def handle_delete_project(self, callback: types.CallbackQuery):
        """Обработка удаления проекта"""
        project_id = callback.data.replace("delete_", "")
        
        project = storage.get_project(project_id)
        
        if not project:
            await callback.answer("❌ Проект не найден!")
            return
        
        project_name = project.get('name', 'Проект')
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(
                text="✅ Да, удалить",
                callback_data=f"confirm_delete_{project_id}"
            ),
            InlineKeyboardButton(
                text="❌ Нет, отмена",
                callback_data=f"project_{project_id}"
            )
        )
        
        await callback.message.edit_text(
            f"⚠️ *Вы уверены, что хотите удалить проект?*\n\n"
            f"*{project_name}*\n\n"
            f"*Это действие нельзя отменить!*",
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def confirm_delete_project(self, callback: types.CallbackQuery):
        """Подтверждение удаления проекта"""
        project_id = callback.data.replace("confirm_delete_", "")
        
        success = storage.delete_project(project_id)
        
        if not success:
            await callback.answer("❌ Ошибка при удалении проекта!")
            return
        
        await callback.message.edit_text(
            f'🗑️ *Проект успешно удален!*',
            parse_mode="Markdown"
        )
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="📋 Список проектов", callback_data="projects_list"),
            InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
        )
        
        await callback.message.answer(
            "Выберите действие:",
            reply_markup=keyboard.as_markup()
        )
        
        await callback.answer()
    
    # ========== АНАЛИТИКА ==========
    
    async def show_analytics_menu(self, callback: types.CallbackQuery):
        """Показать меню аналитики"""
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="📅 За сегодня", callback_data="analytics_today"),
            InlineKeyboardButton(text="📅 За неделю", callback_data="analytics_week")
        )
        keyboard.row(
            InlineKeyboardButton(text="📅 За месяц", callback_data="analytics_month"),
            InlineKeyboardButton(text="📅 За год", callback_data="analytics_year")
        )
        keyboard.row(
            InlineKeyboardButton(text="📊 Общая статистика", callback_data="analytics_total"),
            InlineKeyboardButton(text="💳 Долги", callback_data="debts_menu")
        )
        keyboard.row(
            InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
        )
        
        await callback.message.edit_text(
            "📊 *Меню аналитики*\nВыберите период:",
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def show_quick_stats(self, callback: types.CallbackQuery):
        """Показать быструю статистику"""
        # Статистика по доходам
        income_stats = storage.get_total_income_stats()
        
        # Статистика по проектам
        projects = storage.get_all_projects(include_archived=False)
        archived_projects = storage.get_archived_projects()
        
        active_projects = len([p for p in projects 
                              if p.get('status') != ProjectStatus.COMPLETED.value[0] 
                              and p.get('status') != ProjectStatus.CANCELLED.value[0]])
        
        completed_projects = len([p for p in projects 
                                 if p.get('status') == ProjectStatus.COMPLETED.value[0]])
        
        # Статистика по долгам
        debt_stats = storage.get_debt_stats()
        
        # Доход за последние 7 дней
        week_ago = datetime.now() - timedelta(days=7)
        weekly_incomes = storage.get_incomes(week_ago.isoformat())
        weekly_income = sum(inc['amount'] for inc in weekly_incomes)
        
        stats_text = f"""
⚡ *Быстрая статистика*

💰 *Финансы:*
• Всего доходов: {income_stats['count']}
• Общий доход: {income_stats['total_amount']:,.2f} ₽
• Налоги (15%): {income_stats['total_tax']:,.2f} ₽
• Чистая прибыль (85%): {income_stats['total_profit']:,.2f} ₽
• Доход за неделю: {weekly_income:,.2f} ₽

🏢 *Проекты:*
• Активных проектов: {len(projects)}
• В работе: {active_projects}
• Завершено: {completed_projects}
• В архиве: {len(archived_projects)}

💳 *Долги:*
• Вам должны: {debt_stats['gave_total']:,.2f} ₽ ({debt_stats['gave_count']} долгов)
• Вы должны: {debt_stats['took_total']:,.2f} ₽ ({debt_stats['took_count']} долгов)
• Чистый баланс: {debt_stats['gave_total'] - debt_stats['took_total']:,.2f} ₽
        """
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="📊 Подробная аналитика", callback_data="analytics"),
            InlineKeyboardButton(text="💳 Управление долгами", callback_data="debts_menu")
        )
        keyboard.row(
            InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
        )
        
        await callback.message.answer(
            stats_text,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    async def show_quick_stats_command(self, message: types.Message):
        """Обработка команды /stats"""
        class FakeCallback:
            def __init__(self, message):
                self.message = message
                self.data = "quick_stats"
            
            async def answer(self):
                pass
        
        await self.show_quick_stats(FakeCallback(message))
    
    async def handle_analytics(self, callback: types.CallbackQuery):
        """Обработка аналитики"""
        period = callback.data.replace("analytics_", "")
        
        now = datetime.now()
        
        if period == "today":
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            period_text = "сегодня"
        elif period == "week":
            start_date = now - timedelta(days=7)
            period_text = "за неделю"
        elif period == "month":
            start_date = now - timedelta(days=30)
            period_text = "за месяц"
        elif period == "year":
            start_date = now - timedelta(days=365)
            period_text = "за год"
        elif period == "total":
            start_date = None
            period_text = "за все время"
        else:
            await callback.answer("❌ Неверный период!")
            return
        
        # Фильтруем доходы по периоду
        filtered_incomes = storage.get_incomes(
            start_date.isoformat() if start_date else None,
            now.isoformat()
        )
        
        # Получаем все проекты для статистики
        projects = storage.get_all_projects(include_archived=False)
        archived_projects = storage.get_archived_projects()
        
        # Статистика по долгам (за все время, так как создание долгов не частые)
        debt_stats = storage.get_debt_stats()
        
        if not filtered_incomes and not projects and debt_stats['gave_count'] == 0 and debt_stats['took_count'] == 0:
            await callback.message.answer(
                f"📭 *Нет данных {period_text}*",
                parse_mode="Markdown"
            )
            await callback.answer()
            return
        
        # Рассчитываем статистику
        total_income = sum(inc['amount'] for inc in filtered_incomes)
        total_tax = sum(inc['tax'] for inc in filtered_incomes)
        total_profit = sum(inc['profit'] for inc in filtered_incomes)
        avg_income = total_income / len(filtered_incomes) if filtered_incomes else 0
        
        # Статистика по проектам
        project_stats = {}
        for inc in filtered_incomes:
            if inc.get('project_id'):
                project_name = inc.get('project_name', 'Без названия')
                if project_name not in project_stats:
                    project_stats[project_name] = 0
                project_stats[project_name] += inc['amount']
        
        # Формируем отчет
        report = f"""
📊 *Аналитика {period_text}*

📈 *Общая статистика:*
• Всего операций: {len(filtered_incomes)}
• Общий доход: {total_income:,.2f} ₽
• Средний чек: {avg_income:,.2f} ₽
• Налоги: {total_tax:,.2f} ₽
• Чистая прибыль: {total_profit:,.2f} ₽

🏢 *Проекты:*
• Активных проектов: {len(projects)}
• В архиве: {len(archived_projects)}

💳 *Долги (общая статистика):*
• Вам должны: {debt_stats['gave_total']:,.2f} ₽
• Вы должны: {debt_stats['took_total']:,.2f} ₽
• Чистый баланс: {debt_stats['gave_total'] - debt_stats['took_total']:,.2f} ₽
        """
        
        # Добавляем топ-5 проектов
        if project_stats:
            report += "\n\n🏆 *Топ проектов по доходу:*"
            sorted_projects = sorted(project_stats.items(), key=lambda x: x[1], reverse=True)[:5]
            for project_name, amount in sorted_projects:
                percentage = (amount / total_income * 100) if total_income > 0 else 0
                report += f"\n• {project_name}: {amount:,.2f} ₽ ({percentage:.1f}%)"
        
        # Добавляем последние операции
        if filtered_incomes:
            report += "\n\n📅 *Последние операции:*"
            recent = filtered_incomes[:3]
            for inc in recent:
                date = datetime.fromisoformat(inc['date']).strftime('%d.%m %H:%M')
                report += f"\n• {date}: {inc['amount']:,.2f} ₽ - {inc.get('description', '')[:25]}"
        
        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            InlineKeyboardButton(text="📅 За другой период", callback_data="analytics"),
            InlineKeyboardButton(text="💳 Долги", callback_data="debts_menu")
        )
        keyboard.row(
            InlineKeyboardButton(text="📱 В меню", callback_data="main_menu")
        )
        
        await callback.message.answer(
            report,
            parse_mode="Markdown",
            reply_markup=keyboard.as_markup()
        )
        await callback.answer()
    
    # ========== ОБРАБОТКА ТЕКСТОВЫХ СООБЩЕНИЙ ==========
    
    async def handle_text_messages(self, message: types.Message):
        """Обработка текстовых сообщений"""
        if message.text.startswith('/'):
            return
        
        # Обновляем активность пользователя
        storage.update_user_last_active(message.from_user.id)
        
        # Простая обработка быстрых команд
        text_lower = message.text.lower()
        
        if any(word in text_lower for word in ['привет', 'hello', 'start', 'начать']):
            await self.start_command(message)
        elif any(word in text_lower for word in ['меню', 'menu']):
            await self.show_main_menu(message)
        elif any(word in text_lower for word in ['статистика', 'stats', 'аналитика']):
            class FakeCallback:
                def __init__(self, message):
                    self.message = message
                    self.data = "quick_stats"
                
                async def answer(self):
                    pass
            
            await self.show_quick_stats(FakeCallback(message))
        elif any(word in text_lower for word in ['проекты', 'projects']):
            class FakeCallback:
                def __init__(self, message):
                    self.message = message
                    self.data = "projects_list"
                
                async def answer(self):
                    pass
            
            await self.show_projects_list(FakeCallback(message))
        elif any(word in text_lower for word in ['долги', 'долг', 'debt']):
            class FakeCallback:
                def __init__(self, message):
                    self.message = message
                    self.data = "debts_menu"
                
                async def answer(self):
                    pass
            
            await self.show_debts_menu(FakeCallback(message))
        elif any(word in text_lower for word in ['экспорт', 'export']):
            await self.export_data_command(message)
        elif any(word in text_lower for word in ['импорт', 'import']):
            await self.import_data_start(message, FSMContext)
        else:
            await message.answer(
                "🤖 *Я не понял ваше сообщение*\n\n"
                "Используйте команды:\n"
                "/menu - Главное меню\n"
                "/help - Помощь\n"
                "/stats - Быстрая статистика\n"
                "/projects - Список проектов\n"
                "/debts - Управление долгами\n"
                "/export - Экспорт данных\n"
                "/import - Импорт данных",
                parse_mode="Markdown"
            )
    
    async def run(self):
        """Запуск бота"""
        await self.dp.start_polling(self.bot)

# Точка входа
async def main():
    """Основная функция запуска бота"""
    
    # Проверяем наличие токена
    if not TOKEN:
        logger.error("❌ Токен бота не найден!")
        logger.info("Добавьте TELEGRAM_BOT_TOKEN в файл .env")
        return
    
    # Информация о конфигурации
    logger.info(f"🚀 Запуск Finance Bot")
    logger.info(f"📊 Конфигурация:")
    logger.info(f"   • База данных: {DB_PATH}")
    logger.info(f"   • Налоговая ставка: {TAX_RATE*100}%")
    logger.info(f"   • Прибыль: {PROFIT_RATE*100}%")
    logger.info(f"   • Уровень логирования: {log_level}")
    
    try:
        # Создаем и запускаем бота
        bot = FinanceBot(TOKEN)
        logger.info("✅ Бот успешно запущен")
        await bot.run()
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске бота: {e}")
    finally:
        # Закрываем соединение с БД
        storage.close()
        logger.info("👋 Бот остановлен")

if __name__ == "__main__":
    # Запускаем бота
    asyncio.run(main())
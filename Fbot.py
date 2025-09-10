import os
import shutil
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Конфигурация
BOT_TOKEN = "8358573316:AAGl57ICMBIROD9tbXPHfbtJHJcTh1sSkbc"
ADMIN_IDS = [336076029, 1497957467]  # ID админов
GROUP_ID = -4852605357  # ID группы
BASE_DIR = Path(__file__).parent.resolve()
DB_NAME = BASE_DIR / "finance_bot.db"
BACKUP_DIR = BASE_DIR / "backups/"
REPORT_INTERVAL_DAYS = 10  # Интервал автоотчетов в днях

# Стандартные категории расходов
DEFAULT_CATEGORIES = {
    "outsource_salary": "Зарплаты сотрудников аутсорса",
    "staff_salary": "Зарплаты штата", 
    "office": "Офис, канцелярия, материалы",
    "legal": "Расходы юр. лиц",
    "other": "Прочее"
}

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "finance_bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Роутеры
user_router = Router()
admin_router = Router()

# Фильтр для админов
admin_filter = (F.from_user.id.in_(ADMIN_IDS)) | (F.chat.id.in_(ADMIN_IDS))

# Состояния FSM
class IncomeStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_comment = State()

class ExpenseStates(StatesGroup):
    waiting_for_category = State()
    waiting_for_amount = State()
    waiting_for_comment = State()

class EmployeeStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_birthday = State()

# Database класс
class Database:
    def __init__(self, db_name: str = DB_NAME):
        self.db_name = db_name
        self.conn = None
        self._connect()
        self._create_tables()
        self._init_default_categories()

    def _connect(self):
        try:
            self.conn = sqlite3.connect(self.db_name, check_same_thread=False)
            self.conn.execute("PRAGMA foreign_keys = ON")
            logger.info("Database connection established")
        except sqlite3.Error as e:
            logger.error(f"Error connecting to database: {e}")
            raise

    def _create_tables(self):
        try:
            cursor = self.conn.cursor()
            
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_interval_days INTEGER DEFAULT 10,
                last_report_date TEXT
            )
            """)
            
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS expense_categories (
                name TEXT PRIMARY KEY,
                display_name TEXT NOT NULL
            )
            """)
            
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS incomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                amount REAL NOT NULL,
                comment TEXT,
                date TEXT NOT NULL,
                confirmed INTEGER DEFAULT 0 CHECK (confirmed IN (0, 1))
            )
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                category TEXT NOT NULL,
                amount REAL NOT NULL,
                comment TEXT,
                date TEXT NOT NULL,
                confirmed INTEGER DEFAULT 0
            )
            """)
            
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                birthday TEXT NOT NULL,
                added_date TEXT NOT NULL
            )
            """)
            
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_type TEXT NOT NULL,
                operation_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                UNIQUE(operation_type, operation_id, chat_id)
            )
            """)
            
            self.conn.commit()
            logger.info("Tables created successfully")
        except sqlite3.Error as e:
            logger.error(f"Error creating tables: {e}")
            raise

    def _init_default_categories(self):
        try:
            cursor = self.conn.cursor()
            for key, value in DEFAULT_CATEGORIES.items():
                cursor.execute(
                    "INSERT OR IGNORE INTO expense_categories (name, display_name) VALUES (?, ?)",
                    (key, value)
                )
            self.conn.commit()
            logger.info("Default categories initialized")
        except sqlite3.Error as e:
            logger.error(f"Error initializing default categories: {e}")
            raise

    async def add_income(self, user_id: int, user_name: str, amount: float, comment: str) -> Optional[int]:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO incomes (user_id, user_name, amount, comment, date) VALUES (?, ?, ?, ?, ?)",
                (user_id, user_name, amount, comment, datetime.now().isoformat())
            )
            self.conn.commit()
            logger.info(f"Income added: user_id={user_id}, amount={amount}")
            return cursor.lastrowid
        except sqlite3.Error as e:
            logger.error(f"Error adding income: {e}")
            return None

    async def confirm_income(self, income_id: int) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE incomes SET confirmed = 1 WHERE id = ?",
                (income_id,)
            )
            self.conn.commit()
            if cursor.rowcount > 0:
                logger.info(f"Income confirmed: id={income_id}")
                return True
            return False
        except sqlite3.Error as e:
            logger.error(f"Error confirming income: {e}")
            return False

    async def reject_income(self, income_id: int) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "DELETE FROM incomes WHERE id = ? AND confirmed = 0",
                (income_id,)
            )
            self.conn.commit()
            if cursor.rowcount > 0:
                logger.info(f"Income rejected: id={income_id}")
                return True
            return False
        except sqlite3.Error as e:
            logger.error(f"Error rejecting income: {e}")
            return False
    
    async def delete_expense(self, expense_id: int) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "DELETE FROM expenses WHERE id = ?",
                (expense_id,)
            )
            self.conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error deleting expense: {e}")
            return False

    async def add_expense(self, user_id: int, user_name: str, category: str, 
                          amount: float, comment: str = "") -> Optional[int]:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO expenses (user_id, user_name, category, amount, comment, date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, user_name, category, amount, comment, datetime.now().isoformat())
            )   
            self.conn.commit()
            logger.info(f"Expense added: user={user_name}, category={category}, amount={amount}")
            return cursor.lastrowid
        except sqlite3.Error as e:
            logger.error(f"Error adding expense: {e}")
            return None
    
    async def confirm_expense(self, expense_id: int) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE expenses SET confirmed = 1 WHERE id = ?",
                (expense_id,)
            )
            self.conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error confirming expense: {e}")
            return False

    async def add_employee(self, full_name: str, phone: str, birthday: str) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO employees (full_name, phone, birthday, added_date) VALUES (?, ?, ?, ?)",
                (full_name, phone, birthday, datetime.now().isoformat())
            )
            self.conn.commit()
            logger.info(f"Employee added: {full_name}")
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adding employee: {e}")
            return False

    async def get_employees(self) -> List[Dict]:
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT full_name, phone, birthday FROM employees ORDER BY full_name")
            employees = [
                {"full_name": row[0], "phone": row[1], "birthday": row[2]}
                for row in cursor.fetchall()
            ]
            logger.info(f"Retrieved {len(employees)} employees")
            return employees
        except sqlite3.Error as e:
            logger.error(f"Error getting employees: {e}")
            return []

    async def get_categories(self) -> Dict[str, str]:
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT name, display_name FROM expense_categories")
            categories = {row[0]: row[1] for row in cursor.fetchall()}
            logger.info(f"Retrieved {len(categories)} categories")
            return categories
        except sqlite3.Error as e:
            logger.error(f"Error getting categories: {e}")
            return {}

    async def get_history(self, period: str = "month") -> Tuple[List[Dict], List[Dict]]:
        try:
            now = datetime.now()
            if period == "day":
                start_date = now - timedelta(days=1)
            elif period == "week":
                start_date = now - timedelta(weeks=1)
            else:  # month
                start_date = now - timedelta(days=30)
            
            cursor = self.conn.cursor()
            
            # Доходы
            cursor.execute(
                "SELECT user_name, amount, comment, date FROM incomes WHERE date >= ? AND confirmed = 1 ORDER BY date DESC",
                (start_date.isoformat(),)
            )
            incomes = [
                {"type": "income", "user": row[0], "amount": row[1], "comment": row[2], "date": row[3]}
                for row in cursor.fetchall()
            ]
            
            # Расходы
            cursor.execute(
                "SELECT user_name, category, amount, date FROM expenses WHERE date >= ? ORDER BY date DESC",
                (start_date.isoformat(),)
            )
            expenses = [
                {"type": "expense", "user": row[0], "category": row[1], "amount": row[2], "date": row[3]}
                for row in cursor.fetchall()
            ]
            
            logger.info(f"Retrieved history: {len(incomes)} incomes, {len(expenses)} expenses")
            return incomes, expenses
        except sqlite3.Error as e:
            logger.error(f"Error getting history: {e}")
            return [], []

    async def get_report_data(self, start_date: str, end_date: str) -> Dict:
        try:
            cursor = self.conn.cursor()
            
            cursor.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM incomes WHERE date BETWEEN ? AND ? AND confirmed = 1",
                (start_date, end_date)
            )
            total_income = cursor.fetchone()[0]
            
            cursor.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date BETWEEN ? AND ?",
                (start_date, end_date)
            )
            total_expense = cursor.fetchone()[0]
            
            cursor.execute(
                "SELECT COUNT(*) FROM employees WHERE added_date BETWEEN ? AND ?",
                (start_date, end_date)
            )
            new_employees = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM employees")
            total_employees = cursor.fetchone()[0]

            logger.info(f"Generated report data for period {start_date} to {end_date}")
            return {
                "total_income": total_income,
                "total_expense": total_expense,
                "new_employees": new_employees,
                "total_employees": total_employees,
            }
        except sqlite3.Error as e:
            logger.error(f"Error generating report data: {e}")
            return {
                "total_income": 0,
                "total_expense": 0,
                "new_employees": 0,
                "total_employees": 0,
            }

    async def backup(self) -> bool:
        try:
            if not os.path.exists(BACKUP_DIR):
                os.makedirs(BACKUP_DIR)
            
            backup_name = BACKUP_DIR / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            shutil.copy2(self.db_name, backup_name)
            
            backups = sorted(os.listdir(BACKUP_DIR))
            if len(backups) > 7:
                for old_backup in backups[:-7]:
                    os.remove(BACKUP_DIR / old_backup)
            
            logger.info(f"Database backup created: {backup_name}")
            return True
        except Exception as e:
            logger.error(f"Error creating backup: {e}")
            return False

    async def get_report_interval(self) -> int:
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT report_interval_days FROM bot_settings WHERE id = 1")
            result = cursor.fetchone()
            if result:
                return result[0]
            
            # Если нет записи, создаем дефолтную
            cursor.execute(
                "INSERT INTO bot_settings (report_interval_days) VALUES (?)",
                (REPORT_INTERVAL_DAYS,)
            )
            self.conn.commit()
            return REPORT_INTERVAL_DAYS
        except sqlite3.Error as e:
            logger.error(f"Error getting report interval: {e}")
            return REPORT_INTERVAL_DAYS

    async def set_report_interval(self, days: int) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE bot_settings SET report_interval_days = ? WHERE id = 1",
                (days,)
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "INSERT INTO bot_settings (report_interval_days) VALUES (?)",
                    (days,)
                )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error setting report interval: {e}")
            return False

    async def add_admin_message(self, operation_type: str, operation_id: int, chat_id: int, message_id: int):
        """Сохраняет сообщение админу о необходимости подтверждения"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO admin_messages (operation_type, operation_id, chat_id, message_id)
                VALUES (?, ?, ?, ?)
            """, (operation_type, operation_id, chat_id, message_id))
            self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Error saving admin message: {e}")

    async def get_operation_messages(self, operation_type: str, operation_id: int) -> List[Dict]:
        """Получает все сообщения админов по конкретной операции"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT chat_id, message_id FROM admin_messages 
                WHERE operation_type = ? AND operation_id = ?
            """, (operation_type, operation_id))
            return [{"chat_id": row[0], "message_id": row[1]} for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting operation messages: {e}")
            return []

    async def clear_operation_messages(self, operation_type: str, operation_id: int):
        """Очищает сохраненные сообщения об операции"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                DELETE FROM admin_messages 
                WHERE operation_type = ? AND operation_id = ?
            """, (operation_type, operation_id))
            self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Error clearing operation messages: {e}")

    def close(self):
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed")

# Инициализация БД
db = Database()

# Декоратор для обработки ошибок
def error_handler(func):
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in handler {func.__name__}: {e}", exc_info=True)
            update = args[0] if args else None
            if isinstance(update, (Message, CallbackQuery)):
                try:
                    await update.answer("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")
                except:
                    pass
    return wrapper

# Хэндлеры для пользователей
@user_router.message(Command("start"))
@error_handler
async def cmd_start(message: Message, state: FSMContext, **kwargs):
    await state.clear()
    await message.answer(
        "Привет! Я бот для учета финансов.\n\n"
        "Доступные команды:\n"
        "/income - добавить доход\n"
        "/expense - добавить расход\n" 
        "/history - история операций"
    )

@user_router.message(Command("income"))
@error_handler
async def cmd_income(message: Message, state: FSMContext, **kwargs):
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_income"))
    
    await message.answer(
        "Введите сумму дохода (только число):",
        reply_markup=builder.as_markup()
    )
    await state.set_state(IncomeStates.waiting_for_amount)

@user_router.message(IncomeStates.waiting_for_amount)
@error_handler
async def process_income_amount(message: Message, state: FSMContext, **kwargs):
    if message.text.lower() in ["отмена", "/cancel"]:
        await state.clear()
        await message.answer("❌ Добавление дохода отменено")
        return
    
    try:
        amount = float(message.text.replace(",", "."))
        await state.update_data(amount=amount)
        
        builder = InlineKeyboardBuilder()
        builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_income"))
        
        await message.answer(
            "Введите комментарий к доходу:",
            reply_markup=builder.as_markup()
        )
        await state.set_state(IncomeStates.waiting_for_comment)
    except ValueError:
        await message.answer("Некорректная сумма. Введите число или 'Отмена'")

@user_router.message(IncomeStates.waiting_for_comment)
@error_handler
async def process_income_comment(message: Message, state: FSMContext, **kwargs):
    if message.text.lower() in ["отмена", "/cancel"]:
        await state.clear()
        await message.answer("❌ Добавление дохода отменено")
        return
    
    data = await state.get_data()
    income_id = await db.add_income(
        message.from_user.id,
        message.from_user.full_name,
        data["amount"],
        message.text
    )
    
    if income_id is None:
        await message.answer("❌ Не удалось добавить доход. Попробуйте позже.")
        await state.clear()
        return
    
    keyboard = InlineKeyboardBuilder()
    keyboard.add(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_income_{income_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_income_{income_id}"),
    )
    
    for admin_id in ADMIN_IDS:
        try:
            msg = await bot.send_message(
                chat_id=admin_id,
                text=(
                    f"Новый доход на подтверждение:\n"
                    f"Сумма: {data['amount']} руб.\n"
                    f"Комментарий: {message.text}\n"
                    f"Пользователь: {message.from_user.full_name}"
                ),
                reply_markup=keyboard.as_markup()
            )
            await db.add_admin_message("income", income_id, admin_id, msg.message_id)
        except Exception as e:
            logger.error(f"Error sending confirmation to admin {admin_id}: {e}")
    
    await message.answer("✅ Доход отправлен на подтверждение администратору.")
    await state.clear()

@user_router.message(Command("expense"))
@error_handler
async def cmd_expense(message: Message, state: FSMContext, **kwargs):
    categories = await db.get_categories()
    if not categories:
        await message.answer("❌ Нет доступных категорий расходов")
        return
    
    # Создаем клавиатуру с кнопками в один столбец
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=display_name, callback_data=f"expense_cat_{name}")]
            for name, display_name in categories.items()
        ]
    )
    # Добавляем кнопку отмены в отдельный ряд
    keyboard.inline_keyboard.append(
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_expense_action")]
    )
    
    await message.answer(
        "Выберите категорию расхода:",
        reply_markup=keyboard
    )
    await state.set_state(ExpenseStates.waiting_for_category)

@user_router.callback_query(F.data == "cancel_expense_action", ExpenseStates.waiting_for_category)
@error_handler
async def cancel_expense_category(callback: CallbackQuery, state: FSMContext, **kwargs):
    await state.clear()
    await callback.message.edit_text("❌ Добавление расхода отменено")
    await callback.answer()

@user_router.callback_query(F.data.startswith("expense_cat_"), ExpenseStates.waiting_for_category)
@error_handler
async def process_expense_category(callback: CallbackQuery, state: FSMContext, **kwargs):
    if callback.data == "cancel_expense_action":  # Добавьте эту проверку
        await state.clear()
        await callback.message.edit_text("❌ Добавление расхода отменено")
        await callback.answer()
        return
        
    category = callback.data.split("_")[2]
    await state.update_data(category=category)
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_expense_action")]]
    )
    
    await callback.message.edit_text(
        text="Введите сумму расхода (только число):",
        reply_markup=keyboard
    )
    await state.set_state(ExpenseStates.waiting_for_amount)
    await callback.answer()

@user_router.message(ExpenseStates.waiting_for_amount)
@error_handler
async def process_expense_amount(message: Message, state: FSMContext, **kwargs):
    if message.text.lower() in ["отмена", "/cancel"]:
        await state.clear()
        await message.answer("❌ Добавление расхода отменено")
        return
    
    try:
        amount = float(message.text.replace(",", "."))
        await state.update_data(amount=amount)
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_expense_action")]]
        )
        
        await message.answer(
            "Введите комментарий к расходу:",
            reply_markup=keyboard
        )
        await state.set_state(ExpenseStates.waiting_for_comment)
    except ValueError:
        await message.answer("Некорректная сумма. Введите число или напишите 'Отмена'")

@user_router.callback_query(F.data == "cancel_expense_action")
@error_handler
async def handle_expense_cancel(callback: CallbackQuery, state: FSMContext, **kwargs):
    await state.clear()
    await callback.message.edit_text("❌ Добавление расхода отменено")
    await callback.answer()

@user_router.message(ExpenseStates.waiting_for_comment)
@error_handler
async def process_expense_comment(message: Message, state: FSMContext, **kwargs):
    # Обработка команды отмены
    if message.text.lower() in ["отмена", "/cancel"]:
        await state.clear()
        await message.answer("❌ Добавление расхода отменено")
        return
    
    # Получаем сохраненные данные
    data = await state.get_data()
    
    # Добавляем расход в БД
    expense_id = await db.add_expense(
        user_id=message.from_user.id,
        user_name=message.from_user.full_name,
        category=data['category'],
        amount=data['amount'],
        comment=message.text
    )
    
    if expense_id is None:
        await message.answer("❌ Ошибка при сохранении расхода")
        await state.clear()
        return
    
    # Получаем название категории для отображения
    categories = await db.get_categories()
    category_name = categories.get(data['category'], data['category'])
    
    # Создаем клавиатуру для подтверждения
    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_expense_{expense_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_expense_{expense_id}")
    )
    
    # Отправляем запрос на подтверждение всем админам
    for admin_id in ADMIN_IDS:
        try:
            msg = await bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🔄 Запрос на подтверждение расхода:\n"
                    f"Категория: {category_name}\n"
                    f"Сумма: {data['amount']} руб.\n"
                    f"Комментарий: {message.text}\n"
                    f"Пользователь: {message.from_user.full_name}"
                ),
                reply_markup=keyboard.as_markup()
            )
            # Сохраняем сообщение для последующего обновления
            await db.add_admin_message("expense", expense_id, admin_id, msg.message_id)
        except Exception as e:
            logger.error(f"Error sending confirmation to admin {admin_id}: {e}")
    
    await message.answer("📤 Запрос на расход отправлен администратору")
    await state.clear()

@user_router.message(Command("history"))
@error_handler
async def cmd_history(message: Message, **kwargs):
    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="🕙 День", callback_data="history_day"),
        InlineKeyboardButton(text="🕛 Неделя", callback_data="history_week"), 
        InlineKeyboardButton(text="🕧 Месяц", callback_data="history_month")
    )
    keyboard.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_history"))

    await message.answer(
        "📊 Выберите период для просмотра истории операций:",
        reply_markup=keyboard.as_markup()
    )

@user_router.callback_query(F.data.startswith("history_"))
@error_handler
async def process_history_period(callback: CallbackQuery, **kwargs):
    period = callback.data.split("_")[1]
    incomes, expenses = await db.get_history(period)
    
    # Форматируем историю
    message_lines = ["📊 История операций:\n"]
    all_operations = []
    
    for income in incomes:
        dt = datetime.fromisoformat(income["date"])
        all_operations.append((
            dt,
            f"💰 Доход: {income['amount']} руб.\n"
            f"👤 {income['user']}\n"
            f"📝 {income['comment']}\n"
            f"🕒 {dt.strftime('%d.%m.%Y %H:%M')}\n"
        ))
    
    for expense in expenses:
        dt = datetime.fromisoformat(expense["date"])
        categories = await db.get_categories()
        category_name = categories.get(expense["category"], expense["category"])
        all_operations.append((
            dt,
            f"💸 Расход: {expense['amount']} руб. ({category_name})\n"
            f"👤 {expense['user']}\n"
            f"🕒 {dt.strftime('%d.%m.%Y %H:%M')}\n"
        ))
    
    all_operations.sort(reverse=True, key=lambda x: x[0])
    
    for op in all_operations:
        if len("\n".join(message_lines) + op[1]) < 4000:
            message_lines.append(op[1])
            message_lines.append("─" * 20 + "\n")
        else:
            message_lines.append("\n... показаны не все операции")
            break
    
    if not all_operations:
        message_lines.append("Нет операций за выбранный период")
    
    await callback.message.edit_text(
        text="".join(message_lines),
        reply_markup=None
    )
    await callback.answer()

@user_router.callback_query(F.data == "cancel_history")
@error_handler
async def cancel_history(callback: CallbackQuery, **kwargs):
    await callback.message.edit_text("❌ Просмотр истории отменен")
    await callback.answer()

@user_router.callback_query(F.data == "cancel_income")
@error_handler
async def cancel_income_handler(callback: CallbackQuery, state: FSMContext, **kwargs):
    await state.clear()
    await callback.message.edit_text("❌ Добавление дохода отменено")
    await callback.answer()

@user_router.message(Command("cancel"))
@error_handler
async def cmd_cancel(message: Message, state: FSMContext, **kwargs):
    current_state = await state.get_state()
    if current_state in IncomeStates.__states__:
        await state.clear()
        await message.answer("❌ Добавление дохода отменено")
    elif current_state in ExpenseStates.__states__:
        await state.clear()
        await message.answer("❌ Добавление расхода отменено")
    else:
        await message.answer("ℹ️ Нет активных операций для отмены")

# Хэндлеры для админов
@admin_router.message(Command("add_employee"), admin_filter)
@error_handler
async def cmd_add_employee(message: Message, state: FSMContext, **kwargs):
    await state.update_data(messages_to_delete=[message.message_id])
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_employee"))
    
    msg = await message.answer(
        "Введите ФИО нового сотрудника:",
        reply_markup=builder.as_markup()
    )
    
    data = await state.get_data()
    data["messages_to_delete"].append(msg.message_id)
    await state.set_data(data)
    
    await state.set_state(EmployeeStates.waiting_for_name)

async def delete_previous_messages(chat_id: int, message_ids: List[int]):
    try:
        for msg_id in message_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Ошибка при удалении сообщений: {e}")

@admin_router.message(EmployeeStates.waiting_for_name, admin_filter)
@error_handler
async def process_employee_name(message: Message, state: FSMContext, **kwargs):
    data = await state.get_data()
    await delete_previous_messages(message.chat.id, data.get("messages_to_delete", []))
    
    new_data = {
        "name": message.text,
        "messages_to_delete": [message.message_id]
    }
    await state.set_data(new_data)
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_employee"))
    
    msg = await message.answer(
        "Введите телефон сотрудника:",
        reply_markup=builder.as_markup()
    )
    
    new_data["messages_to_delete"].append(msg.message_id)
    await state.set_data(new_data)
    
    await state.set_state(EmployeeStates.waiting_for_phone)

@admin_router.message(EmployeeStates.waiting_for_phone, admin_filter)
@error_handler
async def process_employee_phone(message: Message, state: FSMContext, **kwargs):
    data = await state.get_data()
    await delete_previous_messages(message.chat.id, data.get("messages_to_delete", []))
    
    new_data = {
        "name": data["name"],
        "phone": message.text,
        "messages_to_delete": [message.message_id]
    }
    await state.set_data(new_data)
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_employee"))
    
    msg = await message.answer(
        "Введите дату рождения сотрудника (ДД.ММ.ГГГГ):",
        reply_markup=builder.as_markup()
    )
    
    new_data["messages_to_delete"].append(msg.message_id)
    await state.set_data(new_data)
    
    await state.set_state(EmployeeStates.waiting_for_birthday)

@admin_router.message(EmployeeStates.waiting_for_birthday, admin_filter)
@error_handler
async def process_employee_birthday(message: Message, state: FSMContext, **kwargs):
    try:
        data = await state.get_data()
        await delete_previous_messages(message.chat.id, data.get("messages_to_delete", []))
        
        birthday = datetime.strptime(message.text, "%d.%m.%Y").date()
        
        if await db.add_employee(
            data["name"],
            data["phone"],
            birthday.strftime("%d.%m.%Y")
        ):
            try:
                await message.delete()
            except:
                pass
            
            await message.answer(
                f"✅ Сотрудник успешно добавлен:\n"
                f"ФИО: {data['name']}\n"
                f"Телефон: {data['phone']}\n"
                f"Дата рождения: {birthday.strftime('%d.%m.%Y')}"
            )
            
            await bot.send_message(
                chat_id=GROUP_ID,
                text=f"👤 Новый сотрудник:\n"
                     f"ФИО: {data['name']}\n"
                     f"Телефон: {data['phone']}\n"
                     f"Дата рождения: {birthday.strftime('%d.%m.%Y')}"
            )
        else:
            await message.answer("❌ Ошибка при добавлении сотрудника")
        
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Неверный формат даты. Введите ДД.ММ.ГГГГ")
        await state.update_data(messages_to_delete=[message.message_id])

@admin_router.callback_query(F.data == "cancel_employee")
@error_handler
async def cancel_employee(callback: CallbackQuery, state: FSMContext, **kwargs):
    data = await state.get_data()
    await delete_previous_messages(callback.message.chat.id, data.get("messages_to_delete", []))
    
    await callback.message.answer("❌ Добавление сотрудника отменено")
    await state.clear()
    await callback.answer()

@admin_router.message(Command("employees"))
@error_handler
async def cmd_employees(message: Message, **kwargs):
    employees = await db.get_employees()
    if not employees:
        await message.answer("Список сотрудников пуст")
        return
    
    text = "📝 Список сотрудников:\n\n"
    for employee in employees:
        text += (
            f"• {employee['full_name']}\n"
            f"  📞 {employee['phone']}\n"
            f"  🎂 {employee['birthday']}\n\n"
        )
    
    await message.answer(text[:4000])

# Обработчики подтверждения/отклонения операций
@admin_router.callback_query(F.data.startswith("confirm_income_"))
@error_handler
async def confirm_income(callback: CallbackQuery, **kwargs):
    income_id = int(callback.data.split("_")[2])
    
    if await db.confirm_income(income_id):
        # Получаем все сообщения админов об этой операции
        messages = await db.get_operation_messages("income", income_id)
        
        # Обновляем сообщения у всех админов
        for msg in messages:
            try:
                await bot.edit_message_text(
                    chat_id=msg["chat_id"],
                    message_id=msg["message_id"],
                    text="✅ Доход подтвержден",
                    reply_markup=None  # Убираем кнопки
                )
            except Exception as e:
                logger.error(f"Error updating message for admin {msg['chat_id']}: {e}")
        
        # Удаляем записи о сообщениях
        await db.clear_operation_messages("income", income_id)
        
        # Получаем данные о доходе для уведомлений
        cursor = db.conn.cursor()
        cursor.execute(
            "SELECT user_id, user_name, amount, comment FROM incomes WHERE id = ?",
            (income_id,)
        )
        income_data = cursor.fetchone()
        
        if income_data:
            user_id, user_name, amount, comment = income_data
            
            # Уведомляем группу
            await bot.send_message(
                chat_id=GROUP_ID,
                text=(
                    f"✅ Подтвержден доход:\n"
                    f"Сумма: {amount} руб.\n"
                    f"От: {user_name}\n"
                    f"Комментарий: {comment}"
                )
            )

@admin_router.callback_query(F.data.startswith("reject_income_"))
@error_handler
async def reject_income(callback: CallbackQuery, **kwargs):
    income_id = int(callback.data.split("_")[2])
    
    # Получаем данные о доходе перед удалением
    cursor = db.conn.cursor()
    cursor.execute(
        "SELECT user_id, user_name, amount, comment FROM incomes WHERE id = ?",
        (income_id,)
    )
    income_data = cursor.fetchone()
    
    if income_data and await db.reject_income(income_id):
        user_id, user_name, amount, comment = income_data
        
        # Получаем все сообщения админов об этой операции
        messages = await db.get_operation_messages("income", income_id)
        
        # Обновляем сообщения у всех админов
        for msg in messages:
            try:
                await bot.edit_message_text(
                    chat_id=msg["chat_id"],
                    message_id=msg["message_id"],
                    text="❌ Доход отклонен"
                )
            except Exception as e:
                logger.error(f"Error updating admin message: {e}")
        
        # Удаляем записи о сообщениях
        await db.clear_operation_messages("income", income_id)
        
        # Логируем действие
        logger.info(f"Income rejected: id={income_id}, user_id={user_id}")
        
        # Уведомляем группу
        await bot.send_message(
            chat_id=GROUP_ID,
            text=(
                f"❌ Доход отклонен администратором:\n"
                f"Сумма: {amount} руб.\n"
                f"От: {user_name}\n"
                f"Комментарий: {comment}\n"
                f"Админ: {callback.from_user.full_name}"
            )
        )
        
        # Уведомляем пользователя
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"Ваш доход {amount} руб. ('{comment}') "
                    f"был отклонен администратором {callback.from_user.full_name}"
                )
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")
        
        await callback.message.edit_text("✅ Доход успешно отклонен")
    else:
        await callback.message.edit_text("❌ Не удалось отклонить доход")
        logger.error(f"Ошибка при отклонении дохода: id={income_id}")
    
    await callback.answer()

@admin_router.callback_query(F.data.startswith("confirm_expense_"))
@error_handler
async def confirm_expense(callback: CallbackQuery, **kwargs):
    expense_id = int(callback.data.split("_")[2])
    
    if await db.confirm_expense(expense_id):
        # Получаем все сообщения админов об этой операции
        messages = await db.get_operation_messages("expense", expense_id)
        
        # Обновляем сообщения у всех админов
        for msg in messages:
            try:
                await bot.edit_message_text(
                    chat_id=msg["chat_id"],
                    message_id=msg["message_id"],
                    text="✅ Расход подтвержден",
                    reply_markup=None  # Убираем кнопки
                )
            except Exception as e:
                logger.error(f"Error updating message for admin {msg['chat_id']}: {e}")
        
        # Удаляем записи о сообщениях
        await db.clear_operation_messages("expense", expense_id)
        
        # Получаем данные о расходе для уведомлений
        cursor = db.conn.cursor()
        cursor.execute(
            "SELECT user_id, user_name, category, amount, comment FROM expenses WHERE id = ?",
            (expense_id,)
        )
        expense_data = cursor.fetchone()
        
        if expense_data:
            user_id, user_name, category, amount, comment = expense_data
            categories = await db.get_categories()
            category_name = categories.get(category, category)
            
            # Уведомляем группу
            await bot.send_message(
                chat_id=GROUP_ID,
                text=(
                    f"✅ Подтвержден расход:\n"
                    f"Категория: {category_name}\n"
                    f"Сумма: {amount} руб.\n"
                    f"Пользователь: {user_name}\n"
                    f"Комментарий: {comment}"
                )
            )

@admin_router.callback_query(F.data.startswith("reject_expense_"))
@error_handler
async def reject_expense(callback: CallbackQuery, **kwargs):
    expense_id = int(callback.data.split("_")[2])
    
    # Получаем данные о расходе перед удалением
    cursor = db.conn.cursor()
    cursor.execute(
        "SELECT user_id, user_name, category, amount, comment FROM expenses WHERE id = ?",
        (expense_id,)
    )
    expense_data = cursor.fetchone()
    
    if expense_data and await db.delete_expense(expense_id):
        user_id, user_name, category, amount, comment = expense_data
        
        # Получаем все сообщения админов об этой операции
        messages = await db.get_operation_messages("expense", expense_id)
        
        # Обновляем сообщения у всех админов
        for msg in messages:
            try:
                await bot.edit_message_text(
                    chat_id=msg["chat_id"],
                    message_id=msg["message_id"],
                    text="❌ Расход отклонен"
                )
            except Exception as e:
                logger.error(f"Error updating admin message: {e}")
        
        # Удаляем записи о сообщениях
        await db.clear_operation_messages("expense", expense_id)
        
        categories = await db.get_categories()
        category_name = categories.get(category, category)
        
        logger.info(f"Expense rejected: id={expense_id}, user_id={user_id}")
        
        # Уведомляем группу
        await bot.send_message(
            chat_id=GROUP_ID,
            text=(
                f"❌ Расход отклонен администратором:\n"
                f"Категория: {category_name}\n"
                f"Сумма: {amount} руб.\n"
                f"Пользователь: {user_name}\n"
                f"Комментарий: {comment}\n"
                f"Админ: {callback.from_user.full_name}"
            )
        )
        
        # Уведомляем пользователя
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"Ваш расход {amount} руб. (категория: {category_name}) "
                    f"был отклонен администратором {callback.from_user.full_name}"
                )
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")
        
        await callback.message.edit_text("✅ Расход успешно отклонен")
    else:
        await callback.message.edit_text("❌ Не удалось отклонить расход")
        logger.error(f"Ошибка при отклонении расхода: id={expense_id}")
    
    await callback.answer()

@admin_router.message(Command("set_report_interval"))
@error_handler
async def cmd_set_report_interval(message: Message, **kwargs):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Эта команда доступна только администраторам")
        return
    
    current_interval = await db.get_report_interval()
    
    keyboard = InlineKeyboardBuilder()
    intervals = [1, 3, 7, 10, 14, 30]
    for days in intervals:
        keyboard.button(text=f"{days} дней", callback_data=f"set_interval_{days}")
    keyboard.adjust(3)
    
    await message.answer(
        f"📅 Текущий интервал отчетов: {current_interval} дней\n"
        "Выберите новый интервал:",
        reply_markup=keyboard.as_markup()
    )

@admin_router.callback_query(F.data.startswith("set_interval_"))
@error_handler
async def process_set_interval(callback: CallbackQuery, **kwargs):
    days = int(callback.data.split("_")[2])
    
    if await db.set_report_interval(days):
        await callback.message.edit_text(
            f"✅ Интервал отчетов изменен на {days} дней",
            reply_markup=None
        )
    else:
        await callback.message.edit_text(
            "❌ Не удалось изменить интервал",
            reply_markup=None
        )
    
    await callback.answer()

async def send_autoreport():
    try:
        end_date = datetime.now().isoformat()
        interval_days = await db.get_report_interval()
        start_date = (datetime.now() - timedelta(days=interval_days)).isoformat()
        
        report_data = await db.get_report_data(start_date, end_date)
        
        start_date_str = datetime.fromisoformat(start_date).strftime("%d.%m.%Y")
        end_date_str = datetime.fromisoformat(end_date).strftime("%d.%m.%Y")
        
        message = (
            f"📊 Финансовый отчёт ({start_date_str} - {end_date_str}):\n\n"
            f"• Общие доходы: {report_data['total_income']:,.2f} руб.\n"
            f"• Общие расходы: {report_data['total_expense']:,.2f} руб.\n"
            f"• Новые сотрудники: {report_data['new_employees']} чел.\n"
            f"• Общее число сотрудников: {report_data['total_employees']} чел."
        )
        
        await bot.send_message(chat_id=GROUP_ID, text=message)
    except Exception as e:
        logger.error(f"Error sending autoreport: {e}")

async def backup_database():
    try:
        await db.backup()
    except Exception as e:
        logger.error(f"Error in backup task: {e}")

async def run_periodic_tasks():
    while True:
        try:
            interval_days = await db.get_report_interval()
            await send_autoreport()
            await asyncio.sleep(interval_days * 24 * 3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in periodic task: {e}")
            await asyncio.sleep(3600)

async def run_backup_task():
    while True:
        try:
            await backup_database()
            await asyncio.sleep(24 * 3600)
        except Exception as e:
            logger.error(f"Error in backup task: {e}")
            await asyncio.sleep(3600)

async def on_startup():
    logger.info("Bot starting up...")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    asyncio.create_task(run_periodic_tasks())
    asyncio.create_task(run_backup_task())

async def on_shutdown():
    logger.info("Bot shutting down...")
    db.close()

# Регистрация роутеров
dp.include_router(user_router)
dp.include_router(admin_router)

# Регистрация обработчиков событий
dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        db.close()
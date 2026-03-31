import os
import json
import sqlite3
import logging
import threading
import asyncio
import secrets
import hashlib
import base64
import requests
import queue
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
import pytz

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
GOOGLE_REDIRECT_URI = os.getenv('GOOGLE_REDIRECT_URI', 'http://localhost:8080')
DATABASE_PATH = 'bot_database.db'
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

# Состояния для ConversationHandler
AUTH_METHOD, WAITING_CREDENTIALS, SELECT_CALENDAR = range(3)

# Глобальные переменные
auth_verifiers = {}  # Для хранения code verifiers
auth_queue = queue.Queue()  # Очередь для кодов авторизации


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Обработчик для локального сервера OAuth"""
    
    def do_GET(self):
        query = urlparse(self.path).query
        params = parse_qs(query)
        user_id = params.get('state', [None])[0]
        
        logger.info(f"OAuth callback received for user {user_id}")
        
        if 'code' in params and user_id:
            code = params['code'][0]
            logger.info(f"Auth code received for user {user_id}")
            
            # Помещаем код в очередь
            auth_queue.put({
                'user_id': int(user_id),
                'code': code
            })
            
            # Отправляем успешный ответ
            html_content = """
            <html>
            <head>
                <meta charset="utf-8">
                <title>Авторизация успешна</title>
                <style>
                    body { font-family: Arial, sans-serif; text-align: center; padding: 50px; }
                    h1 { color: green; }
                    button { 
                        background-color: #4CAF50; 
                        border: none; 
                        color: white; 
                        padding: 15px 32px; 
                        text-align: center; 
                        text-decoration: none; 
                        display: inline-block; 
                        font-size: 16px; 
                        margin: 4px 2px; 
                        cursor: pointer;
                        border-radius: 4px;
                    }
                </style>
            </head>
            <body>
                <h1>✅ Авторизация успешна!</h1>
                <p>Код авторизации получен. Вы можете закрыть это окно и вернуться в Telegram.</p>
                <p>Бот продолжит работу автоматически.</p>
                <button onclick="window.close()">Закрыть окно</button>
            </body>
            </html>
            """.encode('utf-8')
            
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html_content)))
            self.end_headers()
            self.wfile.write(html_content)
        else:
            # Ошибка авторизации
            html_content = """
            <html>
            <head>
                <meta charset="utf-8">
                <title>Ошибка авторизации</title>
                <style>
                    body { font-family: Arial, sans-serif; text-align: center; padding: 50px; }
                    h1 { color: red; }
                    button { 
                        background-color: #f44336; 
                        border: none; 
                        color: white; 
                        padding: 15px 32px; 
                        text-align: center; 
                        text-decoration: none; 
                        display: inline-block; 
                        font-size: 16px; 
                        margin: 4px 2px; 
                        cursor: pointer;
                        border-radius: 4px;
                    }
                </style>
            </head>
            <body>
                <h1>❌ Ошибка авторизации</h1>
                <p>Код авторизации не получен. Попробуйте снова.</p>
                <button onclick="window.close()">Закрыть окно</button>
            </body>
            </html>
            """.encode('utf-8')
            
            self.send_response(400)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html_content)))
            self.end_headers()
            self.wfile.write(html_content)
    
    def log_message(self, format, *args):
        """Отключает логирование HTTP сервера"""
        pass


class OAuthServer:
    """Класс для управления OAuth сервером"""
    
    def __init__(self, host='localhost', port=8080):
        self.host = host
        self.port = port
        self.server = None
        self.thread = None
    
    def start(self):
        """Запуск сервера в отдельном потоке"""
        if not self.server:
            self.server = HTTPServer((self.host, self.port), OAuthCallbackHandler)
            self.thread = threading.Thread(target=self.server.serve_forever)
            self.thread.daemon = True
            self.thread.start()
            logger.info(f"OAuth server started on {self.host}:{self.port}")
    
    def stop(self):
        """Остановка сервера"""
        if self.server:
            self.server.shutdown()
            self.server = None
            self.thread = None
            logger.info("OAuth server stopped")


# Создаем глобальный экземпляр OAuth сервера
oauth_server = OAuthServer()


class DatabaseManager:
    """Класс для работы с базой данных SQLite"""
    
    def __init__(self, db_path):
        self.db_path = db_path
        self.init_db()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def init_db(self):
        """Инициализация таблиц в базе данных"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Таблица пользователей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    telegram_username TEXT,
                    credentials TEXT,
                    reminder_minutes INTEGER DEFAULT 30,
                    selected_calendar TEXT DEFAULT 'primary',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица истории встреч (без calendar_id для совместимости)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS events_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    event_id TEXT,
                    event_name TEXT,
                    event_start TEXT,
                    event_end TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id),
                    UNIQUE(user_id, event_id)
                )
            ''')
            
            # Проверяем, есть ли колонка calendar_id в таблице events_history
            cursor.execute("PRAGMA table_info(events_history)")
            columns = [column[1] for column in cursor.fetchall()]
            
            # Если колонки calendar_id нет, добавляем её
            if 'calendar_id' not in columns:
                try:
                    cursor.execute('ALTER TABLE events_history ADD COLUMN calendar_id TEXT DEFAULT "primary"')
                    logger.info("Added calendar_id column to events_history table")
                except Exception as e:
                    logger.error(f"Error adding calendar_id column: {e}")
            
            # Таблица для отслеживания отправленных уведомлений
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sent_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    event_id TEXT,
                    reminder_time TEXT,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id),
                    UNIQUE(user_id, event_id, reminder_time)
                )
            ''')
            
            # Таблица для списка календарей пользователя
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_calendars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    calendar_id TEXT,
                    calendar_summary TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id),
                    UNIQUE(user_id, calendar_id)
                )
            ''')
            
            conn.commit()
    
    def get_user(self, user_id):
        """Получение данных пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT * FROM users WHERE user_id = ?',
                (user_id,)
            )
            return cursor.fetchone()
    
    def save_user_credentials(self, user_id, username, credentials_json):
        """Сохранение учетных данных пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Убеждаемся, что credentials_json - это строка
            if not isinstance(credentials_json, str):
                credentials_json = json.dumps(credentials_json)
            cursor.execute('''
                INSERT OR REPLACE INTO users (user_id, telegram_username, credentials)
                VALUES (?, ?, ?)
            ''', (user_id, username, credentials_json))
            conn.commit()
    
    def update_reminder_minutes(self, user_id, minutes):
        """Обновление времени напоминания"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE users SET reminder_minutes = ? WHERE user_id = ?',
                (minutes, user_id)
            )
            conn.commit()
    
    def update_selected_calendar(self, user_id, calendar_id):
        """Обновление выбранного календаря"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE users SET selected_calendar = ? WHERE user_id = ?',
                (calendar_id, user_id)
            )
            conn.commit()
    
    def save_user_calendars(self, user_id, calendars):
        """Сохранение списка календарей пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Очищаем старые записи
            cursor.execute('DELETE FROM user_calendars WHERE user_id = ?', (user_id,))
            # Сохраняем новые
            for cal in calendars:
                cursor.execute('''
                    INSERT INTO user_calendars (user_id, calendar_id, calendar_summary)
                    VALUES (?, ?, ?)
                ''', (user_id, cal['id'], cal.get('summary', 'Без названия')))
            conn.commit()
    
    def get_user_calendars(self, user_id):
        """Получение списка календарей пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT calendar_id, calendar_summary FROM user_calendars WHERE user_id = ?',
                (user_id,)
            )
            return cursor.fetchall()
    
    def save_event_to_history(self, user_id, event, calendar_id='primary'):
        """Сохранение события в историю"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO events_history 
                    (user_id, event_id, event_name, event_start, event_end, calendar_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    user_id,
                    event['id'],
                    event.get('summary', 'Без названия'),
                    event['start'].get('dateTime', event['start'].get('date')),
                    event['end'].get('dateTime', event['end'].get('date')),
                    calendar_id
                ))
                conn.commit()
            except Exception as e:
                logger.error(f"Error saving event to history: {e}")
    
    def get_recent_events(self, user_id, limit=10):
        """Получение последних событий пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Проверяем, есть ли колонка calendar_id
            cursor.execute("PRAGMA table_info(events_history)")
            columns = [column[1] for column in cursor.fetchall()]
            
            if 'calendar_id' in columns:
                cursor.execute('''
                    SELECT event_name, event_start, calendar_id 
                    FROM events_history 
                    WHERE user_id = ? 
                    ORDER BY event_start DESC 
                    LIMIT ?
                ''', (user_id, limit))
                return cursor.fetchall()
            else:
                cursor.execute('''
                    SELECT event_name, event_start, "primary" as calendar_id 
                    FROM events_history 
                    WHERE user_id = ? 
                    ORDER BY event_start DESC 
                    LIMIT ?
                ''', (user_id, limit))
                return cursor.fetchall()
    
    def was_notification_sent(self, user_id, event_id, reminder_time):
        """Проверка, было ли уже отправлено уведомление"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 1 FROM sent_notifications 
                WHERE user_id = ? AND event_id = ? AND reminder_time = ?
            ''', (user_id, event_id, reminder_time))
            return cursor.fetchone() is not None
    
    def mark_notification_sent(self, user_id, event_id, reminder_time):
        """Отметить уведомление как отправленное"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO sent_notifications 
                (user_id, event_id, reminder_time)
                VALUES (?, ?, ?)
            ''', (user_id, event_id, reminder_time))
            conn.commit()

db_manager = DatabaseManager(DATABASE_PATH)


class GoogleCalendarService:
    """Сервис для работы с Google Calendar API"""
    
    @staticmethod
    def _generate_code_verifier():
        """Генерация code verifier для PKCE"""
        token = secrets.token_urlsafe(64)
        return token[:128]
    
    @staticmethod
    def _generate_code_challenge(code_verifier):
        """Генерация code challenge из code verifier"""
        code_challenge = hashlib.sha256(code_verifier.encode('utf-8')).digest()
        code_challenge = base64.urlsafe_b64encode(code_challenge).decode('utf-8')
        return code_challenge.replace('=', '')
    
    @staticmethod
    def get_authorization_url(user_id):
        """Получение URL для OAuth авторизации с локальным сервером"""
        # Генерируем code verifier для PKCE
        code_verifier = GoogleCalendarService._generate_code_verifier()
        code_challenge = GoogleCalendarService._generate_code_challenge(code_verifier)
        
        # Сохраняем code_verifier для этого пользователя
        auth_verifiers[user_id] = code_verifier
        
        # Создаем URL
        params = {
            'response_type': 'code',
            'client_id': GOOGLE_CLIENT_ID,
            'redirect_uri': GOOGLE_REDIRECT_URI,
            'scope': ' '.join(SCOPES),
            'state': str(user_id),
            'access_type': 'offline',
            'prompt': 'consent',
            'code_challenge': code_challenge,
            'code_challenge_method': 'S256'
        }
        
        auth_url = f"https://accounts.google.com/o/oauth2/auth?{urlencode(params)}"
        return auth_url
    
    @staticmethod
    def get_credentials_from_code(auth_code, user_id):
        """Получение credentials из authorization code с code verifier"""
        # Получаем сохраненный code_verifier
        code_verifier = auth_verifiers.get(user_id)
        if not code_verifier:
            raise Exception("No code verifier found for user")
        
        try:
            # Обмениваем код на токены
            token_url = "https://oauth2.googleapis.com/token"
            data = {
                'code': auth_code,
                'client_id': GOOGLE_CLIENT_ID,
                'client_secret': GOOGLE_CLIENT_SECRET,
                'redirect_uri': GOOGLE_REDIRECT_URI,
                'grant_type': 'authorization_code',
                'code_verifier': code_verifier
            }
            
            response = requests.post(token_url, data=data)
            response.raise_for_status()
            token_data = response.json()
            
            # Создаем credentials
            credentials = Credentials(
                token=token_data.get('access_token'),
                refresh_token=token_data.get('refresh_token'),
                token_uri=token_url,
                client_id=GOOGLE_CLIENT_ID,
                client_secret=GOOGLE_CLIENT_SECRET,
                scopes=SCOPES
            )
            
            return credentials
            
        except Exception as e:
            logger.error(f"Error exchanging code for tokens: {e}")
            raise
        finally:
            # Очищаем verifier после использования
            if user_id in auth_verifiers:
                del auth_verifiers[user_id]
    
    @staticmethod
    def get_calendar_service(credentials_json):
        """Получение сервиса календаря из сохраненных credentials"""
        try:
            # Если credentials_json - строка, парсим её
            if isinstance(credentials_json, str):
                creds_data = json.loads(credentials_json)
            else:
                creds_data = credentials_json
                
            creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
            
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                return build('calendar', 'v3', credentials=creds), creds
            elif creds and creds.valid:
                return build('calendar', 'v3', credentials=creds), None
            else:
                return None, None
        except Exception as e:
            logger.error(f"Error getting calendar service: {e}")
            return None, None
    
    @staticmethod
    def get_user_calendars(service):
        """Получение списка календарей пользователя"""
        try:
            calendar_list = service.calendarList().list().execute()
            return calendar_list.get('items', [])
        except Exception as e:
            logger.error(f"Error getting calendars: {e}")
            return []
    
    @staticmethod
    def get_upcoming_events(service, calendar_id='primary', minutes_ahead=60):
        """Получение предстоящих событий из указанного календаря"""
        now = datetime.utcnow().isoformat() + 'Z'
        time_max = (datetime.utcnow() + timedelta(minutes=minutes_ahead)).isoformat() + 'Z'
        
        try:
            events_result = service.events().list(
                calendarId=calendar_id,
                timeMin=now,
                timeMax=time_max,
                maxResults=10,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            return events_result.get('items', [])
        except Exception as e:
            logger.error(f"Error getting upcoming events: {e}")
            return []


async def check_auth_queue(app: Application):
    """Проверка очереди авторизации"""
    try:
        while not auth_queue.empty():
            auth_data = auth_queue.get_nowait()
            user_id = auth_data['user_id']
            code = auth_data['code']
            
            logger.info(f"Processing auth code from queue for user {user_id}")
            
            user_data = app.user_data.get(user_id, {})
            if 'auth_event' in user_data:
                user_data['auth_code'] = code
                user_data['auth_event'].set()
                logger.info(f"Auth code set for user {user_id}")
            else:
                logger.error(f"No auth_event found for user {user_id}")
                await app.bot.send_message(
                    chat_id=user_id,
                    text="❌ **Ошибка авторизации**\n\n"
                         "Сессия авторизации истекла. Пожалуйста, отправьте /start для новой попытки."
                )
    except Exception as e:
        logger.error(f"Error checking auth queue: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    db_user = db_manager.get_user(user.id)
    
    if db_user and db_user[3]:  # Проверяем наличие credentials
        # Получаем список календарей для отображения
        calendars = db_manager.get_user_calendars(user.id)
        calendar_info = f"Выбранный календарь: {db_user[4]}\n" if calendars else ""
        
        await update.message.reply_text(
            f"👋 С возвращением, {user.first_name}!\n\n"
            f"Вы уже авторизованы в Google Calendar.\n"
            f"Текущее время напоминания: {db_user[3]} минут(ы)\n"
            f"{calendar_info}\n"
            "Доступные команды:\n"
            "/set_reminder - установить время напоминания\n"
            "/list_calendars - показать список календарей\n"
            "/select_calendar - выбрать календарь\n"
            "/history - показать историю встреч\n"
            "/help - помощь"
        )
        return ConversationHandler.END
    
    keyboard = [
        [InlineKeyboardButton("🔑 Авторизоваться через Google", callback_data='oauth')],
        [InlineKeyboardButton("📝 Ввести credentials вручную", callback_data='manual')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Я бот для уведомлений о встречах из Google Calendar.\n"
        "Для начала работы необходимо авторизоваться.",
        reply_markup=reply_markup
    )
    
    return AUTH_METHOD


async def auth_method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора способа авторизации"""
    query = update.callback_query
    
    try:
        await query.answer()
        
        if query.data == 'oauth':
            user_id = update.effective_user.id
            auth_url = GoogleCalendarService.get_authorization_url(user_id)
            
            auth_event = asyncio.Event()
            context.user_data['auth_event'] = auth_event
            context.user_data['auth_code'] = None
            
            await query.edit_message_text(
                "🔐 **Авторизация через Google**\n\n"
                "1. Перейдите по ссылке ниже\n"
                "2. Войдите в свой Google аккаунт\n"
                "3. Разрешите доступ к календарю\n"
                "4. Дождитесь автоматического перенаправления\n\n"
                f"**Ссылка для авторизации:**\n{auth_url}\n\n"
                "⏳ Ожидаю код авторизации..."
            )
            
            # Запускаем ожидание кода
            asyncio.create_task(wait_for_auth_code(update, context))
            
        elif query.data == 'manual':
            await query.edit_message_text(
                "📝 **Ручной ввод credentials**\n\n"
                "Отправьте содержимое файла credentials.json\n"
                "(весь текст JSON) одним сообщением.\n\n"
                "Как получить файл:\n"
                "1. В Google Cloud Console скачайте JSON с учетными данными\n"
                "2. Откройте файл в текстовом редакторе\n"
                "3. Скопируйте всё содержимое\n"
                "4. Отправьте сюда"
            )
            return WAITING_CREDENTIALS
            
    except Exception as e:
        logger.error(f"Error in auth_method_callback: {e}")
        await query.edit_message_text(
            f"❌ Произошла ошибка: {str(e)}\n\nПопробуйте снова через /start"
        )
        return ConversationHandler.END


async def wait_for_auth_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ожидание получения кода авторизации"""
    user_id = update.effective_user.id
    logger.info(f"Waiting for auth code from user {user_id}")
    
    auth_event = context.user_data.get('auth_event')
    if not auth_event:
        logger.error(f"No auth_event for user {user_id}")
        return
    
    try:
        # Ждем код с таймаутом 5 минут
        await asyncio.wait_for(auth_event.wait(), timeout=300)
        
        auth_code = context.user_data.get('auth_code')
        if auth_code:
            logger.info(f"Auth code received for user {user_id}")
            
            # Отправляем сообщение о получении кода
            await context.bot.send_message(
                chat_id=user_id,
                text="✅ Код получен! Завершаю авторизацию..."
            )
            
            # Обрабатываем код
            await handle_oauth_code(update, context, auth_code)
        else:
            raise Exception("No auth code in user_data")
            
    except asyncio.TimeoutError:
        logger.warning(f"Auth timeout for user {user_id}")
        await context.bot.send_message(
            chat_id=user_id,
            text="⏰ **Время ожидания истекло**\n\n"
                 "Вы не завершили авторизацию в течение 5 минут.\n"
                 "Запустите /start для новой попытки."
        )
    except Exception as e:
        logger.error(f"Error waiting for auth code: {e}")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ Ошибка: {str(e)}\n\nЗапустите /start для новой попытки."
        )
    finally:
        # Очищаем данные
        context.user_data.pop('auth_event', None)
        context.user_data.pop('auth_code', None)


async def handle_oauth_code(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    """Обработка полученного OAuth кода"""
    user = update.effective_user
    
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text="🔄 Обмениваю код на токены доступа..."
        )
        
        credentials = GoogleCalendarService.get_credentials_from_code(code, user.id)
        credentials_json = credentials.to_json()
        
        db_manager.save_user_credentials(user.id, user.username, credentials_json)
        db_manager.update_reminder_minutes(user.id, 30)
        
        # Получаем список календарей пользователя
        service, _ = GoogleCalendarService.get_calendar_service(credentials_json)
        if service:
            calendars = GoogleCalendarService.get_user_calendars(service)
            db_manager.save_user_calendars(user.id, calendars)
        
        await context.bot.send_message(
            chat_id=user.id,
            text="✅ **Авторизация успешна!**\n\n"
                 "Теперь вы будете получать уведомления о встречах.\n"
                 "По умолчанию уведомления приходят за 30 минут до начала.\n\n"
                 "**Доступные команды:**\n"
                 "/set_reminder - изменить время напоминания\n"
                 "/list_calendars - показать список календарей\n"
                 "/select_calendar - выбрать календарь\n"
                 "/history - показать историю встреч\n"
                 "/help - справка"
        )
        
        await check_user_events(context.application, user.id)
        
    except Exception as e:
        logger.error(f"Auth error for user {user.id}: {e}")
        await context.bot.send_message(
            chat_id=user.id,
            text=f"❌ **Ошибка авторизации**\n\n"
                 f"{str(e)}\n\n"
                 "Попробуйте снова через /start"
        )


async def handle_manual_credentials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ручного ввода credentials"""
    user = update.effective_user
    text = update.message.text.strip()
    
    try:
        if len(text) > 50 and not text.startswith('{'):
            await handle_oauth_code(update, context, text)
            return ConversationHandler.END
        
        credentials_data = json.loads(text)
        required_fields = ['token', 'refresh_token', 'token_uri', 'client_id', 'client_secret']
        missing_fields = [field for field in required_fields if field not in credentials_data]
        
        if missing_fields:
            await update.message.reply_text(
                f"❌ **Ошибка: отсутствуют поля**\n\n"
                f"Не найдены: {', '.join(missing_fields)}\n\n"
                "Убедитесь, что вы отправляете полные credentials JSON."
            )
            return WAITING_CREDENTIALS
        
        # Сохраняем credentials как строку JSON
        db_manager.save_user_credentials(user.id, user.username, text)
        db_manager.update_reminder_minutes(user.id, 30)
        
        # Получаем список календарей
        service, _ = GoogleCalendarService.get_calendar_service(text)
        if service:
            calendars = GoogleCalendarService.get_user_calendars(service)
            db_manager.save_user_calendars(user.id, calendars)
        
        await update.message.reply_text(
            "✅ **Авторизация успешна!**\n\n"
            "Теперь вы будете получать уведомления о встречах.\n"
            "По умолчанию уведомления приходят за 30 минут до начала.\n\n"
            "**Доступные команды:**\n"
            "/set_reminder - изменить время напоминания\n"
            "/list_calendars - показать список календарей\n"
            "/select_calendar - выбрать календарь\n"
            "/history - показать историю встреч\n"
            "/help - справка"
        )
        
        await check_user_events(context.application, user.id)
        
    except json.JSONDecodeError:
        await update.message.reply_text(
            "❌ **Ошибка: неверный формат JSON**\n\n"
            "Пожалуйста, отправьте корректный JSON."
        )
        return WAITING_CREDENTIALS
    except Exception as e:
        logger.error(f"Manual auth error for user {user.id}: {e}")
        await update.message.reply_text(
            f"❌ **Ошибка авторизации**\n\n{str(e)}\n\nПопробуйте снова через /start"
        )
        return WAITING_CREDENTIALS
    
    return ConversationHandler.END


async def list_calendars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список доступных календарей"""
    user = update.effective_user
    
    db_user = db_manager.get_user(user.id)
    if not db_user or not db_user[3]:
        await update.message.reply_text(
            "❌ **Необходима авторизация**\n\nСначала выполните /start"
        )
        return
    
    calendars = db_manager.get_user_calendars(user.id)
    if not calendars:
        await update.message.reply_text(
            "📭 **Список календарей пуст**\n\n"
            "Попробуйте обновить список позже."
        )
        return
    
    message = "📅 **Ваши календари:**\n\n"
    for cal_id, cal_name in calendars:
        selected = "✅ " if cal_id == db_user[4] else "• "
        message += f"{selected} **{cal_name}**\n`{cal_id}`\n\n"
    
    message += "Используйте /select_calendar для выбора календаря"
    await update.message.reply_text(message)


async def select_calendar_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало выбора календаря"""
    user = update.effective_user
    
    db_user = db_manager.get_user(user.id)
    if not db_user or not db_user[3]:
        await update.message.reply_text(
            "❌ **Необходима авторизация**\n\nСначала выполните /start"
        )
        return ConversationHandler.END
    
    calendars = db_manager.get_user_calendars(user.id)
    if not calendars:
        await update.message.reply_text(
            "📭 **Список календарей пуст**"
        )
        return ConversationHandler.END
    
    keyboard = []
    for cal_id, cal_name in calendars:
        keyboard.append([InlineKeyboardButton(
            f"{cal_name}",
            callback_data=f"cal_{cal_id}"
        )])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📅 **Выберите календарь:**",
        reply_markup=reply_markup
    )
    return SELECT_CALENDAR


async def select_calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора календаря"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    cal_id = query.data.replace('cal_', '')
    
    db_manager.update_selected_calendar(user.id, cal_id)
    
    # Получаем название календаря
    calendars = db_manager.get_user_calendars(user.id)
    cal_name = next((name for cid, name in calendars if cid == cal_id), cal_id)
    
    await query.edit_message_text(
        f"✅ **Календарь выбран**\n\n"
        f"Теперь уведомления будут приходить из календаря:\n"
        f"**{cal_name}**"
    )
    return ConversationHandler.END


async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка времени напоминания"""
    user = update.effective_user
    
    db_user = db_manager.get_user(user.id)
    if not db_user or not db_user[3]:
        await update.message.reply_text(
            "❌ **Необходима авторизация**\n\nСначала выполните /start"
        )
        return
    
    if context.args and context.args[0].isdigit():
        minutes = int(context.args[0])
        if minutes < 1 or minutes > 1440:
            await update.message.reply_text(
                "❌ **Ошибка**\n\n"
                "Время должно быть от 1 до 1440 минут (24 часа)"
            )
            return
        
        db_manager.update_reminder_minutes(user.id, minutes)
        await update.message.reply_text(
            f"✅ **Время напоминания установлено**\n\n"
            f"Теперь уведомления будут приходить за **{minutes}** минут(ы) до начала встречи"
        )
    else:
        await update.message.reply_text(
            f"📌 **Текущие настройки**\n\n"
            f"Время напоминания: **{db_user[3]}** минут(ы)\n\n"
            "Для изменения используйте:\n"
            "`/set_reminder <минуты>`\n"
            "Пример: `/set_reminder 15`"
        )


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать историю встреч"""
    user = update.effective_user
    
    db_user = db_manager.get_user(user.id)
    if not db_user or not db_user[3]:
        await update.message.reply_text(
            "❌ **Необходима авторизация**\n\nСначала выполните /start"
        )
        return
    
    events = db_manager.get_recent_events(user.id)
    
    if not events:
        await update.message.reply_text("📭 **История пуста**\n\nУ вас пока нет завершенных встреч.")
        return
    
    message = "📅 **Последние 10 встреч:**\n\n"
    for i, event_data in enumerate(events, 1):
        if len(event_data) == 3:
            name, start_time, calendar_id = event_data
        else:
            name, start_time = event_data
            calendar_id = "primary"
            
        try:
            dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            local_tz = datetime.now().astimezone().tzinfo
            dt_local = dt.astimezone(local_tz)
            formatted_time = dt_local.strftime("%d.%m.%Y %H:%M")
            message += f"{i}. **[{formatted_time}]** {name}\n"
        except:
            message += f"{i}. {start_time} - {name}\n"
    
    await update.message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать справку"""
    help_text = (
        "🤖 **Calendar Bot - Справка**\n\n"
        "**Команды:**\n"
        "/start - начать работу и авторизация\n"
        "/set_reminder <минуты> - установить время напоминания\n"
        "/list_calendars - показать список календарей\n"
        "/select_calendar - выбрать календарь для уведомлений\n"
        "/history - показать историю последних 10 встреч\n"
        "/help - показать эту справку\n\n"
        "**Как это работает:**\n"
        "• Бот проверяет ваш календарь каждую минуту\n"
        "• При обнаружении встречи придет уведомление\n"
        "• История сохраняется автоматически\n"
        "• Можно выбрать конкретный календарь\n"
        "• Можно выбрать время напоминания от 1 до 1440 минут\n\n"
        "**Примеры:**\n"
        "`/set_reminder 15` - напоминание за 15 минут\n"
        "`/set_reminder 60` - напоминание за 1 час"
    )
    await update.message.reply_text(help_text)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего действия"""
    await update.message.reply_text("❌ Действие отменено")
    return ConversationHandler.END


async def check_user_events(app: Application, user_id: int):
    """Проверка событий для конкретного пользователя"""
    try:
        db_user = db_manager.get_user(user_id)
        if not db_user or not db_user[3]:
            return
        
        # Проверяем длину кортежа (разные версии БД)
        if len(db_user) >= 6:
            credentials_json = db_user[3]
            reminder_minutes = db_user[4]
            selected_calendar = db_user[5] if db_user[5] else 'primary'
        else:
            credentials_json = db_user[3]
            reminder_minutes = db_user[4]
            selected_calendar = 'primary'
        
        service, updated_creds = GoogleCalendarService.get_calendar_service(credentials_json)
        if not service:
            return
        
        if updated_creds:
            db_manager.save_user_credentials(user_id, db_user[2], updated_creds.to_json())
        
        events = GoogleCalendarService.get_upcoming_events(service, selected_calendar, reminder_minutes + 5)
        
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            
            if 'dateTime' in event['start']:
                event_time = datetime.fromisoformat(start.replace('Z', '+00:00'))
                now = datetime.now(pytz.UTC)
                time_until = (event_time - now).total_seconds() / 60
                
                if 0 <= time_until <= reminder_minutes:
                    reminder_key = f"{reminder_minutes}_minutes"
                    if not db_manager.was_notification_sent(user_id, event['id'], reminder_key):
                        await send_notification(app, user_id, event, time_until)
                        db_manager.mark_notification_sent(user_id, event['id'], reminder_key)
            
            end = event['end'].get('dateTime', event['end'].get('date'))
            if 'dateTime' in event['end']:
                end_time = datetime.fromisoformat(end.replace('Z', '+00:00'))
                if end_time < datetime.now(pytz.UTC):
                    db_manager.save_event_to_history(user_id, event, selected_calendar)
            else:
                end_date = datetime.fromisoformat(end)
                if end_date.date() < datetime.now().date():
                    db_manager.save_event_to_history(user_id, event, selected_calendar)
    
    except Exception as e:
        logger.error(f"Error checking events for user {user_id}: {e}")


async def send_notification(app: Application, user_id: int, event: dict, minutes_until: float):
    """Отправка уведомления о событии"""
    try:
        summary = event.get('summary', 'Без названия')
        start = event['start'].get('dateTime', event['start'].get('date'))
        
        if 'dateTime' in event['start']:
            dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
            msk_tz = ZoneInfo("Europe/Moscow")
            dt_msk = dt.astimezone(msk_tz)
            time_str = dt_msk.strftime("%d %B %Y, %H:%M (МСК)")
        else:
            time_str = f"{start} (весь день)"
        
        hangout_link = event.get('hangoutLink', '')
        html_link = event.get('htmlLink', '')
        
        message = (
            f"⏰ **Напоминание!**\n\n"
            f"**Встреча:** \"{summary}\"\n"
            f"**Время:** {time_str}\n"
        )
        
        if hangout_link:
            message += f"**Ссылка на видеовстречу:** {hangout_link}\n"
        elif html_link:
            message += f"**Ссылка на событие:** {html_link}\n"
        
        if minutes_until < 1:
            message += "\n⚠️ **Встреча начинается менее чем через минуту!**"
        else:
            message += f"\n⏱ **До начала:** {int(minutes_until)} мин."
        
        await app.bot.send_message(chat_id=user_id, text=message)
        logger.info(f"Notification sent to user {user_id} for event {event['id']}")
        
    except Exception as e:
        logger.error(f"Error sending notification to user {user_id}: {e}")


async def scheduled_check(app: Application):
    """Периодическая проверка событий для всех пользователей"""
    logger.info("Running scheduled event check")
    
    try:
        # Проверяем очередь авторизации
        await check_auth_queue(app)
        
        # Проверяем события пользователей
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id FROM users WHERE credentials IS NOT NULL')
            users = cursor.fetchall()
        
        for user in users:
            await check_user_events(app, user[0])
    except Exception as e:
        logger.error(f"Error in scheduled check: {e}")


async def job_wrapper(context: ContextTypes.DEFAULT_TYPE):
    """Обертка для задачи в JobQueue"""
    await scheduled_check(context.application)

def setup_jobs(app: Application):
    """Настройка периодических задач через JobQueue"""
    app.job_queue.run_repeating(
        job_wrapper,
        interval=60,  # каждую минуту
        first=10,      # первый запуск через 10 секунд
        name="check_events"
    )
    logger.info("Job queue started - checking events every minute")


async def post_init(app: Application):
    """Действия после инициализации бота"""
    # Запускаем OAuth сервер
    oauth_server.start()
    
    # Устанавливаем команды бота
    commands = [
        BotCommand("start", "Начать работу"),
        BotCommand("set_reminder", "Установить время напоминания"),
        BotCommand("list_calendars", "Показать список календарей"),
        BotCommand("select_calendar", "Выбрать календарь"),
        BotCommand("history", "Показать историю встреч"),
        BotCommand("help", "Показать справку")
    ]
    await app.bot.set_my_commands(commands)
    
    # Запускаем задачи через JobQueue
    setup_jobs(app)
    logger.info("Bot started successfully")


def main():
    """Основная функция запуска бота"""
    # Проверка наличия необходимых переменных окружения
    required_vars = ['TELEGRAM_TOKEN', 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET']
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"Missing environment variables: {', '.join(missing_vars)}")
        logger.error("Please check your .env file")
        return
    
    # Создание приложения с JobQueue
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .connect_timeout(30.0)
           .read_timeout(30.0)
           .write_timeout(30.0)
           .pool_timeout(30.0)
           .post_init(post_init)
           .build())
    
    # Настройка ConversationHandler для авторизации
    auth_conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            AUTH_METHOD: [CallbackQueryHandler(auth_method_callback)],
            WAITING_CREDENTIALS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_credentials)],
            SELECT_CALENDAR: [CallbackQueryHandler(select_calendar_callback, pattern='^cal_')],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_message=False,
        per_chat=True,
        per_user=True,
    )
    
    # Добавление обработчиков
    app.add_handler(auth_conv)
    app.add_handler(CommandHandler('set_reminder', set_reminder))
    app.add_handler(CommandHandler('list_calendars', list_calendars))
    app.add_handler(CommandHandler('select_calendar', select_calendar_start))
    app.add_handler(CommandHandler('history', show_history))
    app.add_handler(CommandHandler('help', help_command))
    
    logger.info("Starting bot...")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        # Останавливаем OAuth сервер при завершении
        oauth_server.stop()


if __name__ == '__main__':
    main()
# 📅 Google Calendar Telegram Bot

Telegram-бот для уведомлений о встречах из Google Calendar. Бот отслеживает события в вашем календаре и присылает напоминания за заданное время до начала встречи.

## ✨ Возможности

- 🔐 **OAuth 2.0 авторизация** через Google (с поддержкой PKCE)
- ⏰ **Настраиваемые напоминания** — установите время оповещения от 1 до 1440 минут
- 📅 **Выбор календаря** — если у вас несколько календарей, выберите нужный
- 📋 **История встреч** — просмотр последних 10 завершённых событий
- 👥 **Многопользовательская поддержка** — бот работает с несколькими пользователями одновременно
- 🔄 **Автоматическое обновление токенов** — токены обновляются автоматически
- 🪵 **Логирование** — подробные логи для отладки

## 🚀 Быстрый старт

### 1. Создание Telegram-бота

1. Откройте Telegram и найдите [@BotFather](https://t.me/botfather)
2. Отправьте команду `/newbot`
3. Придумайте имя бота (например, "Calendar Reminder Bot")
4. Придумайте username бота (должен заканчиваться на `bot`)
5. Сохраните полученный **токен** — он понадобится позже

### 2. Настройка Google Cloud Project

1. Перейдите в [Google Cloud Console](https://console.cloud.google.com/)
2. Создайте новый проект или выберите существующий
3. Включите **Google Calendar API**:
   - Перейдите в "APIs & Services" → "Library"
   - Найдите "Google Calendar API" и нажмите "ENABLE"

4. Настройте OAuth 2.0:
   - Перейдите в "APIs & Services" → "Credentials"
   - Нажмите "+ CREATE CREDENTIALS" → "OAuth client ID"
   - Выберите тип приложения: **Web application**
   - В поле "Authorized redirect URIs" добавьте:
   - http://localhost:8080
   - Нажмите "CREATE"
- Скопируйте **Client ID** и **Client Secret**

### 3. Установка и запуск

#### Клонирование репозитория

``bash
git clone https://github.com/Vetev701/telegram-calendar-bot.git
cd calendar-telegram-bot

## Установка зависимостей
pip install -r requirements.txt

Настройка окружения
Создайте файл .env в корне проекта:

Telegram Bot Token (полученный от @BotFather)
TELEGRAM_TOKEN=8207288036:AAGCcxA2g34Nm_38f6NbzV7_4rMMt62Etwo

Google OAuth 2.0 credentials
GOOGLE_CLIENT_ID=1085665057695-xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxx

Redirect URI (не меняйте, если не меняли в консоли)
GOOGLE_REDIRECT_URI=http://localhost:8080

### Запуск бота
python main.py

### Процесс авторизации
1)Отправьте боту команду /start
2)Выберите "🔑 Авторизоваться через Google"
3)Перейдите по полученной ссылке
4)Войдите в свой Google аккаунт
5)Разрешите доступ к календарю
6)Дождитесь автоматического перенаправления

### После успешной авторизации бот покажет:

✅ Авторизация успешна!

Теперь вы будете получать уведомления о встречах.
По умолчанию уведомления приходят за 30 минут до начала.

### Пример уведомления

⏰ Напоминание!

Встреча: "Еженедельный стендап"
Время: 15 марта 2026, 14:35 (МСК)
Ссылка: https://meet.google.com/abc-defg-hij

### Пример истории встреч

Последние 10 встреч:

1. [15.03.2026 14:35] Еженедельный стендап
2. [14.03.2026 11:00] Обсуждение ТЗ
3. [13.03.2026 16:30] Code Review

## Используемые технологии

Компонент	Технология
Язык программирования	Python 3.10+
Telegram API	python-telegram-bot 20.7
Google API	google-api-python-client
OAuth 2.0	google-auth, google-auth-oauthlib
Хранение данных	SQLite
Планировщик	JobQueue (встроенный)
Асинхронность	asyncio
Переменные окружения	python-dotenv

### Зависимости

python-telegram-bot==20.7
google-api-python-client==2.108.0
google-auth==2.25.2
google-auth-oauthlib==1.1.0
google-auth-httplib2==0.1.1
requests==2.31.0
python-dotenv==1.0.0
pytz==2023.3

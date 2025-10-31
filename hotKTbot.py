import os
import json
import time
import random
import logging
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import schedule
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Загружаем .env
load_dotenv()

# === НАСТРОЙКИ ИЗ .env ===
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден! Установите его в .env")
if not CHAT_ID:
    raise ValueError("CHAT_ID не найден! Установите его в .env")

try:
    CHAT_ID = int(CHAT_ID)
except ValueError:
    raise ValueError("CHAT_ID должен быть числом (например, -1001234567890)")

URL = 'https://www.kino-teatr.ru/mourn/y2025/m12/'
STATE_FILE = 'last_obits.json'

# === ЛОГИРОВАНИЕ + СКРЫТИЕ ТОКЕНА ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Фильтр: скрываем токен в логах
class NoTokenFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if "bot" in msg and ":" in msg:
            parts = msg.split(":", 1)
            if len(parts) > 1 and len(parts[1].strip()) > 10:
                record.msg = msg.replace(parts[1].strip(), "HIDDEN_TOKEN")
        return True

# Применяем фильтр ко всем логам
logging.getLogger().addFilter(NoTokenFilter())

# Убираем спам от HTTP-запросов (getUpdates)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Глобальное состояние
last_obits = []

# === ФУНКЦИИ ПАРСИНГА ===
def load_state():
    global last_obits
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            last_obits = data
            return data
    except FileNotFoundError:
        logger.info("Файл состояния не найден — начинаем с нуля.")
        return []
    except Exception as e:
        logger.error(f"Ошибка чтения состояния: {e}")
        return last_obits

def save_state(obits):
    global last_obits
    last_obits = obits
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(obits, f, ensure_ascii=False, indent=2)
        logger.info("Состояние сохранено.")
    except Exception as e:
        logger.warning(f"Не удалось сохранить: {e}")

def is_recent(death_date_str):
    try:
        months_ru = {
            'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6,
            'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12
        }
        if ' - ' in death_date_str:
            death_date_str = death_date_str.split(' - ')[-1].strip()
        parts = death_date_str.split()
        if len(parts) >= 3:
            day = int(parts[0])
            month_name = parts[1].lower()
            year = int(parts[2])
            month = months_ru.get(month_name, 10)
            death_date = datetime(year, month, day)
            return death_date >= datetime.now() - timedelta(hours=24)
    except:
        return False
    return False

def parse_obits():
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(URL, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        obits = []
        for block in soup.find_all(['h3', 'div', 'p'], string=lambda t: t and ' - ' in t):
            text = block.get_text(strip=True)
            if len(text) < 15 or ' - ' not in text:
                continue
            parts = text.split(' - ', 1)
            name = parts[0].strip()
            dates = parts[1].strip()
            if any(kw in text.lower() for kw in ['актер', 'артист', 'режиссёр', 'театр', 'гимнаст']):
                obits.append({'name': name, 'date': dates})

        seen = set()
        unique = []
        for obit in obits:
            key = f"{obit['name']} {obit['date']}"
            if key not in seen and is_recent(obit['date']):
                seen.add(key)
                unique.append(obit)

        logger.info(f"Найдено {len(unique)} свежих анкет.")
        return unique
    except Exception as e:
        logger.error(f"Ошибка парсинга: {e}")
        return []

# === КОМАНДА /ping ===
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ответ на /ping"""
    now = datetime.now().strftime("%H:%M:%S")
    await update.message.reply_text(f"Pong! Бот работает.\nВремя: {now}")

# === ПРОВЕРКА ОБНОВЛЕНИЙ ===
async def check_updates():
    current_obits = parse_obits()
    if not current_obits:
        return

    last_keys = {f"{o['name']} {o['date']}" for o in last_obits}
    new_obits = [o for o in current_obits if f"{o['name']} {o['date']}" not in last_keys]

    if new_obits:
        message = "Новые анкеты на Кино-Театр.Ру:\n\n"
        for obit in new_obits:
            message += f"• <b>{obit['name']}</b>\n  {obit['date']}\n\n"
        message += f"<a href='{URL}'>Перейти</a>"

        try:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=message,
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            logger.info(f"Отправлено: {len(new_obits)} анкет.")
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")

        save_state(last_obits + new_obits)
    else:
        logger.info("Новых анкет нет.")

# === ОСНОВНОЙ ЦИКЛ ===
def main():
    logger.info("Запуск бота...")
    load_state()

    # Создаём приложение
    application = Application.builder().token(BOT_TOKEN).build()

    # Добавляем команду /ping
    application.add_handler(CommandHandler("ping", ping_command))

    # Запускаем polling (для получения команд)
    application.run_polling(drop_pending_updates=True)

    # === ФОНОВАЯ ПРОВЕРКА КАЖДУЮ МИНУТУ ===
    # Отдельный поток для schedule
    def schedule_loop():
        check_updates_sync = lambda: application.job_queue.run_once(
            lambda ctx: check_updates(), 0
        )
        schedule.every(random.randint(55, 65)).seconds.do(check_updates_sync)
        while True:
            schedule.run_pending()
            time.sleep(1)

    import threading
    threading.Thread(target=schedule_loop, daemon=True).start()

    # === ФЕЙКОВЫЙ СЕРВЕР ДЛЯ RENDER (открывает порт) ===
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is alive! /ping works.')

def run_server():
    port = int(os.getenv('PORT', 10000))  # Render использует $PORT
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    logger.info(f"Фейковый сервер запущен на порту {port}")
    server.serve_forever()

# Запускаем сервер в отдельном потоке
threading.Thread(target=run_server, daemon=True).start()

if __name__ == '__main__':

    main()

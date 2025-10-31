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
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# === ЗАГРУЗКА .env ===
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден!")
if not CHAT_ID:
    raise ValueError("CHAT_ID не найден!")

try:
    CHAT_ID = int(CHAT_ID)
except ValueError:
    raise ValueError("CHAT_ID должен быть числом")

# URL: страница 12 пагинации (m12) за 2025 год
URL = 'https://www.kino-teatr.ru/mourn/y2025/m12/'
STATE_FILE = 'last_obits.json'

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class NoTokenFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if "bot" in msg and ":" in msg:
            parts = msg.split(":", 1)
            if len(parts) > 1 and len(parts[1].strip()) > 10:
                record.msg = msg.replace(parts[1].strip(), "HIDDEN_TOKEN")
        return True

logging.getLogger().addFilter(NoTokenFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# === СТАТИСТИКА ===
stats = {
    "checks_last_hour": 0,
    "last_check": None,
    "start_time": datetime.now()
}

# Сброс счётчика каждый час
def reset_hourly():
    stats["checks_last_hour"] = 0
    logger.info("Сброс счётчика проверок за час.")

schedule.every().hour.do(reset_hourly)

# === СОСТОЯНИЕ ===
last_obits = []

def load_state():
    global last_obits
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            last_obits = data
            return data
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.error(f"Ошибка чтения: {e}")
        return last_obits

def save_state(obits):
    global last_obits
    last_obits = obits
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(obits, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Не сохранилось: {e}")

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
            month = months_ru.get(month_name, 10)  # По умолчанию октябрь
            death_date = datetime(year, month, day)
            return death_date >= datetime.now() - timedelta(hours=24)
    except:
        return False
    return False

def parse_obits():
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        response = requests.get(URL, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        obits = []
        # Улучшенный парсинг: ищем блоки с именами (h3/strong) и датами (' - ')
        entries = soup.find_all(['h3', 'strong', 'div'], class_=['mourning-entry', 'entry']) or soup.find_all(string=lambda t: t and ' - ' in str(t))
        for entry in entries:
            text = entry.get_text(strip=True) if entry.name else str(entry).strip()
            if len(text) < 15 or ' - ' not in text:
                continue
            parts = text.split(' - ', 1)
            name = parts[0].strip()
            dates = parts[1].strip()
            text_lower = text.lower()
            # Фильтр: только анкеты актеров/артистов
            if any(kw in text_lower for kw in ['актер', 'артист', 'режиссёр', 'театр', 'гимнаст', 'спорт']):
                obits.append({'name': name, 'date': dates})

        # Дедупликация + свежие
        seen = set()
        unique = []
        for obit in obits:
            key = f"{obit['name']} {obit['date']}"
            if key not in seen and is_recent(obit['date']):
                seen.add(key)
                unique.append(obit)

        logger.info(f"Парсинг страницы 12 (m12): найдено {len(unique)} свежих анкет.")
        return unique
    except Exception as e:
        logger.error(f"Ошибка парсинга страницы 12 (m12): {e}")
        return []

# === КОМАНДЫ ===
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now().strftime("%H:%M:%S")
    await update.message.reply_text(f"Pong! Бот жив.\nВремя: {now}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_obits = len(last_obits)
    last_check = stats["last_check"] or "ещё не было"
    checks = stats["checks_last_hour"]

    # Расчёт uptime
    delta = datetime.now() - stats["start_time"]
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    uptime = f"{hours}ч {minutes}м"

    message = f"<b>Статус бота:</b>\n\n"
    message += f"• Всего анкет в базе: <b>{total_obits}</b>\n"
    message += f"• Проверок за час: <b>{checks}</b>\n"
    message += f"• Последняя проверка: <b>{last_check}</b>\n"
    message += f"• Время работы: <b>{uptime}</b>\n"
    message += f"• Мониторит: <a href='{URL}'>Страница 12 (m12)</a>"

    await update.message.reply_text(message, parse_mode='HTML', disable_web_page_preview=True)

# === ПРОВЕРКА ОБНОВЛЕНИЙ ===
async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    stats["checks_last_hour"] += 1
    stats["last_check"] = datetime.now().strftime("%H:%M:%S")

    current_obits = parse_obits()
    if not current_obits:
        return

    last_keys = {f"{o['name']} {o['date']}" for o in last_obits}
    new_obits = [o for o in current_obits if f"{o['name']} {o['date']}" not in last_keys]

    if new_obits:
        message = "🪦 <b>Новые анкеты на странице 12:</b>\n\n"
        for obit in new_obits:
            message += f"• <b>{obit['name']}</b>\n  {obit['date']}\n\n"
        message += f"<a href='{URL}'>Подробнее на сайте</a>"

        try:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=message,
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            logger.info(f"Отправлено уведомление: {len(new_obits)} новых анкет на стр. 12.")
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")

        save_state(last_obits + new_obits)
    else:
        logger.info("Новых анкет на странице 12 нет.")

# === ФЕЙКОВЫЙ СЕРВЕР ДЛЯ RENDER ===
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is alive! Monitoring page 12 (m12) obituaries.')

def run_server():
    port = int(os.getenv('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    logger.info(f"Фейковый сервер запущен на порту {port}")
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# === ОСНОВНОЙ ЦИКЛ ===
def main():
    logger.info(f"Запуск бота. Мониторим страницу 12 (m12): {URL}")
    load_state()

    # Инициализация
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("status", status_command))

    # Запуск проверки каждую минуту
    app.job_queue.run_repeating(
        callback=check_updates,
        interval=random.randint(55, 65),
        first=10
    )

    # Запуск сброса статистики
    app.job_queue.run_repeating(
        callback=lambda ctx: schedule.run_pending(),
        interval=60,
        first=0
    )

    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()

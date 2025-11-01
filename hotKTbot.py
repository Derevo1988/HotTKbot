import os
import json
import time
import random
import logging
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    JobQueue
)
from dotenv import load_dotenv
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# === ЗАГРУЗКА ===
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

URL = 'https://www.kino-teatr.ru/mourn/y2025/m12/'
STATE_FILE = 'last_obits.json'

# === ЛОГИ ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot_debug.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Отключаем логирование requests и telegram, чтобы видеть только важное
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)

# === СТАТИСТИКА ===
stats = {
    "checks_last_hour": 0,
    "last_check": None,
    "start_time": datetime.now(),
    "last_successful_parse": None
}

last_obits = []

def load_state():
    global last_obits
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            last_obits = data
        logger.info(f"Загружено {len(last_obits)} анкет из файла состояния")
    except FileNotFoundError:
        logger.info("Файл состояния не найден, начинаем с чистого листа")
        last_obits = []
    except Exception as e:
        logger.error(f"Ошибка чтения файла состояния: {e}")
        last_obits = []

def save_state(obits):
    global last_obits
    last_obits = obits
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(obits, f, ensure_ascii=False, indent=2)
        logger.info(f"Сохранено {len(obits)} анкет в файл состояния")
    except Exception as e:
        logger.error(f"Ошибка сохранения состояния: {e}")

def is_recent(death_date_str):
    try:
        months_ru = {
            'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6,
            'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12
        }
        
        logger.debug(f"Проверка даты: {death_date_str}")
        
        if ' - ' in death_date_str:
            death_date_str = death_date_str.split(' - ')[-1].strip()
        
        parts = death_date_str.split()
        if len(parts) >= 3:
            day = int(parts[0])
            month_name = parts[1].lower()
            year = int(parts[2])
            month = months_ru.get(month_name)
            
            if month is None:
                logger.debug(f"Неизвестный месяц: {month_name}")
                return False
                
            death_date = datetime(year, month, day)
            is_recent = death_date >= datetime.now() - timedelta(hours=24)
            logger.debug(f"Дата {death_date} является свежей: {is_recent}")
            return is_recent
            
        logger.debug(f"Не удалось разобрать дату: {death_date_str}")
        return False
        
    except Exception as e:
        logger.warning(f"Ошибка в is_recent для '{death_date_str}': {e}")
        return False

def parse_obits():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
        'Connection': 'keep-alive',
    }
    
    try:
        logger.info("🚀 Начало парсинга страницы...")
        start_time = time.time()
        
        response = requests.get(URL, headers=headers, timeout=15)
        response.raise_for_status()
        
        parse_time = time.time() - start_time
        logger.info(f"📄 Страница загружена за {parse_time:.2f} сек, размер: {len(response.text)} символов")
        
        # Проверка на блокировку
        if any(blocked in response.text.lower() for blocked in ['cloudflare', 'access denied', 'доступ запрещен']):
            logger.warning("🛑 Обнаружена блокировка Cloudflare или аналогичная")
            return []
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Сохраним HTML для отладки
        with open('debug_page.html', 'w', encoding='utf-8') as f:
            f.write(response.text)
        logger.info("💾 HTML страницы сохранен в debug_page.html")

        obits = []
        
        # Попробуем разные стратегии поиска
        search_strategies = [
            # Поиск по текстовым элементам
            lambda: soup.find_all(string=lambda text: text and ' - ' in text),
            # Поиск по заголовкам
            lambda: soup.find_all(['h1', 'h2', 'h3', 'h4', 'strong', 'b']),
            # Поиск по дивам с текстом
            lambda: soup.find_all('div', string=lambda text: text and ' - ' in text),
        ]
        
        for i, strategy in enumerate(search_strategies):
            try:
                elements = strategy()
                logger.info(f"🔍 Стратегия {i+1}: найдено {len(elements)} элементов")
                
                for element in elements:
                    if hasattr(element, 'get_text'):
                        text = element.get_text(strip=True)
                    else:
                        text = str(element).strip()
                    
                    if not text or ' - ' not in text or len(text) < 10:
                        continue
                        
                    logger.debug(f"📝 Найден текст: {text[:100]}...")
                    
                    # Более надежное разделение
                    if ' - ' in text:
                        parts = text.split(' - ', 1)
                        if len(parts) == 2:
                            name = parts[0].strip()
                            dates = parts[1].strip()
                            
                            text_lower = text.lower()
                            keywords = ['актер', 'артист', 'режиссёр', 'театр', 'гимнаст', 'спорт', 'кино', 'сценарист', 'писатель']
                            
                            if any(kw in text_lower for kw in keywords):
                                logger.debug(f"✅ Найдена подходящая запись: {name} - {dates}")
                                if is_recent(dates):
                                    obits.append({'name': name, 'date': dates})
                                else:
                                    logger.debug(f"❌ Запись не свежая: {dates}")
                            
            except Exception as e:
                logger.error(f"Ошибка в стратегии поиска {i+1}: {e}")

        # Убираем дубликаты
        seen = set()
        unique = []
        for obit in obits:
            key = f"{obit['name']} {obit['date']}"
            if key not in seen:
                seen.add(key)
                unique.append(obit)

        logger.info(f"✅ Парсинг завершен: найдено {len(unique)} свежих анкет.")
        stats["last_successful_parse"] = datetime.now().isoformat()
        return unique
        
    except requests.exceptions.Timeout:
        logger.error("⏰ Таймаут при запросе к сайту")
        return []
    except requests.exceptions.RequestException as e:
        logger.error(f"🌐 Ошибка сети: {e}")
        return []
    except Exception as e:
        logger.error(f"💥 Неожиданная ошибка парсинга: {e}", exc_info=True)
        return []

# === УВЕДОМЛЕНИЕ ПРИ ЗАПУСКЕ ===
async def startup_notification(context: ContextTypes.DEFAULT_TYPE):
    try:
        now = datetime.now().strftime("%H:%M:%S")
        message = f"🟢 Бот запущен и работает!\nВремя: {now}\nМониторит: <a href='{URL}'>Страница 12 (m12)</a>"
        await context.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='HTML')
        logger.info("📤 Уведомление о запуске отправлено.")
    except Exception as e:
        logger.error(f"❌ Не удалось отправить уведомление: {e}")

# === КОМАНДЫ ===
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now().strftime("%H:%M:%S")
    await update.message.reply_text(f"🟢 Pong! Бот жив.\nВремя: {now}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_obits = len(last_obits)
    last_check = stats["last_check"] or "ещё не было"
    checks = stats["checks_last_hour"]
    last_parse = stats["last_successful_parse"] or "ещё не было"

    delta = datetime.now() - stats["start_time"]
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    uptime = f"{hours}ч {minutes}м"

    message = f"<b>Статус бота:</b>\n\n"
    message += f"• Всего анкет в базе: <b>{total_obits}</b>\n"
    message += f"• Проверок за час: <b>{checks}</b>\n"
    message += f"• Последняя проверка: <b>{last_check}</b>\n"
    message += f"• Успешный парсинг: <b>{last_parse}</b>\n"
    message += f"• Время работы: <b>{uptime}</b>\n"
    message += f"• Мониторит: <a href='{URL}'>Страница 12 (m12)</a>"

    await update.message.reply_text(message, parse_mode='HTML', disable_web_page_preview=True)

# === ПРОВЕРКА ОБНОВЛЕНИЙ ===
async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    try:
        stats["checks_last_hour"] += 1
        current_time = datetime.now().strftime("%H:%M:%S")
        stats["last_check"] = current_time

        logger.info(f"🔍 Проверка обновлений #{stats['checks_last_hour']} в {current_time}")
        
        current_obits = parse_obits()
        
        if current_obits is None:
            current_obits = []

        last_keys = {f"{o['name']} {o['date']}" for o in last_obits}
        new_obits = [o for o in current_obits if f"{o['name']} {o['date']}" not in last_keys]

        logger.info(f"📊 Результат: {len(current_obits)} текущих, {len(new_obits)} новых")

        if new_obits:
            message = "🪦 <b>Новые анкеты на странице 12:</b>\n\n"
            for obit in new_obits:
                message += f"• <b>{obit['name']}</b>\n  {obit['date']}\n\n"
            message += f"<a href='{URL}'>Подробнее</a>"

            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=message,
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            logger.info(f"📤 Отправлено {len(new_obits)} новых анкет.")
            
            # Обновляем состояние
            save_state(last_obits + new_obits)
        else:
            logger.info("✅ Новых анкет не найдено")
            
    except Exception as e:
        logger.error(f"💥 Ошибка в check_updates: {e}", exc_info=True)

# === СБРОС СТАТИСТИКИ ===
async def reset_hourly(context: ContextTypes.DEFAULT_TYPE):
    stats["checks_last_hour"] = 0
    logger.info("🔄 Сброс счётчика проверок за час.")

# === ФЕЙКОВЫЙ СЕРВЕР ===
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is alive!')
    
    def log_message(self, format, *args):
        return

def run_server():
    port = int(os.getenv('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    logger.info(f"🌐 Фейковый сервер запущен на порту {port}")
    server.serve_forever()

# === ОСНОВНОЙ ЦИКЛ ===
def main():
    logger.info("🚀 Запуск бота...")
    load_state()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("status", status_command))

    # Запускаем сервер в отдельном потоке
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # Настраиваем job queue после создания app
    job_queue = app.job_queue
    
    # Уведомление при запуске
    job_queue.run_once(startup_notification, when=5)
    
    # Проверка каждую минуту
    job_queue.run_repeating(check_updates, interval=60, first=10)
    
    # Сброс статистики каждый час
    job_queue.run_repeating(reset_hourly, interval=3600, first=3600)

    try:
        logger.info("🤖 Бот начал работу (polling)...")
        app.run_polling(
            drop_pending_updates=True,
            close_loop=False,
            stop_signals=[]
        )
    except KeyboardInterrupt:
        logger.info("⏹️ Бот остановлен пользователем")
    except Exception as e:
        logger.critical(f"💥 КРИТИЧЕСКАЯ ОШИБКА: {e}", exc_info=True)
        raise
    finally:
        logger.info("🔚 Бот завершил работу")

if __name__ == '__main__':
    main()
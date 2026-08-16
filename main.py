import os
import re
import time
import signal
import logging
import threading
import html
import requests
import json
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Optional, Dict, List
from urllib.parse import urlparse, unquote
from http.server import BaseHTTPRequestHandler, HTTPServer
from concurrent.futures import ThreadPoolExecutor

import mysql.connector
from mysql.connector import pooling
from imap_tools import MailBox

# =============================================================================
# LOGGING
# =============================================================================
log_formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s")
logger = logging.getLogger("rental_bot")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

file_handler = RotatingFileHandler("bot.log", maxBytes=5*1024*1024, backupCount=5, encoding="utf-8")
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

# =============================================================================
# CONFIG
# =============================================================================
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("MYSQL_URL")
RAILWAY_PUBLIC_URL = os.environ.get("RAILWAY_PUBLIC_URL")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")

MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5
MAX_IDLE_THREADS = 20
DAILY_PURCHASE_LIMIT = 3
AI_CLEANUP_INTERVAL = 3600

def get_db_config() -> dict:
    if not DATABASE_URL:
        raise ValueError("Критическая ошибка: Переменная DATABASE_URL/MYSQL_URL не найдена в Railway!")
    try:
        parsed = urlparse(DATABASE_URL)
        return {
            "host": parsed.hostname,
            "user": parsed.username,
            "password": unquote(parsed.password) if parsed.password else "",
            "database": parsed.path.lstrip("/"),
            "port": parsed.port if parsed.port else 3306,
            "charset": "utf8mb4",
            "connect_timeout": 10
        }
    except Exception as parse_err:
        raise ValueError(f"Ошибка парсинга строки базы данных: {parse_err}")

POLL_INTERVAL = 15
RENT_DURATION_HOURS = 2
DB_MAX_RETRIES = 3
MAX_REST_PERCENT = 0.40

# =============================================================================
# SHUTDOWN
# =============================================================================
SHUTDOWN_EVENT = threading.Event()

def shutdown_handler(signum, frame):
    logger.info(f"Получен сигнал {signum}. Инициируем плавную остановку...")
    SHUTDOWN_EVENT.set()

signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

# =============================================================================
# ENV VARIABLES
# =============================================================================
MASTER_EMAIL = os.environ.get("MASTER_EMAIL")
MASTER_PASSWORD = os.environ.get("MASTER_PASSWORD")
IMAP_MASTER_SERVER = os.environ.get("IMAP_MASTER_SERVER", "imap.mail.ru")
PAYGAME_SESSION = os.environ.get("PAYGAME_SESSION")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")  # Используется для DeepSeek

# =============================================================================
# DEEPSEEK AI
# =============================================================================
import openai

DEEPSEEK_API_KEY = OPENAI_API_KEY

def setup_deepseek():
    if DEEPSEEK_API_KEY:
        openai.api_key = DEEPSEEK_API_KEY
        openai.api_base = "https://api.deepseek.com"
        logger.info("✅ DeepSeek API успешно подключен.")
        return True
    else:
        logger.warning("⚠️ OPENAI_API_KEY не задан. ИИ-поддержка отключена.")
        return False

DEEPSEEK_ENABLED = setup_deepseek()

AI_CHAT_HISTORY: Dict[str, List[Dict]] = {}
AI_CHAT_HISTORY_LOCK = threading.Lock()

# =============================================================================
# DATABASE POOL
# =============================================================================
try:
    db_pool = pooling.MySQLConnectionPool(
        pool_name="bot_pool",
        pool_size=10,
        pool_reset_session=True,
        **get_db_config()
    )
    logger.info("✅ Пул соединений MySQL успешно инициализирован.")
except Exception as pool_err:
    logger.critical(f"Не удалось инициализировать пул базы данных: {pool_err}")
    db_pool = None

# =============================================================================
# PAYGAME SESSION
# =============================================================================
PAYGAME_HTTP_SESSION = requests.Session()
adapter = requests.adapters.HTTPAdapter(
    max_retries=requests.adapters.Retry(
        total=3, 
        backoff_factor=1, 
        status_forcelist=[500, 502, 503, 504]
    )
)
PAYGAME_HTTP_SESSION.mount("https://", adapter)

if PAYGAME_SESSION and PAYGAME_SESSION not in ["ВСТАВЬТЕ_СЮДА", "ВСТАВЬТЕ_СЮДА_ЗНАЧЕНИЕ_COOKIE_SESSION"]:
    PAYGAME_HTTP_SESSION.cookies.set("session", PAYGAME_SESSION, domain="paygame.ru")
else:
    logger.error("❌ PAYGAME_SESSION не задан или невалиден!")

PAYGAME_HTTP_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
})

def verify_paygame_session():
    try:
        test_response = PAYGAME_HTTP_SESSION.get("https://paygame.ru/api/v1/user", timeout=10)
        if test_response.status_code == 200:
            logger.info("✅ Paygame session валиден")
            return True
        else:
            logger.error(f"❌ Paygame session невалиден: {test_response.status_code}")
            return False
    except Exception as e:
        logger.error(f"❌ Ошибка проверки Paygame session: {e}")
        return False

# =============================================================================
# THREAD POOL
# =============================================================================
idle_thread_pool = ThreadPoolExecutor(max_workers=MAX_IDLE_THREADS)

# =============================================================================
# CONSTANTS
# =============================================================================
CODE_REGEX_PATTERN = os.environ.get("CODE_REGEX_PATTERN", r'\b\d{6}\b')
LOGOUT_LINK_PATTERN = os.environ.get("LOGOUT_LINK_PATTERN", r'https://[\S]+?(?:logout|not-me|disavow|deauthorize|security|cancel|выйти|завершить|сессию)[\S]*?(?=["\'>\s]|$)')

STATUS_FREE = 0
STATUS_WAIT_CODE = 1
STATUS_IN_RENT = 2
STATUS_MANUAL_RESET = 3
STATUS_REST = 4

# =============================================================================
# HELPERS
# =============================================================================
def mask_email(email_str: str) -> str:
    if not email_str or "@" not in email_str: return "***"
    name, domain = email_str.split("@", 1)
    return f"{name[:2]}***@{domain}" if len(name) > 2 else f"***@{domain}"

def clean_extracted_url(raw_url: str) -> str:
    if isinstance(raw_url, list):
        raw_url = raw_url[0]
    url = html.unescape(raw_url)
    url = url.replace("=\n", "").replace("=\r\n", "")
    url = re.sub(r'["\'><\s\)\(\]]', '', url)
    return url.strip()

def send_telegram_notification(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление в Telegram: {e}")

# =============================================================================
# DATABASE
# =============================================================================
@contextmanager
def db_connection():
    connection = None
    backoff = 1
    for attempt in range(DB_MAX_RETRIES):
        try:
            connection = db_pool.get_connection()
            connection.ping(reconnect=True)
            yield connection
            break
        except Exception as err:
            logger.warning(f"Сбой получения соединения из пула (попытка {attempt+1}/{DB_MAX_RETRIES}): {err}")
            if connection: 
                try: connection.rollback() 
                except: pass
            if attempt == DB_MAX_RETRIES - 1: raise
            time.sleep(backoff)
            backoff *= 2
    if connection:
        try: connection.close()
        except: pass

def initialize_database():
    try:
        with db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS accounts (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        email VARCHAR(255) NOT NULL,
                        email_password VARCHAR(255) NOT NULL,
                        imap_server VARCHAR(255) NOT NULL,
                        status INT DEFAULT 0,
                        trophies INT DEFAULT 0,
                        price INT DEFAULT 100,
                        rent_hours INT DEFAULT 2,
                        chat_id VARCHAR(255) DEFAULT NULL,
                        rent_end_time DATETIME DEFAULT NULL,
                        rest_until DATETIME DEFAULT NULL,
                        last_status_update DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_status_rent (status, rent_end_time),
                        INDEX idx_chat_id (chat_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                """)
                
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS sales (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        chat_id VARCHAR(255) NOT NULL,
                        account_id INT NOT NULL,
                        account_email VARCHAR(255) NOT NULL,
                        trophies INT DEFAULT 0,
                        price INT DEFAULT 0,
                        rent_hours INT DEFAULT 2,
                        sold_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        status VARCHAR(50) DEFAULT 'completed'
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                """)
                
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS logs (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        event_type VARCHAR(50) NOT NULL,
                        chat_id VARCHAR(255) DEFAULT NULL,
                        account_id INT DEFAULT NULL,
                        message TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_event_type (event_type),
                        INDEX idx_created_at (created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                """)
                
                conn.commit()
        logger.info("✅ База данных успешно проинициализирована.")
    except Exception:
        logger.exception("Критическая ошибка инициализации структуры БД")

def log_event(event_type: str, chat_id: Optional[str] = None, account_id: Optional[int] = None, message: str = ""):
    try:
        with db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO logs (event_type, chat_id, account_id, message)
                    VALUES (%s, %s, %s, %s)
                """, (event_type, chat_id, account_id, message))
                conn.commit()
    except Exception:
        logger.exception("Ошибка записи лога")

# =============================================================================
# PAYGAME SEND
# =============================================================================
def send_to_paygame(chat_id: str, text: str, retries: int = MAX_RETRY_ATTEMPTS) -> bool:
    if not chat_id:
        logger.error("chat_id пустой!")
        return False
    
    url = f"https://paygame.ru{chat_id}/messages"
    for attempt in range(retries):
        try:
            res = PAYGAME_HTTP_SESSION.post(url, json={"message": text}, timeout=10)
            if res.status_code == 200:
                log_event("message_sent", chat_id, None, text[:50])
                return True
            logger.warning(f"Ошибка Paygame API (попытка {attempt+1}/{retries}): {res.status_code}")
            time.sleep(RETRY_DELAY_SECONDS)
        except Exception as e:
            logger.warning(f"Ошибка отправки (попытка {attempt+1}/{retries}): {e}")
            time.sleep(RETRY_DELAY_SECONDS)
    logger.error(f"Не удалось отправить сообщение в чат {chat_id} после {retries} попыток")
    log_event("send_failed", chat_id, None, str(text[:50]))
    return False

# =============================================================================
# AI ASSISTANT (DEEPSEEK)
# =============================================================================
def clear_old_ai_history():
    while not SHUTDOWN_EVENT.is_set():
        time.sleep(AI_CLEANUP_INTERVAL)
        with AI_CHAT_HISTORY_LOCK:
            now = time.time()
            chats_to_remove = []
            for chat_id, history in AI_CHAT_HISTORY.items():
                if history and history[-1].get('timestamp', now) < now - 7200:
                    chats_to_remove.append(chat_id)
            for chat_id in chats_to_remove:
                del AI_CHAT_HISTORY[chat_id]
            if chats_to_remove:
                logger.info(f"Очищено {len(chats_to_remove)} старых чатов из истории AI")

def ask_ai_assistant(chat_id: str, buyer_message: str, current_bot_status: str) -> str:
    if not DEEPSEEK_ENABLED or not DEEPSEEK_API_KEY:
        return "Извините, я автоматический бот. Пожалуйста, следуйте инструкциям выше."
    
    with AI_CHAT_HISTORY_LOCK:
        if chat_id not in AI_CHAT_HISTORY:
            AI_CHAT_HISTORY[chat_id] = []
        if len(AI_CHAT_HISTORY[chat_id]) > 10:
            AI_CHAT_HISTORY[chat_id] = AI_CHAT_HISTORY[chat_id][-10:]
    
    system_prompt = f"""
Ты — профессиональный ИИ-ассистент сервиса аренды аккаунтов на Paygame.

ТЫ МОЖЕШЬ:
- Отвечать на вопросы о времени аренды (2 часа)
- Объяснять процесс: оплата → получение почты → получение кода → вход в игру

ТЫ НЕ МОЖЕШЬ:
- Никогда не называть почту, пароль или код до оплаты
- Никогда не давать данные аккаунта до подтверждения оплаты
- Никогда не просить дополнительные коды или пароли

ТЕКУЩИЙ СТАТУС ЗАКАЗА: {current_bot_status}
"""
    
    try:
        messages = [{"role": "system", "content": system_prompt}]
        with AI_CHAT_HISTORY_LOCK:
            messages.extend(AI_CHAT_HISTORY.get(chat_id, [])[-10:])
        messages.append({"role": "user", "content": buyer_message})
        
        response = openai.ChatCompletion.create(
            model="deepseek-v4-flash",
            messages=messages,
            timeout=10,
            temperature=0.1
        )
        ai_reply = response.choices[0].message.content
        
        with AI_CHAT_HISTORY_LOCK:
            if chat_id not in AI_CHAT_HISTORY:
                AI_CHAT_HISTORY[chat_id] = []
            AI_CHAT_HISTORY[chat_id].append({"role": "user", "content": buyer_message, "timestamp": time.time()})
            AI_CHAT_HISTORY[chat_id].append({"role": "assistant", "content": ai_reply, "timestamp": time.time()})
        
        return ai_reply
    except Exception as e:
        logger.error(f"Ошибка DeepSeek API: {e}")
        return "Я зафиксировал ваш вопрос. Администратор ответит вам в ближайшее время."

# =============================================================================
# WEB ADMIN PANEL
# =============================================================================
def get_admin_html():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Админ панель - Аренда аккаунтов</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
            .container { max-width: 1400px; margin: 0 auto; }
            h1 { color: #58a6ff; margin-bottom: 20px; border-bottom: 1px solid #30363d; padding-bottom: 15px; }
            .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; margin-bottom: 30px; }
            .stat-card { background: #161b22; border-radius: 10px; padding: 20px; text-align: center; border: 1px solid #30363d; }
            .stat-card .number { font-size: 28px; font-weight: bold; color: #58a6ff; }
            .stat-card .label { color: #8b949e; font-size: 14px; margin-top: 5px; }
            .stat-card .number.green { color: #3fb950; }
            .stat-card .number.orange { color: #d29922; }
            .stat-card .number.red { color: #f85149; }
            .stat-card .number.gold { color: #f0c94d; }
            
            .tabs { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
            .tabs button { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 10px 20px; border-radius: 8px; cursor: pointer; font-size: 14px; }
            .tabs button.active { background: #58a6ff; color: #0d1117; border-color: #58a6ff; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }
            
            table { width: 100%; border-collapse: collapse; font-size: 14px; }
            th { background: #161b22; color: #8b949e; padding: 12px; text-align: left; border-bottom: 2px solid #30363d; }
            td { padding: 10px 12px; border-bottom: 1px solid #21262d; }
            tr:hover { background: #161b22; }
            .status-badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 500; }
            .status-free { background: #2ea043; color: #fff; }
            .status-wait { background: #d29922; color: #fff; }
            .status-rent { background: #58a6ff; color: #fff; }
            .status-reset { background: #f85149; color: #fff; }
            .status-rest { background: #8b949e; color: #fff; }
            
            .form-add { background: #161b22; padding: 20px; border-radius: 10px; margin-bottom: 30px; border: 1px solid #30363d; display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; align-items: end; }
            .form-add input { background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 10px; border-radius: 6px; width: 100%; }
            .form-add input:focus { border-color: #58a6ff; outline: none; }
            .form-add button { background: #2ea043; border: none; color: #fff; padding: 10px 20px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 14px; height: 42px; }
            .form-add button:hover { background: #3fb950; }
            .btn-delete { background: #f85149; border: none; color: #fff; padding: 5px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; }
            .btn-delete:hover { background: #da3633; }
            
            .refresh-btn { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 8px 16px; border-radius: 6px; cursor: pointer; margin-bottom: 15px; }
            .refresh-btn:hover { background: #30363d; }
            
            @media (max-width: 600px) { .stats { grid-template-columns: repeat(2, 1fr); } .form-add { grid-template-columns: 1fr; } table { font-size: 12px; } td, th { padding: 6px 8px; } }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🤖 Панель управления арендой аккаунтов</h1>
            
            <div class="stats" id="stats">
                <div class="stat-card"><div class="number" id="stat-total">-</div><div class="label">Всего аккаунтов</div></div>
                <div class="stat-card"><div class="number green" id="stat-free">-</div><div class="label">Свободные</div></div>
                <div class="stat-card"><div class="number orange" id="stat-rented">-</div><div class="label">В аренде</div></div>
                <div class="stat-card"><div class="number red" id="stat-resting">-</div><div class="label">На отдыхе</div></div>
                <div class="stat-card"><div class="number gold" id="stat-revenue">-</div><div class="label">Выручка (₽)</div></div>
                <div class="stat-card"><div class="number green" id="stat-today">-</div><div class="label">Продаж сегодня</div></div>
            </div>
            
            <div class="tabs">
                <button class="active" onclick="showTab('accounts')">📋 Аккаунты</button>
                <button onclick="showTab('sales')">💰 Продажи</button>
                <button onclick="showTab('logs')">📜 Логи</button>
                <button onclick="showTab('add')">➕ Добавить</button>
            </div>
            
            <div id="tab-accounts" class="tab-content active">
                <button class="refresh-btn" onclick="loadAccounts()">🔄 Обновить</button>
                <div style="overflow-x: auto;">
                    <table>
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>Почта</th>
                                <th>🏆</th>
                                <th>Цена</th>
                                <th>Часы</th>
                                <th>Статус</th>
                                <th>Чат</th>
                                <th>Действия</th>
                            </tr>
                        </thead>
                        <tbody id="accounts-table">
                            <tr><td colspan="8" style="text-align:center;color:#8b949e;">Загрузка...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
            
            <div id="tab-sales" class="tab-content">
                <button class="refresh-btn" onclick="loadSales()">🔄 Обновить</button>
                <div style="overflow-x: auto;">
                    <table>
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>Чат</th>
                                <th>Почта</th>
                                <th>🏆</th>
                                <th>Цена</th>
                                <th>Часы</th>
                                <th>Дата</th>
                            </tr>
                        </thead>
                        <tbody id="sales-table">
                            <tr><td colspan="7" style="text-align:center;color:#8b949e;">Загрузка...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
            
            <div id="tab-logs" class="tab-content">
                <button class="refresh-btn" onclick="loadLogs()">🔄 Обновить</button>
                <div style="overflow-x: auto;">
                    <table>
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>Тип</th>
                                <th>Чат</th>
                                <th>Аккаунт</th>
                                <th>Сообщение</th>
                                <th>Дата</th>
                            </tr>
                        </thead>
                        <tbody id="logs-table">
                            <tr><td colspan="6" style="text-align:center;color:#8b949e;">Загрузка...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
            
            <div id="tab-add" class="tab-content">
                <div class="form-add">
                    <div><input type="text" id="add-email" placeholder="Почта аккаунта" /></div>
                    <div><input type="text" id="add-password" placeholder="Пароль от почты" /></div>
                    <div><input type="text" id="add-imap" placeholder="IMAP сервер" /></div>
                    <div><input type="number" id="add-trophies" placeholder="Кубки" value="0" min="0" /></div>
                    <div><input type="number" id="add-price" placeholder="Цена" value="100" min="1" /></div>
                    <div><input type="number" id="add-rent-hours" placeholder="Часы" value="2" min="1" /></div>
                    <div><button onclick="addAccount()">➕ Добавить аккаунт</button></div>
                </div>
                <div id="add-result" style="margin-top:10px;color:#3fb950;"></div>
            </div>
        </div>
        
        <script>
            function showTab(name) {
                document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
                document.querySelectorAll('.tabs button').forEach(el => el.classList.remove('active'));
                document.getElementById('tab-' + name).classList.add('active');
                document.querySelector(`.tabs button[onclick="showTab('${name}')"]`).classList.add('active');
                if (name === 'accounts') loadAccounts();
                if (name === 'sales') loadSales();
                if (name === 'logs') loadLogs();
            }
            
            async function loadStats() {
                try {
                    const res = await fetch('/api/stats');
                    const data = await res.json();
                    document.getElementById('stat-total').textContent = data.total || 0;
                    document.getElementById('stat-free').textContent = data.free || 0;
                    document.getElementById('stat-rented').textContent = data.rented || 0;
                    document.getElementById('stat-resting').textContent = data.resting || 0;
                    document.getElementById('stat-revenue').textContent = data.revenue || 0;
                    document.getElementById('stat-today').textContent = data.today_sales || 0;
                } catch(e) { console.error(e); }
            }
            
            async function loadAccounts() {
                try {
                    const res = await fetch('/api/accounts');
                    const data = await res.json();
                    const tbody = document.getElementById('accounts-table');
                    if (!data.length) {
                        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#8b949e;">Нет аккаунтов</td></tr>';
                        return;
                    }
                    tbody.innerHTML = data.map(a => `
                        <tr>
                            <td>${a.id}</td>
                            <td style="font-size:12px;">${a.email}</td>
                            <td>${a.trophies}</td>
                            <td>${a.price}₽</td>
                            <td>${a.rent_hours}</td>
                            <td><span class="status-badge status-${a.status_code === 0 ? 'free' : a.status_code === 1 ? 'wait' : a.status_code === 2 ? 'rent' : a.status_code === 3 ? 'reset' : 'rest'}">${a.status}</span></td>
                            <td style="font-size:11px;">${a.chat_id}</td>
                            <td><button class="btn-delete" onclick="deleteAccount(${a.id})">🗑️</button></td>
                        </tr>
                    `).join('');
                } catch(e) { console.error(e); }
            }
            
            async function loadSales() {
                try {
                    const res = await fetch('/api/sales');
                    const data = await res.json();
                    const tbody = document.getElementById('sales-table');
                    if (!data.length) {
                        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#8b949e;">Нет продаж</td></tr>';
                        return;
                    }
                    tbody.innerHTML = data.map(s => `
                        <tr>
                            <td>${s.id}</td>
                            <td style="font-size:11px;">${s.chat_id}</td>
                            <td style="font-size:12px;">${s.account_email}</td>
                            <td>${s.trophies}</td>
                            <td>${s.price}₽</td>
                            <td>${s.rent_hours}</td>
                            <td style="font-size:12px;">${s.sold_at}</td>
                        </tr>
                    `).join('');
                } catch(e) { console.error(e); }
            }
            
            async function loadLogs() {
                try {
                    const res = await fetch('/api/logs');
                    const data = await res.json();
                    const tbody = document.getElementById('logs-table');
                    if (!data.length) {
                        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#8b949e;">Нет логов</td></tr>';
                        return;
                    }
                    tbody.innerHTML = data.map(l => `
                        <tr>
                            <td>${l.id}</td>
                            <td>${l.event_type}</td>
                            <td style="font-size:11px;">${l.chat_id}</td>
                            <td>${l.account_id}</td>
                            <td style="font-size:12px;">${l.message}</td>
                            <td style="font-size:12px;">${l.created_at}</td>
                        </tr>
                    `).join('');
                } catch(e) { console.error(e); }
            }
            
            async function addAccount() {
                const data = {
                    email: document.getElementById('add-email').value.trim(),
                    password: document.getElementById('add-password').value.trim(),
                    imap_server: document.getElementById('add-imap').value.trim(),
                    trophies: parseInt(document.getElementById('add-trophies').value) || 0,
                    price: parseInt(document.getElementById('add-price').value) || 100,
                    rent_hours: parseInt(document.getElementById('add-rent-hours').value) || 2
                };
                if (!data.email || !data.password || !data.imap_server) {
                    document.getElementById('add-result').textContent = '❌ Заполните все поля!';
                    document.getElementById('add-result').style.color = '#f85149';
                    return;
                }
                if (data.trophies < 0 || data.price <= 0 || data.rent_hours <= 0) {
                    document.getElementById('add-result').textContent = '❌ Проверьте значения (цена и часы должны быть > 0)';
                    document.getElementById('add-result').style.color = '#f85149';
                    return;
                }
                try {
                    const res = await fetch('/api/account/add', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(data)
                    });
                    const result = await res.json();
                    if (result.success) {
                        document.getElementById('add-result').textContent = '✅ Аккаунт добавлен!';
                        document.getElementById('add-result').style.color = '#3fb950';
                        document.getElementById('add-email').value = '';
                        document.getElementById('add-password').value = '';
                        document.getElementById('add-imap').value = '';
                        loadStats();
                        loadAccounts();
                    } else {
                        document.getElementById('add-result').textContent = '❌ ' + (result.error || 'Ошибка');
                        document.getElementById('add-result').style.color = '#f85149';
                    }
                } catch(e) { console.error(e); }
            }
            
            async function deleteAccount(id) {
                if (!confirm('Удалить аккаунт ID ' + id + '?')) return;
                try {
                    await fetch('/api/account/delete', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({id: id})
                    });
                    loadStats();
                    loadAccounts();
                } catch(e) { console.error(e); }
            }
            
            loadStats();
            loadAccounts();
            setInterval(() => { loadStats(); }, 30000);
        </script>
    </body>
    </html>
    """

class AdminPanelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(get_admin_html().encode('utf-8'))
        elif self.path == '/api/stats':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(get_stats()).encode('utf-8'))
        elif self.path == '/api/accounts':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(get_accounts()).encode('utf-8'))
        elif self.path == '/api/sales':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(get_sales()).encode('utf-8'))
        elif self.path == '/api/logs':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(get_logs()).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()
    
    def do_POST(self):
        if self.path == '/api/account/add':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            result = add_account(data)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode('utf-8'))
        elif self.path == '/api/account/delete':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            result = delete_account(data.get('id'))
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        return

def get_stats():
    try:
        with db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM accounts")
                total = cursor.fetchone()[0]
                
                cursor.execute("SELECT COUNT(*) FROM accounts WHERE status = %s", (STATUS_FREE,))
                free = cursor.fetchone()[0]
                
                cursor.execute("SELECT COUNT(*) FROM accounts WHERE status = %s", (STATUS_IN_RENT,))
                rented = cursor.fetchone()[0]
                
                cursor.execute("SELECT COUNT(*) FROM accounts WHERE status = %s", (STATUS_REST,))
                resting = cursor.fetchone()[0]
                
                cursor.execute("SELECT SUM(price) FROM sales")
                revenue = cursor.fetchone()[0] or 0
                
                cursor.execute("SELECT COUNT(*) FROM sales WHERE DATE(sold_at) = CURDATE()")
                today_sales = cursor.fetchone()[0]
                
                return {
                    'total': total,
                    'free': free,
                    'rented': rented,
                    'resting': resting,
                    'revenue': revenue,
                    'today_sales': today_sales
                }
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        return {'error': str(e)}

def get_accounts():
    try:
        with db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT id, email, trophies, price, rent_hours, status, chat_id, 
                           rent_end_time, rest_until, last_status_update 
                    FROM accounts 
                    ORDER BY id DESC
                """)
                rows = cursor.fetchall()
                accounts = []
                status_map = {
                    STATUS_FREE: 'Свободен',
                    STATUS_WAIT_CODE: 'Ожидание кода',
                    STATUS_IN_RENT: 'В аренде',
                    STATUS_MANUAL_RESET: 'Требует сброса',
                    STATUS_REST: 'Отдых'
                }
                for row in rows:
                    accounts.append({
                        'id': row[0],
                        'email': row[1],
                        'trophies': row[2],
                        'price': row[3],
                        'rent_hours': row[4],
                        'status': status_map.get(row[5], 'Неизвестно'),
                        'status_code': row[5],
                        'chat_id': row[6] or '-',
                        'rent_end_time': str(row[7]) if row[7] else '-',
                        'rest_until': str(row[8]) if row[8] else '-',
                        'last_update': str(row[9]) if row[9] else '-'
                    })
                return accounts
    except Exception as e:
        logger.error(f"Ошибка получения аккаунтов: {e}")
        return []

def get_sales():
    try:
        with db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT id, chat_id, account_email, trophies, price, rent_hours, sold_at, status
                    FROM sales 
                    ORDER BY sold_at DESC 
                    LIMIT 100
                """)
                rows = cursor.fetchall()
                sales = []
                for row in rows:
                    sales.append({
                        'id': row[0],
                        'chat_id': row[1],
                        'account_email': row[2],
                        'trophies': row[3],
                        'price': row[4],
                        'rent_hours': row[5],
                        'sold_at': str(row[6]) if row[6] else '-',
                        'status': row[7] or 'completed'
                    })
                return sales
    except Exception as e:
        logger.error(f"Ошибка получения продаж: {e}")
        return []

def get_logs():
    try:
        with db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT id, event_type, chat_id, account_id, message, created_at
                    FROM logs 
                    ORDER BY id DESC 
                    LIMIT 50
                """)
                rows = cursor.fetchall()
                logs = []
                for row in rows:
                    logs.append({
                        'id': row[0],
                        'event_type': row[1],
                        'chat_id': row[2] or '-',
                        'account_id': row[3] or '-',
                        'message': row[4] or '-',
                        'created_at': str(row[5]) if row[5] else '-'
                    })
                return logs
    except Exception as e:
        logger.error(f"Ошибка получения логов: {e}")
        return []

def add_account(data):
    try:
        email = data.get('email', '').strip()
        password = data.get('password', '').strip()
        imap_server = data.get('imap_server', '').strip()
        trophies = int(data.get('trophies', 0))
        price = int(data.get('price', 100))
        rent_hours = int(data.get('rent_hours', 2))
        
        if not email or not password or not imap_server:
            return {'success': False, 'error': 'Все поля обязательны'}
        if trophies < 0:
            return {'success': False, 'error': 'Кубки не могут быть отрицательными'}
        if price <= 0:
            return {'success': False, 'error': 'Цена должна быть больше 0'}
        if rent_hours <= 0:
            return {'success': False, 'error': 'Часы аренды должны быть больше 0'}
        if '@' not in email:
            return {'success': False, 'error': 'Некорректный email'}
        
        with db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO accounts (email, email_password, imap_server, trophies, price, rent_hours, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (email, password, imap_server, trophies, price, rent_hours, STATUS_FREE))
                conn.commit()
        
        send_telegram_notification(f"📥 Добавлен аккаунт: {mask_email(email)} ({trophies}🏆, {price}₽)")
        log_event("account_added", None, None, f"{mask_email(email)} ({trophies}🏆)")
        return {'success': True}
    except Exception as e:
        logger.error(f"Ошибка добавления аккаунта: {e}")
        return {'success': False, 'error': str(e)}

def delete_account(account_id):
    try:
        with db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT email FROM accounts WHERE id = %s", (account_id,))
                row = cursor.fetchone()
                if not row:
                    return {'success': False, 'error': 'Аккаунт не найден'}
                email = row[0]
                cursor.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
                conn.commit()
        send_telegram_notification(f"🗑️ Удален аккаунт: {mask_email(email)}")
        log_event("account_deleted", None, account_id, mask_email(email))
        return {'success': True}
    except Exception as e:
        logger.error(f"Ошибка удаления аккаунта: {e}")
        return {'success': False, 'error': str(e)}

def run_admin_server():
    port = int(os.environ.get("ADMIN_PORT", "8080"))
    try:
        server = HTTPServer(("0.0.0.0", port), AdminPanelHandler)
        logger.info(f"✅ Админ панель запущена на порту {port}")
        while not SHUTDOWN_EVENT.is_set():
            server.handle_request()
    except Exception as e:
        logger.error(f"Не удалось запустить админ панель: {e}")

# =============================================================================
# BOT LOGIC
# =============================================================================
def allocate_account_and_start_idle(chat_id: str, exclude_account_id: Optional[int] = None, target_trophies: Optional[int] = None):
    try:
        with db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM sales WHERE chat_id = %s AND DATE(sold_at) = CURDATE()", (chat_id,))
                if cursor.fetchone()[0] >= DAILY_PURCHASE_LIMIT:
                    send_to_paygame(chat_id, f"❌ Вы уже купили {DAILY_PURCHASE_LIMIT} аккаунтов сегодня. Лимит исчерпан.")
                    return
            
            with conn.cursor() as check_cursor:
                if not exclude_account_id:
                    check_cursor.execute("SELECT id FROM accounts WHERE chat_id = %s AND status IN (%s, %s)", 
                                       (chat_id, STATUS_WAIT_CODE, STATUS_IN_RENT))
                    if check_cursor.fetchone():
                        logger.warning(f"Заказ для чата {chat_id} уже обрабатывается.")
                        return
            
            conn.start_transaction(isolation_level='READ COMMITTED')
            with conn.cursor(dictionary=True) as cursor:
                if exclude_account_id and target_trophies is not None:
                    target_trophies = target_trophies or 0
                    min_trophies = int(target_trophies * 0.8)
                    max_trophies = int(target_trophies * 1.2)
                    cursor.execute("""
                        SELECT * FROM accounts
                        WHERE status = %s AND id != %s AND trophies BETWEEN %s AND %s
                        LIMIT 1 FOR UPDATE SKIP LOCKED
                    """, (STATUS_FREE, exclude_account_id, min_trophies, max_trophies))
                    account = cursor.fetchone()
                    if not account:
                        cursor.execute("""
                            SELECT * FROM accounts WHERE status = %s AND id != %s
                            ORDER BY ABS(trophies - %s) LIMIT 1 FOR UPDATE SKIP LOCKED
                        """, (STATUS_FREE, exclude_account_id, target_trophies))
                        account = cursor.fetchone()
                else:
                    cursor.execute("SELECT * FROM accounts WHERE status = %s LIMIT 1 FOR UPDATE SKIP LOCKED", (STATUS_FREE,))
                    account = cursor.fetchone()
                
                if not account:
                    logger.critical(f"Нет свободных аккаунтов для чата {chat_id}")
                    send_to_paygame(chat_id, "Извините, все аккаунты заняты. Подождите.")
                    send_telegram_notification(f"⚠️ Закончились аккаунты! Чат: {chat_id}")
                    log_event("stock_empty", chat_id, None, "Нет свободных аккаунтов")
                    conn.rollback()
                    return
                
                if not account.get('email') or not account.get('email_password') or not account.get('imap_server'):
                    logger.error(f"Битый аккаунт ID {account['id']} пропущен.")
                    conn.rollback()
                    return
                
                cursor.execute("UPDATE accounts SET status = %s, chat_id = %s, last_status_update = NOW() WHERE id = %s",
                               (STATUS_WAIT_CODE, chat_id, account['id']))
                conn.commit()
            
            prefix = "Взамен прежнего подобран аналогичный аккаунт. " if exclude_account_id else ""
            instruction = (f"🔔 {prefix}Введите этот Email в поле входа:\n\n"
                           f"👉 {account['email']}\n\n"
                           f"Нажмите отправку кода. Бот перехватит его и пришлет сюда.")
            send_to_paygame(chat_id, instruction)
            
            send_telegram_notification(f"💰 Продажа!\nЧат: {chat_id}\nАккаунт: {mask_email(account['email'])} ({account['trophies']}🏆)")
            log_event("sale", chat_id, account['id'], f"{mask_email(account['email'])} ({account['trophies']}🏆)")
            
            with db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO sales (chat_id, account_id, account_email, trophies, price, rent_hours)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (chat_id, account['id'], account['email'], account['trophies'], account['price'], account['rent_hours']))
                    conn.commit()
            
            idle_thread_pool.submit(
                instant_code_waiter,
                account['id'], account['email'], account['email_password'], 
                account['imap_server'], chat_id, account['trophies']
            )
            
    except Exception as e:
        logger.exception(f"Ошибка выделения аккаунта: {e}")
        log_event("error", chat_id, None, str(e))

def instant_code_waiter(account_id: int, user_email: str, user_pass: str, imap_server: str, chat_id: str, trophies: int):
    logger.info(f"Запущен IDLE поток для {mask_email(user_email)}")
    code_pattern = re.compile(CODE_REGEX_PATTERN)
    timeout_minutes = 5
    start_time = datetime.now()
    code_received = False
    
    while not SHUTDOWN_EVENT.is_set():
        if datetime.now() - start_time > timedelta(minutes=timeout_minutes) and not code_received:
            send_to_paygame(chat_id, "⏰ Код не пришел. Предлагаю аналогичный аккаунт с близкими кубками.")
            
            with db_connection() as conn:
                with conn.cursor(dictionary=True) as cursor:
                    target_trophies = trophies or 0
                    min_trophies = int(target_trophies * 0.8)
                    max_trophies = int(target_trophies * 1.2)
                    cursor.execute("""
                        SELECT id FROM accounts 
                        WHERE status = %s AND trophies BETWEEN %s AND %s 
                        LIMIT 1
                    """, (STATUS_FREE, min_trophies, max_trophies))
                    replacement = cursor.fetchone()
            
            if replacement:
                send_to_paygame(chat_id, "🎮 Найден аналогичный аккаунт. Заменяю...")
                log_event("replacement", chat_id, account_id, "Замена аккаунта")
                allocate_account_and_start_idle(chat_id, exclude_account_id=account_id, target_trophies=trophies)
            else:
                send_to_paygame(chat_id, "💳 Аналогичных аккаунтов нет. Возврат средств в течение 24 часов.")
                send_account_to_rest(account_id)
            return
        
        try:
            with MailBox(imap_server, timeout=15).login(user_email, user_pass, 'INBOX') as mailbox:
                for msg in mailbox.idle(wait_timeout=5):
                    if SHUTDOWN_EVENT.is_set():
                        break
                    
                    if '\\Seen' in msg.flags:
                        continue
                    
                    raw_search_area = f"{msg.subject} {msg.text} {msg.html}"
                    decoded_search_area = html.unescape(raw_search_area)
                    match = code_pattern.search(decoded_search_area) or code_pattern.search(raw_search_area)
                    
                    if match:
                        code = match.group(0)
                        code_received = True
                        logger.info(f"✅ Код {code} перехвачен для чата {chat_id}")
                        
                        send_to_paygame(chat_id, f"🔑 Ваш код: {code}\n\n✅ Вход выполнен! Время аренды ({RENT_DURATION_HOURS} ч.) пошло.\n\n🎮 Удачной игры!\n⭐ Оцените наш сервис 5 звезд!")
                        mailbox.flag(msg.uid, 'SEEN', True)
                        
                        with db_connection() as conn:
                            with conn.cursor() as cursor:
                                end_rent_time = datetime.now() + timedelta(hours=RENT_DURATION_HOURS)
                                cursor.execute("""
                                    UPDATE accounts 
                                    SET status = %s, rent_end_time = %s 
                                    WHERE id = %s
                                """, (STATUS_IN_RENT, end_rent_time, account_id))
                                conn.commit()
                        
                        send_telegram_notification(f"🔑 Код выдан!\nЧат: {chat_id}\nКод: {code}")
                        log_event("code_delivered", chat_id, account_id, f"Код: {code}")
                        
                        remaining = RENT_DURATION_HOURS * 3600
                        while remaining > 0 and not SHUTDOWN_EVENT.is_set():
                            time.sleep(10)
                            remaining -= 10
                            with db_connection() as conn:
                                with conn.cursor(dictionary=True) as cursor:
                                    cursor.execute("SELECT status FROM accounts WHERE id = %s", (account_id,))
                                    acc = cursor.fetchone()
                                    if acc and acc['status'] != STATUS_IN_RENT:
                                        logger.info(f"Аккаунт {user_email} уже не в аренде")
                                        return
                        
                        if SHUTDOWN_EVENT.is_set():
                            return
                        
                        logout_success = trigger_logout_session(user_email, user_pass, imap_server)
                        
                        if logout_success:
                            send_to_paygame(chat_id, f"⏰ Время аренды ({RENT_DURATION_HOURS} ч.) истекло. Сессия завершена.\n⭐ Оцените нашу работу! Покупайте у нас еще! 🚀")
                            reset_account_to_free(account_id)
                            send_telegram_notification(f"⏰ Аренда завершена: {mask_email(user_email)}")
                            log_event("rental_ended", chat_id, account_id, "Успешный выход")
                        else:
                            send_to_paygame(chat_id, "⚠️ Не удалось автоматически выйти. Выйдите вручную.")
                            update_account_status(account_id, STATUS_MANUAL_RESET)
                            send_telegram_notification(f"🚨 Требуется ручной сброс: {mask_email(user_email)}")
                            log_event("manual_reset_needed", chat_id, account_id, "Автоматический выход не удался")
                        
                        return
                        
        except Exception as e:
            logger.warning(f"Ошибка IDLE для {mask_email(user_email)}: {e}")
            time.sleep(5)
            continue

def send_account_to_rest(account_id: int):
    try:
        with db_connection() as conn:
            with conn.cursor() as cursor:
                rest_until_time = datetime.now() + timedelta(days=2)
                cursor.execute("UPDATE accounts SET status = %s, rest_until = %s WHERE id = %s",
                             (STATUS_REST, rest_until_time, account_id))
                conn.commit()
                logger.info(f"Аккаунт ID {account_id} отправлен на отдых до {rest_until_time}")
                log_event("account_rest", None, account_id, f"Отдых до {rest_until_time}")
    except Exception as e:
        logger.error(f"Ошибка отправки в REST: {e}")

def reset_account_to_free(account_id: int):
    try:
        with db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE accounts SET status = %s, chat_id = NULL, rent_end_time = NULL, rest_until = NULL WHERE id = %s",
                             (STATUS_FREE, account_id))
                conn.commit()
                log_event("account_reset", None, account_id, "Сброшен в FREE")
    except Exception as e:
        logger.error(f"Ошибка сброса аккаунта: {e}")

def update_account_status(account_id: int, status: int):
    try:
        with db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE accounts SET status = %s WHERE id = %s", (status, account_id))
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка обновления статуса: {e}")

def trigger_logout_session(user, pwd, server) -> bool:
    with requests.Session() as session:
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        })
        try:
            with MailBox(server, timeout=15).login(user, pwd, 'INBOX') as mailbox:
                for msg in reversed(list(mailbox.fetch(limit=20))):
                    text_to_search = html.unescape(f"{msg.subject} {msg.text} {msg.html}")
                    logout_links = re.findall(LOGOUT_LINK_PATTERN, text_to_search, re.IGNORECASE)
                    if logout_links:
                        target_url = clean_extracted_url(logout_links[0])
                        logger.info(f"Найдена ссылка выхода: {target_url}")
                        try:
                            response = session.get(target_url, timeout=15, allow_redirects=True)
                            if response.status_code in [200, 302, 301]:
                                logger.info(f"Выход выполнен для {mask_email(user)}")
                                return True
                        except Exception as e:
                            logger.warning(f"Ошибка перехода по ссылке: {e}")
            return False
        except Exception as e:
            logger.exception(f"Ошибка выхода для {mask_email(user)}: {e}")
            return False

# =============================================================================
# WATCHDOG
# =============================================================================
def watchdog_and_timer_thread():
    logger.info("Поток Watchdog и контроля таймеров аренды запущен.")
    while not SHUTDOWN_EVENT.is_set():
        try:
            with db_connection() as conn:
                with conn.cursor(dictionary=True) as cursor:
                    cursor.execute("SELECT * FROM accounts WHERE status = %s AND rent_end_time <= NOW()", (STATUS_IN_RENT,))
                    expired_accounts = cursor.fetchall()
                    for acc in expired_accounts:
                        if acc['status'] != STATUS_IN_RENT:
                            continue
                        logger.info(f"Срок аренды аккаунта {mask_email(acc['email'])} истек.")
                        try:
                            logout_success = trigger_logout_session(acc['email'], acc['email_password'], acc['imap_server'])
                        except Exception as e:
                            logger.error(f"Ошибка выхода для {mask_email(acc['email'])}: {e}")
                            logout_success = False
                        
                        if logout_success:
                            reset_account_to_free(acc['id'])
                            send_to_paygame(acc['chat_id'], "⏰ Время аренды истекло. Доступ закрыт.\n⭐ Оцените нашу работу!")
                            send_telegram_notification(f"⏰ Аренда завершена: {mask_email(acc['email'])}")
                            log_event("rental_expired", acc['chat_id'], acc['id'], "Автоматический выход")
                        else:
                            update_account_status(acc['id'], STATUS_MANUAL_RESET)
                            send_telegram_notification(f"🚨 Требуется ручной сброс: {mask_email(acc['email'])}")
                            log_event("manual_reset_needed", acc['chat_id'], acc['id'], "Выход не удался")
        except Exception as e:
            logger.exception(f"Ошибка в watchdog: {e}")
        
        try:
            with db_connection() as conn:
                with conn.cursor(dictionary=True) as cursor:
                    cursor.execute("SELECT id, email FROM accounts WHERE status = %s AND rest_until <= NOW()", (STATUS_REST,))
                    rested_accounts = cursor.fetchall()
                    for acc in rested_accounts:
                        logger.info(f"Отдых для {mask_email(acc['email'])} завершен.")
                        reset_account_to_free(acc['id'])
                        send_telegram_notification(f"♻️ Аккаунт вышел из отдыха: {mask_email(acc['email'])}")
                        log_event("rest_ended", None, acc['id'], "Отдых завершен")
        except Exception as e:
            logger.exception(f"Ошибка возврата аккаунтов из отдыха: {e}")
        
        SHUTDOWN_EVENT.wait(POLL_INTERVAL)

# =============================================================================
# SALES LISTENER
# =============================================================================
def sales_listener_thread():
    logger.info("Поток мониторинга продаж запущен.")
    while not SHUTDOWN_EVENT.is_set():
        try:
            with MailBox(IMAP_MASTER_SERVER, timeout=15).login(MASTER_EMAIL, MASTER_PASSWORD, 'INBOX') as mailbox:
                for msg in mailbox.idle(wait_timeout=5):
                    if SHUTDOWN_EVENT.is_set():
                        break
                    
                    body = msg.text or msg.html or ""
                    sender = msg.from_ or ""
                    
                    if "noreply@paygame.ru" in sender.lower():
                        match = re.search(r"chats/(\d+)", body)
                        if match:
                            chat_id = match.group(1)
                            logger.info(f"Новая продажа! Чат: {chat_id}")
                            log_event("new_sale", chat_id, None, "Получено уведомление о продаже")
                            allocate_account_and_start_idle(chat_id)
                            mailbox.flag(msg.uid, 'SEEN', True)
                    
                    elif "сообщение" in body.lower() or "message" in body.lower():
                        chat_match = re.search(r"chats/(\d+)", body)
                        if chat_match:
                            chat_id = chat_match.group(1)
                            buyer_text = re.search(r"Покупатель:\s*(.*)", body)
                            buyer_text = buyer_text.group(1) if buyer_text else "Вопрос по аккаунту"
                            
                            with db_connection() as conn:
                                with conn.cursor(dictionary=True) as cursor:
                                    cursor.execute("SELECT status, email FROM accounts WHERE chat_id = %s", (chat_id,))
                                    acc_info = cursor.fetchone()
                            
                            status_desc = "Оплата не подтверждена. Задайте вопрос до покупки."
                            if acc_info:
                                if acc_info['status'] == STATUS_WAIT_CODE:
                                    status_desc = f"Ожидание кода для {mask_email(acc_info['email'])}"
                                elif acc_info['status'] == STATUS_IN_RENT:
                                    status_desc = f"Активна аренда {mask_email(acc_info['email'])}"
                            
                            ai_reply = ask_ai_assistant(chat_id, buyer_text, status_desc)
                            send_to_paygame(chat_id, f"🤖 {ai_reply}")
                            mailbox.flag(msg.uid, 'SEEN', True)
                            
        except Exception as e:
            logger.exception(f"Ошибка мониторинга продаж: {e}")
        if SHUTDOWN_EVENT.is_set():
            break
        SHUTDOWN_EVENT.wait(POLL_INTERVAL)

# =============================================================================
# RAILWAY SELF PINGER
# =============================================================================
def railway_self_pinger():
    if not RAILWAY_PUBLIC_URL:
        return
    while not SHUTDOWN_EVENT.is_set():
        try:
            requests.get(RAILWAY_PUBLIC_URL, timeout=10)
        except:
            pass
        for _ in range(240):
            if SHUTDOWN_EVENT.is_set():
                break
            time.sleep(1)

# =============================================================================
# MAIN
# =============================================================================
def main():
    logger.info("🚀 Запуск профессионального бота...")
    
    required_vars = {
        "MASTER_EMAIL": MASTER_EMAIL,
        "MASTER_PASSWORD": MASTER_PASSWORD,
        "IMAP_MASTER_SERVER": IMAP_MASTER_SERVER,
        "PAYGAME_SESSION": PAYGAME_SESSION,
    }
    missing = [k for k, v in required_vars.items() if not v or v in ["ВСТАВЬТЕ_СЮДА", "ВСТАВЬТЕ_СЮДА_ЗНАЧЕНИЕ_COOKIE_SESSION"]]
    if missing:
        logger.critical(f"❌ Отсутствуют или невалидны переменные: {', '.join(missing)}")
        return
    
    if not db_pool:
        logger.critical("❌ Нет подключения к БД")
        return
    
    verify_paygame_session()
    initialize_database()
    
    threading.Thread(target=run_admin_server, name="AdminPanel", daemon=True).start()
    threading.Thread(target=railway_self_pinger, name="Pinger", daemon=True).start()
    threading.Thread(target=sales_listener_thread, name="SalesListener", daemon=True).start()
    threading.Thread(target=watchdog_and_timer_thread, name="Watchdog", daemon=True).start()
    threading.Thread(target=clear_old_ai_history, name="AICleaner", daemon=True).start()
    
    send_telegram_notification("🚀 Профессиональный бот запущен! Админ панель: " + (RAILWAY_PUBLIC_URL or "http://localhost:8080"))
    logger.info("✅ Бот успешно запущен!")
    
    try:
        while not SHUTDOWN_EVENT.is_set():
            SHUTDOWN_EVENT.wait(timeout=1.0)
    except KeyboardInterrupt:
        SHUTDOWN_EVENT.set()
    
    idle_thread_pool.shutdown(wait=False)
    logger.info("Бот остановлен.")

if __name__ == "__main__":
    main()

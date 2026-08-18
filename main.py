import os
import re
import time
import signal
import logging
import threading
import html
import requests
import json
import sqlite3
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Optional, Dict, List
from http.server import BaseHTTPRequestHandler, HTTPServer
from concurrent.futures import ThreadPoolExecutor

import openai
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
RAILWAY_PUBLIC_URL = os.environ.get("RAILWAY_PUBLIC_URL")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
BRAWL_STARS_API_KEY = os.environ.get("BRAWL_STARS_API_KEY")
MASTER_EMAIL = os.environ.get("MASTER_EMAIL")
MASTER_PASSWORD = os.environ.get("MASTER_PASSWORD")
IMAP_MASTER_SERVER = os.environ.get("IMAP_MASTER_SERVER", "imap.mail.ru")
PAYGAME_SESSION = os.environ.get("PAYGAME_SESSION")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5
MAX_IDLE_THREADS = 20
DAILY_PURCHASE_LIMIT = 3
REST_DAYS = 1
POLL_INTERVAL = 15
RENT_DURATION_HOURS = 2

# =============================================================================
# SQLITE DATABASE
# =============================================================================
DB_PATH = "bot.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def initialize_database():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                email_password TEXT NOT NULL,
                imap_server TEXT NOT NULL,
                player_tag TEXT DEFAULT NULL,
                player_name TEXT DEFAULT NULL,
                status INTEGER DEFAULT 0,
                trophies INTEGER DEFAULT 0,
                highest_trophies INTEGER DEFAULT 0,
                brawlers_count INTEGER DEFAULT 0,
                price INTEGER DEFAULT 100,
                rent_hours INTEGER DEFAULT 2,
                chat_id TEXT DEFAULT NULL,
                rent_end_time TEXT DEFAULT NULL,
                rest_until TEXT DEFAULT NULL,
                total_rents INTEGER DEFAULT 0,
                total_revenue INTEGER DEFAULT 0,
                last_status_update TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                account_id INTEGER NOT NULL,
                account_email TEXT NOT NULL,
                player_tag TEXT DEFAULT NULL,
                trophies_before INTEGER DEFAULT 0,
                trophies_after INTEGER DEFAULT 0,
                price INTEGER DEFAULT 0,
                rent_hours INTEGER DEFAULT 2,
                sold_at TEXT DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'completed'
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                chat_id TEXT DEFAULT NULL,
                account_id INTEGER DEFAULT NULL,
                message TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        logger.info("✅ База данных SQLite успешно инициализирована.")

@contextmanager
def db_connection():
    conn = get_db_connection()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def log_event(event_type: str, chat_id: Optional[str] = None, account_id: Optional[int] = None, message: str = ""):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO logs (event_type, chat_id, account_id, message)
                VALUES (?, ?, ?, ?)
            """, (event_type, chat_id, account_id, message))
            conn.commit()
    except Exception:
        logger.exception("Ошибка записи лога")

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
# DEEPSEEK AI
# =============================================================================
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
# BRAWL STARS API
# =============================================================================
def get_brawl_stats(player_tag: str):
    if not BRAWL_STARS_API_KEY:
        return None
    try:
        url = f"https://api.brawlstars.com/v1/players/%23{player_tag.replace('#', '')}"
        headers = {"Authorization": f"Bearer {BRAWL_STARS_API_KEY}"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return {
                "name": data.get("name", "Unknown"),
                "trophies": data.get("trophies", 0),
                "highestTrophies": data.get("highestTrophies", 0),
                "brawlers_count": len(data.get("brawlers", [])),
            }
        return None
    except Exception as e:
        logger.error(f"Ошибка запроса к Brawl Stars API: {e}")
        return None

def update_account_stats(account_id: int, player_tag: str):
    stats = get_brawl_stats(player_tag)
    if not stats:
        return False
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE accounts 
                SET trophies = ?, brawlers_count = ?, player_name = ?, 
                    highest_trophies = ?, last_status_update = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (stats["trophies"], stats["brawlers_count"], stats["name"], 
                  stats["highestTrophies"], account_id))
            conn.commit()
        logger.info(f"✅ Аккаунт ID {account_id} обновлён: {stats['trophies']}🏆")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка обновления аккаунта: {e}")
        return False

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
# PAYGAME SEND
# =============================================================================
def send_to_paygame(chat_id: str, text: str, retries: int = MAX_RETRY_ATTEMPTS) -> bool:
    if not chat_id:
        return False
    url = f"https://paygame.ru{chat_id}/messages"
    for attempt in range(retries):
        try:
            res = PAYGAME_HTTP_SESSION.post(url, json={"message": text}, timeout=10)
            if res.status_code == 200:
                return True
            time.sleep(RETRY_DELAY_SECONDS)
        except Exception:
            time.sleep(RETRY_DELAY_SECONDS)
    return False

# =============================================================================
# AI ASSISTANT
# =============================================================================
def ask_ai_assistant(chat_id: str, buyer_message: str, current_bot_status: str) -> str:
    if not DEEPSEEK_ENABLED:
        return "Извините, я автоматический бот. Следуйте инструкциям выше."
    
    system_prompt = f"""
Ты — профессиональный ИИ-ассистент сервиса аренды аккаунтов Brawl Stars на Paygame.
ТЫ МОЖЕШЬ отвечать на вопросы о времени аренды (2 часа) и процессе.
ТЫ НЕ МОЖЕШЬ называть почту, пароль или код до оплаты.
ТЕКУЩИЙ СТАТУС ЗАКАЗА: {current_bot_status}
"""
    try:
        response = openai.ChatCompletion.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": buyer_message}
            ],
            timeout=10,
            temperature=0.1
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка DeepSeek API: {e}")
        return "Я зафиксировал ваш вопрос. Администратор ответит вам в ближайшее время."

# =============================================================================
# WEB ADMIN PANEL (УПРОЩЁННАЯ)
# =============================================================================
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
        elif self.path.startswith('/api/accounts'):
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
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM accounts")
            total = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM accounts WHERE status = ?", (STATUS_FREE,))
            free = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM accounts WHERE status = ?", (STATUS_IN_RENT,))
            rented = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM accounts WHERE status = ?", (STATUS_REST,))
            resting = cursor.fetchone()[0]
            cursor.execute("SELECT SUM(price) FROM sales")
            revenue = cursor.fetchone()[0] or 0
            cursor.execute("SELECT COUNT(*) FROM sales WHERE DATE(sold_at) = DATE('now')")
            today_sales = cursor.fetchone()[0]
            return {
                'total': total, 'free': free, 'rented': rented,
                'resting': resting, 'revenue': revenue, 'today_sales': today_sales
            }
    except Exception as e:
        return {'error': str(e)}

def get_accounts():
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, email, player_tag, player_name, trophies, brawlers_count,
                       price, rent_hours, status, chat_id, total_rents
                FROM accounts ORDER BY id DESC
            """)
            rows = cursor.fetchall()
            status_map = {0: 'Свободен', 1: 'Ожидание кода', 2: 'В аренде', 3: 'Требует сброса', 4: 'Отдых'}
            accounts = []
            for row in rows:
                accounts.append({
                    'id': row[0], 'email': row[1], 'player_tag': row[2] or '-',
                    'player_name': row[3] or '-', 'trophies': row[4] or 0,
                    'brawlers_count': row[5] or 0, 'price': row[6] or 0,
                    'rent_hours': row[7] or 2, 'status': status_map.get(row[8], 'Неизвестно'),
                    'status_code': row[8], 'chat_id': row[9] or '-',
                    'total_rents': row[10] or 0
                })
            return accounts
    except Exception as e:
        return []

def get_sales():
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, chat_id, player_tag, trophies_before, trophies_after, price, sold_at
                FROM sales ORDER BY sold_at DESC LIMIT 100
            """)
            rows = cursor.fetchall()
            sales = []
            for row in rows:
                sales.append({
                    'id': row[0], 'chat_id': row[1] or '-',
                    'player_tag': row[2] or '-', 'trophies_before': row[3] or 0,
                    'trophies_after': row[4] or 0, 'price': row[5] or 0,
                    'sold_at': row[6] or '-'
                })
            return sales
    except Exception as e:
        return []

def get_logs():
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, event_type, chat_id, account_id, message, created_at FROM logs ORDER BY id DESC LIMIT 50")
            rows = cursor.fetchall()
            logs = []
            for row in rows:
                logs.append({
                    'id': row[0], 'event_type': row[1], 'chat_id': row[2] or '-',
                    'account_id': row[3] or '-', 'message': row[4] or '-',
                    'created_at': row[5] or '-'
                })
            return logs
    except Exception as e:
        return []

def add_account(data):
    try:
        email = data.get('email', '').strip()
        password = data.get('password', '').strip()
        imap_server = data.get('imap_server', '').strip()
        player_tag = data.get('player_tag', '').strip().replace('#', '')
        trophies = int(data.get('trophies', 0))
        price = int(data.get('price', 100))
        rent_hours = int(data.get('rent_hours', 2))
        
        if not email or not password or not imap_server:
            return {'success': False, 'error': 'Все поля обязательны'}
        
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO accounts (email, email_password, imap_server, player_tag,
                                      trophies, price, rent_hours, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (email, password, imap_server, player_tag, trophies, price, rent_hours, STATUS_FREE))
            conn.commit()
        
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def delete_account(account_id):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            conn.commit()
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def get_admin_html():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Админ панель - Brawl Stars</title>
        <style>
            body { font-family: Arial; background: #0d1117; color: #c9d1d9; padding: 20px; }
            .stats { display: grid; grid-template-columns: repeat(6, 1fr); gap: 15px; }
            .stat-card { background: #161b22; padding: 20px; border-radius: 10px; text-align: center; }
            .number { font-size: 28px; font-weight: bold; color: #58a6ff; }
            .label { color: #8b949e; font-size: 14px; }
            table { width: 100%; border-collapse: collapse; margin-top: 20px; }
            th { background: #161b22; padding: 10px; text-align: left; }
            td { padding: 10px; border-bottom: 1px solid #21262d; }
            .status-badge { padding: 3px 10px; border-radius: 20px; font-size: 12px; }
            .status-free { background: #2ea043; color: #fff; }
            .status-rent { background: #58a6ff; color: #fff; }
            .status-rest { background: #8b949e; color: #fff; }
            .tabs { display: flex; gap: 10px; margin: 20px 0; }
            .tabs button { background: #21262d; border: none; color: #c9d1d9; padding: 10px 20px; border-radius: 8px; cursor: pointer; }
            .tabs button.active { background: #58a6ff; color: #0d1117; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }
            .form-add { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; background: #161b22; padding: 20px; border-radius: 10px; }
            .form-add input { background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 10px; border-radius: 6px; }
            .form-add button { background: #2ea043; border: none; color: #fff; padding: 10px; border-radius: 6px; cursor: pointer; }
            .btn-delete { background: #f85149; border: none; color: #fff; padding: 5px 10px; border-radius: 4px; cursor: pointer; }
        </style>
    </head>
    <body>
        <h1>🎮 Панель управления Brawl Stars</h1>
        
        <div class="stats" id="stats">
            <div class="stat-card"><div class="number" id="stat-total">-</div><div class="label">Всего</div></div>
            <div class="stat-card"><div class="number green" id="stat-free">-</div><div class="label">Свободные</div></div>
            <div class="stat-card"><div class="number orange" id="stat-rented">-</div><div class="label">В аренде</div></div>
            <div class="stat-card"><div class="number red" id="stat-resting">-</div><div class="label">На отдыхе</div></div>
            <div class="stat-card"><div class="number gold" id="stat-revenue">-</div><div class="label">Выручка</div></div>
            <div class="stat-card"><div class="number green" id="stat-today">-</div><div class="label">Сегодня</div></div>
        </div>
        
        <div class="tabs">
            <button class="active" onclick="showTab('accounts')">📋 Аккаунты</button>
            <button onclick="showTab('sales')">💰 Продажи</button>
            <button onclick="showTab('add')">➕ Добавить</button>
        </div>
        
        <div id="tab-accounts" class="tab-content active">
            <table><thead><tr><th>ID</th><th>Почта</th><th>Тег</th><th>🏆</th><th>Цена</th><th>Статус</th><th>Действия</th></tr></thead>
            <tbody id="accounts-table"></tbody></table>
        </div>
        
        <div id="tab-sales" class="tab-content">
            <table><thead><tr><th>Чат</th><th>Тег</th><th>🏆 до</th><th>🏆 после</th><th>Цена</th><th>Дата</th></tr></thead>
            <tbody id="sales-table"></tbody></table>
        </div>
        
        <div id="tab-add" class="tab-content">
            <div class="form-add">
                <input type="text" id="add-email" placeholder="Почта" />
                <input type="text" id="add-password" placeholder="Пароль" />
                <input type="text" id="add-imap" placeholder="IMAP сервер" />
                <input type="text" id="add-tag" placeholder="Тег Brawl Stars" />
                <input type="number" id="add-trophies" placeholder="Кубки" value="0" />
                <input type="number" id="add-price" placeholder="Цена" value="100" />
                <input type="number" id="add-rent-hours" placeholder="Часы" value="2" />
                <button onclick="addAccount()">➕ Добавить</button>
            </div>
            <div id="add-result" style="margin-top:10px;color:#3fb950;"></div>
        </div>
        
        <script>
            function showTab(name) {
                document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
                document.querySelectorAll('.tabs button').forEach(el => el.classList.remove('active'));
                document.getElementById('tab-' + name).classList.add('active');
                document.querySelector(`.tabs button[onclick="showTab('${name}')"]`).classList.add('active');
                if (name === 'accounts') loadAccounts();
                if (name === 'sales') loadSales();
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
                    if (!data.length) { tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;">Нет аккаунтов</td></tr>'; return; }
                    tbody.innerHTML = data.map(a => `
                        <tr>
                            <td>${a.id}</td>
                            <td style="font-size:11px;">${a.email}</td>
                            <td style="color:#58a6ff;">${a.player_tag}</td>
                            <td>${a.trophies}</td>
                            <td>${a.price}₽</td>
                            <td><span class="status-badge status-${a.status_code === 0 ? 'free' : a.status_code === 2 ? 'rent' : 'rest'}">${a.status}</span></td>
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
                    if (!data.length) { tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;">Нет продаж</td></tr>'; return; }
                    tbody.innerHTML = data.map(s => `
                        <tr>
                            <td style="font-size:11px;">${s.chat_id}</td>
                            <td style="color:#58a6ff;">${s.player_tag}</td>
                            <td>${s.trophies_before}</td>
                            <td>${s.trophies_after}</td>
                            <td>${s.price}₽</td>
                            <td style="font-size:12px;">${s.sold_at}</td>
                        </tr>
                    `).join('');
                } catch(e) { console.error(e); }
            }
            
            async function addAccount() {
                const data = {
                    email: document.getElementById('add-email').value.trim(),
                    password: document.getElementById('add-password').value.trim(),
                    imap_server: document.getElementById('add-imap').value.trim(),
                    player_tag: document.getElementById('add-tag').value.trim(),
                    trophies: parseInt(document.getElementById('add-trophies').value) || 0,
                    price: parseInt(document.getElementById('add-price').value) || 100,
                    rent_hours: parseInt(document.getElementById('add-rent-hours').value) || 2
                };
                try {
                    const res = await fetch('/api/account/add', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
                    const result = await res.json();
                    if (result.success) {
                        document.getElementById('add-result').textContent = '✅ Аккаунт добавлен!';
                        document.getElementById('add-result').style.color = '#3fb950';
                        loadStats(); loadAccounts();
                    }
                } catch(e) { console.error(e); }
            }
            
            async function deleteAccount(id) {
                if (!confirm('Удалить аккаунт?')) return;
                try {
                    await fetch('/api/account/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: id}) });
                    loadStats(); loadAccounts();
                } catch(e) { console.error(e); }
            }
            
            loadStats(); loadAccounts();
            setInterval(() => { loadStats(); }, 30000);
        </script>
    </body>
    </html>
    """

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
# THREAD POOL
# =============================================================================
idle_thread_pool = ThreadPoolExecutor(max_workers=MAX_IDLE_THREADS)

# =============================================================================
# BOT LOGIC
# =============================================================================
def allocate_account_and_start_idle(chat_id: str, exclude_account_id: Optional[int] = None, target_trophies: Optional[int] = None):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sales WHERE chat_id = ? AND DATE(sold_at) = DATE('now')", (chat_id,))
            if cursor.fetchone()[0] >= DAILY_PURCHASE_LIMIT:
                send_to_paygame(chat_id, f"❌ Вы уже купили {DAILY_PURCHASE_LIMIT} аккаунтов сегодня. Лимит исчерпан.")
                return
            
            if not exclude_account_id:
                cursor.execute("SELECT id FROM accounts WHERE chat_id = ? AND status IN (?, ?)", 
                             (chat_id, STATUS_WAIT_CODE, STATUS_IN_RENT))
                if cursor.fetchone():
                    logger.warning(f"Заказ для чата {chat_id} уже обрабатывается.")
                    return
            
            if exclude_account_id and target_trophies is not None:
                min_trophies = int(target_trophies * 0.8)
                max_trophies = int(target_trophies * 1.2)
                cursor.execute("""
                    SELECT * FROM accounts
                    WHERE status = ? AND id != ? AND trophies BETWEEN ? AND ?
                    LIMIT 1
                """, (STATUS_FREE, exclude_account_id, min_trophies, max_trophies))
                account = cursor.fetchone()
                if not account:
                    cursor.execute("""
                        SELECT * FROM accounts WHERE status = ? AND id != ?
                        ORDER BY ABS(trophies - ?) LIMIT 1
                    """, (STATUS_FREE, exclude_account_id, target_trophies))
                    account = cursor.fetchone()
            else:
                cursor.execute("SELECT * FROM accounts WHERE status = ? LIMIT 1", (STATUS_FREE,))
                account = cursor.fetchone()
            
            if not account:
                logger.critical(f"Нет свободных аккаунтов для чата {chat_id}")
                send_to_paygame(chat_id, "Извините, все аккаунты заняты.")
                return
            
            cursor.execute("UPDATE accounts SET status = ?, chat_id = ?, last_status_update = CURRENT_TIMESTAMP WHERE id = ?",
                         (STATUS_WAIT_CODE, chat_id, account[0]))
            conn.commit()
            
            instruction = (f"🔔 Введите этот Email в поле входа Brawl Stars:\n\n"
                           f"👉 {account[1]}\n\n"
                           f"Нажмите отправку кода. Бот перехватит его.")
            send_to_paygame(chat_id, instruction)
            
            # Запись в продажи
            cursor.execute("""
                INSERT INTO sales (chat_id, account_id, account_email, player_tag, trophies_before, price, rent_hours)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (chat_id, account[0], account[1], account[3], account[4] or 0, account[6], account[7]))
            conn.commit()
            
            idle_thread_pool.submit(
                instant_code_waiter,
                account[0], account[1], account[2], account[3], chat_id, account[4] or 0
            )
            
    except Exception as e:
        logger.exception(f"Ошибка выделения аккаунта: {e}")

def instant_code_waiter(account_id: int, user_email: str, user_pass: str, imap_server: str, chat_id: str, trophies: int):
    logger.info(f"Запущен IDLE поток для {mask_email(user_email)}")
    code_pattern = re.compile(CODE_REGEX_PATTERN)
    timeout_minutes = 5
    start_time = datetime.now()
    
    while not SHUTDOWN_EVENT.is_set():
        if datetime.now() - start_time > timedelta(minutes=timeout_minutes):
            send_to_paygame(chat_id, "⏰ Код не пришел. Предлагаю аналогичный аккаунт.")
            with db_connection() as conn:
                cursor = conn.cursor()
                min_trophies = int(trophies * 0.8) if trophies else 0
                max_trophies = int(trophies * 1.2) if trophies else 1000
                cursor.execute("""
                    SELECT id FROM accounts 
                    WHERE status = ? AND trophies BETWEEN ? AND ? 
                    LIMIT 1
                """, (STATUS_FREE, min_trophies, max_trophies))
                replacement = cursor.fetchone()
            if replacement:
                allocate_account_and_start_idle(chat_id, exclude_account_id=account_id, target_trophies=trophies)
            else:
                send_to_paygame(chat_id, "💳 Аналогичных аккаунтов нет. Возврат средств.")
                send_account_to_rest(account_id)
            return
        
        try:
            with MailBox(imap_server, timeout=15).login(user_email, user_pass, 'INBOX') as mailbox:
                for msg in mailbox.idle(wait_timeout=5):
                    if SHUTDOWN_EVENT.is_set():
                        break
                    if '\\Seen' in msg.flags:
                        continue
                    raw = f"{msg.subject} {msg.text} {msg.html}"
                    decoded = html.unescape(raw)
                    match = code_pattern.search(decoded) or code_pattern.search(raw)
                    if match:
                        code = match.group(0)
                        send_to_paygame(chat_id, f"🔑 Ваш код: {code}\n\n✅ Вход выполнен! Время аренды ({RENT_DURATION_HOURS} ч.) пошло.")
                        mailbox.flag(msg.uid, 'SEEN', True)
                        
                        with db_connection() as conn:
                            cursor = conn.cursor()
                            end_rent_time = datetime.now() + timedelta(hours=RENT_DURATION_HOURS)
                            cursor.execute("""
                                UPDATE accounts 
                                SET status = ?, rent_end_time = ?, total_rents = total_rents + 1
                                WHERE id = ?
                            """, (STATUS_IN_RENT, end_rent_time.isoformat(), account_id))
                            conn.commit()
                        
                        # Ждём окончания аренды
                        remaining = RENT_DURATION_HOURS * 3600
                        while remaining > 0 and not SHUTDOWN_EVENT.is_set():
                            time.sleep(10)
                            remaining -= 10
                        
                        if SHUTDOWN_EVENT.is_set():
                            return
                        
                        logout_success = trigger_logout_session(user_email, user_pass, imap_server)
                        if logout_success:
                            send_account_to_rest(account_id)
                            send_to_paygame(chat_id, f"⏰ Время аренды истекло. Аккаунт ушёл на отдых на {REST_DAYS} день.")
                        else:
                            update_account_status(account_id, STATUS_MANUAL_RESET)
                        return
        except Exception as e:
            logger.warning(f"Ошибка IDLE для {mask_email(user_email)}: {e}")
            time.sleep(5)

def send_account_to_rest(account_id: int):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            rest_until_time = datetime.now() + timedelta(days=REST_DAYS)
            cursor.execute("UPDATE accounts SET status = ?, rest_until = ? WHERE id = ?",
                         (STATUS_REST, rest_until_time.isoformat(), account_id))
            conn.commit()
    except Exception as e:
        logger.error(f"Ошибка отправки в REST: {e}")

def update_account_status(account_id: int, status: int):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, account_id))
            conn.commit()
    except Exception as e:
        logger.error(f"Ошибка обновления статуса: {e}")

def trigger_logout_session(user, pwd, server) -> bool:
    with requests.Session() as session:
        try:
            with MailBox(server, timeout=15).login(user, pwd, 'INBOX') as mailbox:
                for msg in reversed(list(mailbox.fetch(limit=20))):
                    text = html.unescape(f"{msg.subject} {msg.text} {msg.html}")
                    links = re.findall(LOGOUT_LINK_PATTERN, text, re.IGNORECASE)
                    if links:
                        target_url = clean_extracted_url(links[0])
                        response = session.get(target_url, timeout=15, allow_redirects=True)
                        if response.status_code in [200, 302, 301]:
                            return True
            return False
        except Exception:
            return False

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
                            allocate_account_and_start_idle(chat_id)
                            mailbox.flag(msg.uid, 'SEEN', True)
                    elif "сообщение" in body.lower():
                        chat_match = re.search(r"chats/(\d+)", body)
                        if chat_match:
                            chat_id = chat_match.group(1)
                            buyer_text = re.search(r"Покупатель:\s*(.*)", body)
                            buyer_text = buyer_text.group(1) if buyer_text else "Вопрос"
                            with db_connection() as conn:
                                cursor = conn.cursor()
                                cursor.execute("SELECT status, email, trophies FROM accounts WHERE chat_id = ?", (chat_id,))
                                acc_info = cursor.fetchone()
                            status_desc = "Оплата не подтверждена"
                            if acc_info:
                                if acc_info[0] == STATUS_WAIT_CODE:
                                    status_desc = f"Ожидание кода для {mask_email(acc_info[1])}"
                                elif acc_info[0] == STATUS_IN_RENT:
                                    status_desc = f"Активна аренда {mask_email(acc_info[1])}"
                            ai_reply = ask_ai_assistant(chat_id, buyer_text, status_desc)
                            send_to_paygame(chat_id, f"🤖 {ai_reply}")
                            mailbox.flag(msg.uid, 'SEEN', True)
        except Exception:
            logger.exception("Ошибка мониторинга продаж")
        SHUTDOWN_EVENT.wait(POLL_INTERVAL)

# =============================================================================
# WATCHDOG
# =============================================================================
def watchdog_and_timer_thread():
    logger.info("Поток Watchdog запущен.")
    while not SHUTDOWN_EVENT.is_set():
        try:
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM accounts WHERE status = ? AND rent_end_time <= datetime('now')", (STATUS_IN_RENT,))
                expired = cursor.fetchall()
                for acc in expired:
                    logout_success = trigger_logout_session(acc[1], acc[2], acc[3])
                    if logout_success:
                        send_account_to_rest(acc[0])
                    else:
                        update_account_status(acc[0], STATUS_MANUAL_RESET)
        except Exception:
            logger.exception("Ошибка в watchdog")
        
        try:
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM accounts WHERE status = ? AND rest_until <= datetime('now')", (STATUS_REST,))
                rested = cursor.fetchall()
                for acc in rested:
                    cursor.execute("UPDATE accounts SET status = ?, rest_until = NULL WHERE id = ?", (STATUS_FREE, acc[0]))
                    conn.commit()
        except Exception:
            logger.exception("Ошибка возврата аккаунтов из отдыха")
        
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
    logger.info("🚀 Запуск бота для Brawl Stars с SQLite...")
    
    required_vars = {
        "MASTER_EMAIL": MASTER_EMAIL,
        "MASTER_PASSWORD": MASTER_PASSWORD,
        "PAYGAME_SESSION": PAYGAME_SESSION,
    }
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        logger.critical(f"❌ Отсутствуют переменные: {', '.join(missing)}")
        return
    
    initialize_database()
    
    threading.Thread(target=run_admin_server, name="AdminPanel", daemon=True).start()
    threading.Thread(target=railway_self_pinger, name="Pinger", daemon=True).start()
    threading.Thread(target=sales_listener_thread, name="SalesListener", daemon=True).start()
    threading.Thread(target=watchdog_and_timer_thread, name="Watchdog", daemon=True).start()
    
    logger.info("✅ Бот успешно запущен!")
    send_telegram_notification("🚀 Бот Brawl Stars запущен!")
    
    try:
        while not SHUTDOWN_EVENT.is_set():
            SHUTDOWN_EVENT.wait(timeout=1.0)
    except KeyboardInterrupt:
        SHUTDOWN_EVENT.set()
    
    logger.info("Бот остановлен.")

if __name__ == "__main__":
    main()

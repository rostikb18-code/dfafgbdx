import os
import re
import time
import signal
import logging
import imaplib
import email
import requests
from contextlib import contextmanager
from typing import Optional, Dict

import pymysql
from pymysql.cursors import DictCursor
from dotenv import load_dotenv

# =============================================================================
# CONFIG
# =============================================================================

load_dotenv()

DB_CONFIG = {
    "host": os.environ["DB_HOST"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
    "database": os.environ["DB_NAME"],
    "port": int(os.getenv("DB_PORT", "3306")),
    "charset": "utf8mb4",
    "connect_timeout": 10,
    "read_timeout": 15,
    "write_timeout": 15,
    "autocommit": False,
}

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))
RESERVATION_TIMEOUT_MINUTES = int(os.getenv("RESERVATION_TIMEOUT_MINUTES", "15"))
DB_RETRIES = int(os.getenv("DB_RETRIES", "3"))

# Настройки для интеграции с почтой уведомлений и PayGame
MASTER_EMAIL = os.getenv("MASTER_EMAIL", "mhqizxqg@bekommenmail.com")
MASTER_PASSWORD = os.getenv("MASTER_PASSWORD", "12346789")
IMAP_MASTER_SERVER = os.getenv("IMAP_MASTER_SERVER", "bekommenmail.com")  # Без ://

PAYGAME_SESSION = os.getenv("PAYGAME_SESSION", "uHC4EvIUTBhxPMRqWs7S")

PAYGAME_COOKIES = {"session": PAYGAME_SESSION}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# =============================================================================
# STATES
# =============================================================================

ACCOUNT_FREE = 0
ACCOUNT_RESERVED = 1
ACCOUNT_DELIVERED = 2
ACCOUNT_SOLD = 3

ORDER_NEW = "NEW"
ORDER_RESERVED = "RESERVED"
ORDER_DELIVERED = "DELIVERED"
ORDER_COMPLETED = "COMPLETED"
ORDER_CANCELLED = "CANCELLED"

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("account_bot")

# =============================================================================
# SHUTDOWN
# =============================================================================

RUNNING = True

def shutdown_handler(signum, frame):
    global RUNNING
    logger.info("Получен сигнал %s. Останавливаем worker...", signum)
    RUNNING = False

signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

# =============================================================================
# VALIDATION
# =============================================================================

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def validate_email(value: str) -> bool:
    if not value:
        return False
    value = value.strip()
    if len(value) > 320:
        return False
    return bool(EMAIL_RE.fullmatch(value))

def mask_email(value: str) -> str:
    if not value or "@" not in value:
        return "***"
    name, domain = value.split("@", 1)
    if len(name) <= 2:
        masked = "***"
    else:
        masked = name[:2] + "***"
    return f"{masked}@{domain}"

# =============================================================================
# DATABASE
# =============================================================================

@contextmanager
def db_connection():
    connection = None
    try:
        connection = pymysql.connect(**DB_CONFIG)
        yield connection
    except Exception:
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        raise
    finally:
        if connection:
            try:
Redirecting...
logger.info


connection.close()
            except Exception:
                pass

def health_check() -> bool:
    try:
        with db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                result = cursor.fetchone()
                return result is not None and result[0] == 1
    except Exception:
        logger.exception("Database health-check failed")
        return False

# =============================================================================
# АВТОМАТИЗАЦИЯ ВЫДАЧИ И СЛУШАТЕЛЯ ПОЧТЫ
# =============================================================================

def reserve_and_get_account() -> Optional[Dict[str, str]]:
    """Извлекает свободный аккаунт из таблицы с использованием ваших имен колонок"""
    for attempt in range(DB_RETRIES):
        try:
            with db_connection() as connection:
                with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                    cursor.execute(
                        "SELECT id, `mhqizxqq@bekommenmail.com`, `1account1` "
                        "FROM account WHERE status = %s LIMIT 1 FOR UPDATE;",
                        (ACCOUNT_FREE,)
                    )
                    account = cursor.fetchone()
                    
                    if account:
                        cursor.execute(
                            "UPDATE account SET status = %s WHERE id = %s;",
                            (ACCOUNT_RESERVED, account['id'])
                        )
                        connection.commit()
                        return {
                            'id': account['id'],
                            'email': account['mhqizxqq@bekommenmail.com'],
                            'password': account['1account1']
                        }
                    return None
        except Exception as e:
            logger.warning("Ошибка при резервировании аккаунта (попытка %s/%s): %s", attempt + 1, DB_RETRIES, e)
            time.sleep(1)
    return None

def update_account_status(account_id: int, status: int):
    """Обновляет финальный статус аккаунта в БД"""
    try:
        with db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE account SET status = %s WHERE id = %s;", (status, account_id))
                connection.commit()
    except Exception:
        logger.exception("Не удалось обновить статус аккаунта %s", account_id)

def check_paygame_sales() -> Optional[str]:
    """Ищет новое уведомление о продаже на основной почте"""
    mail = None
    chat_id = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_MASTER_SERVER, timeout=10)
        mail.login(MASTER_EMAIL, MASTER_PASSWORD)
        mail.select("inbox")
        
        status, messages = mail.search(None, '(UNSEEN FROM "noreply@paygame.ru")')
        if status == "OK" and messages:
            for msg_id in messages[0].split():
                _, data = mail.fetch(msg_id, '(RFC822)')
                msg = email.message_from_bytes(data[0][1])
                
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                            break
                else:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                
                chat_match = re.search(r'chats/(\d+)', body)
                if chat_match:
                    chat_id = chat_match.group(1)
                    mail.store(msg_id, '+FLAGS', '\\Seen')  # Защита от повторной обработки
                    break
    except Exception as e:
        logger.error("Ошибка при проверке почты PayGame: %s", e)
    finally:
        if mail:
            try:
                mail.close()
                mail.logout()


except:
                pass
    return chat_id

def send_paygame_chat(chat_id: str, text: str) -> bool:
    """Отправляет сообщение покупателю в чат торговой площадки"""
    if "ВСТАВЬТЕ_СЮДА" in PAYGAME_COOKIES["session"]:
        logger.error("Запуск невозможен: не заполнен сессионный кук PAYGAME_SESSION в .env")
        return False
        
    url = f"https://paygame.ru{chat_id}/messages"
    payload = {"message": text}
    try:
        response = requests.post(url, json=payload, cookies=PAYGAME_COOKIES, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            return True
        logger.error("Ошибка PayGame API: Статус %s, Ответ: %s", response.status_code, response.text)
        return False
    except Exception as e:
        logger.error("Сетевая ошибка при отправке сообщения в чат %s: %s", chat_id, e)
        return False

def listen_for_code(account_email: str, account_password: str) -> Optional[str]:
    """Слушает почту выданного аккаунта для захвата 6-значного кода подтверждения"""
    imap_server = "bekommenmail.com" if "bekommenmail" in account_email else "imap.mail.ru"
    logger.info("Ожидание кода авторизации для %s", mask_email(account_email))
    
    start_time = time.time()
    # Ожидание длится максимум 5 минут (300 секунд)
    while time.time() - start_time < 300 and RUNNING:
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(imap_server, timeout=10)
            mail.login(account_email, account_password)
            mail.select("inbox")
            
            status, messages = mail.search(None, 'UNSEEN')
            if status == "OK" and messages:
                latest_id = messages[0].split()[-1]
                _, data = mail.fetch(latest_id, '(RFC822)')
                msg = email.message_from_bytes(data[0][1])
                
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                            break
                else:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                
                code_match = re.search(r'\b(\d{6})\b', body)
                if code_match:
                    code = code_match.group(1)
                    mail.store(latest_id, '+FLAGS', '\\Seen')
                    logger.info("Код найден: %s", code)
                    return code
                    
        except Exception as e:
            logger.debug("Временный сбой связи с ящиком аккаунта: %s", e)
        finally:
            if mail:
                try:
                    mail.close()
                    mail.logout()
                except:
                    pass
        
        time.sleep(7)
    
    logger.warning("Время ожидания кода истекло")
    return None

# =============================================================================
# WORKER LOOP
# =============================================================================

def worker():
    logger.info("[+] Сервер запущен. Проверка работоспособности базы данных...")
    if not health_check():
        logger.critical("[-] База данных недоступна. Завершение работы.")
        return
    
    logger.info("[+] Бот автоматизации PayGame успешно запущен в режиме 24/7.")
    
    while RUNNING:
        try:
            chat_id = check_paygame_sales()
            
            if chat_id:
                logger.info("[!] Зафиксирована новая продажа. Обработка чата %s", chat_id)
                
                account = reserve_and_get_account()
                if not account:
                    logger.critical("[-] Товар закончился! База данных пуста.")
                    send_paygame_chat(
                        chat_id,
                        "Здравствуйте! Товар временно закончился на складе. Пожалуйста, ожидайте попол
Redirecting...
logger.info


нения базы."
                    )
                    continue
                
                # Формируем сообщение: только почта, без пароля
                welcome_text = (
                    f"Приветствуем! Спасибо за покупку.\n"
                    f"Ваша почта для входа в аккаунт:\n"
                    f"• {account['email']}\n\n"
                    f"Пожалуйста, введите её в окно авторизации нужного вам сервиса. "
                    f"Как только система отправит 6-значный защитный код, наш бот моментально перешлет его прямо сюда!"
                )
                
                if send_paygame_chat(chat_id, welcome_text):
                    logger.info("[+] Почта аккаунта отправлена покупателю.")
                    
                    # Запускаем чтение кода с выданного ящика
                    code = listen_for_code(account['email'], account['password'])
                    
                    if code:
                        send_paygame_chat(chat_id, f"Ваш код для входа: {code}")
                        logger.info("[+] Код авторизации (%s) успешно переслан покупателю.", code)
                        update_account_status(account['id'], ACCOUNT_SOLD)
                    else:
                        send_paygame_chat(
                            chat_id,
                            "Время ожидания кода безопасности (5 минут) истекло. Пожалуйста, запросите код повторно."
                        )
                        logger.warning("[-] Код для аккаунта %s не дождались вовремя.", mask_email(account['email']))
                        update_account_status(account['id'], ACCOUNT_FREE)  # Возвращаем в продажу
                else:
                    logger.error("[-] Не удалось отправить стартовое сообщение. Возвращаем аккаунт в базу.")
                    update_account_status(account['id'], ACCOUNT_FREE)
            
            time.sleep(POLL_INTERVAL)
            
        except Exception as global_err:
            logger.error("Критический сбой в главном цикле: %s", global_err)
            time.sleep(POLL_INTERVAL)
    
    logger.info("Worker успешно остановлен.")

# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    try:
        worker()
    except KeyboardInterrupt:
        logger.info("Остановка пользователем")
    except Exception as e:
        logger.error("Fatal error: %s", e)
        raise

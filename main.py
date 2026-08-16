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

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

DB_CONFIG = {
    "host": "autorack.proxy.rlwy.net",
    "user": "root",
    "password": "DGoDqbSoxOT1BKjmTGnRtIPJCvxEObjF",
    "database": "railway",
    "port": 27376,
    "charset": "utf8mb4",
    "connect_timeout": 10,
    "read_timeout": 15,
    "write_timeout": 15,
    "autocommit": False,
}

POLL_INTERVAL = 15
DB_RETRIES = 3

MASTER_EMAIL = "mhqizxqg@bekommenmail.com"
MASTER_PASSWORD = "12346789"
IMAP_MASTER_SERVER = "bekommenmail.com"

PAYGAME_SESSION = "uHC4EvIUTBhxPMRqWs7S"

PAYGAME_COOKIES = {"session": PAYGAME_SESSION}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# =============================================================================
# СОСТОЯНИЯ
# =============================================================================

ACCOUNT_FREE = 0
ACCOUNT_RESERVED = 1
ACCOUNT_SOLD = 3

# =============================================================================
# ЛОГИРОВАНИЕ
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("paygame_bot")

RUNNING = True

# =============================================================================
# ОСТАНОВКА
# =============================================================================

def shutdown_handler(signum, frame):
    global RUNNING
    logger.info("Остановка...")
    RUNNING = False

signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ
# =============================================================================

def mask_email(value: str) -> str:
    if not value or "@" not in value:
        return "***"
    name, domain = value.split("@", 1)
    if len(name) <= 2:
        return "***@" + domain
    return name[:2] + "***@" + domain

# =============================================================================
# БАЗА ДАННЫХ
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
                connection.close()
            except Exception:
                pass

def health_check() -> bool:
    try:
        with db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone() is not None
    except Exception:
        logger.exception("Database health-check failed")
        return False

# =============================================================================
# РАБОТА С АККАУНТАМИ
# =============================================================================

def reserve_and_get_account() -> Optional[Dict[str, str]]:
    for attempt in range(DB_RETRIES):
        try:
            with db_connection() as connection:
                with connection.cursor(DictCursor) as cursor:
                    cursor.execute(
                        "SELECT id, `mhqizxqq@bekommenmail.com`, `1account1` "
                        "FROM account WHERE status = %s LIMIT 1 FOR UPDATE",
                        (ACCOUNT_FREE,)
                    )
                    account = cursor.fetchone()
                    if account:
                        cursor.execute(
                            "UPDATE account SET status = %s WHERE id = %s",
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
            logger.warning("Ошибка резервирования (попытка %s/%s): %s", attempt + 1, DB_RETRIES, e)
            time.sleep(1)
    return None

def update_account_status(account_id: int, status: int):
    try:
        with db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE account SET status = %s WHERE id = %s", (status, account_id))
                connection.commit()
    except Exception:
        logger.exception("Не удалось обновить статус аккаунта %s", account_id)

# =============================================================================
# РАБОТА С ПОЧТОЙ
# =============================================================================

def check_paygame_sales() -> Optional[str]:
    mail = None
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
                    mail.store(msg_id, '+FLAGS', '\\Seen')
                    logger.info("Найден новый заказ, chat_id: %s", chat_id)
                    return chat_id
        return None
    except Exception as e:
        logger.error("Ошибка проверки почты: %s", e)
        return None
    finally:
        if mail:
            try:
                mail.close()
                mail.logout()
            except:
                pass

def send_paygame_chat(chat_id: str, text: str) -> bool:
    if "ВСТАВЬТЕ_СЮДА" in PAYGAME_COOKIES["session"]:
        logger.error("Не заполнен session-кук PayGame!")
        return False

    url = f"https://paygame.ru{chat_id}/messages"
    try:
        response = requests.post(url, json={"message": text}, cookies=PAYGAME_COOKIES, headers=HEADERS, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logger.error("Ошибка отправки: %s", e)
        return False

# =============================================================================
# ОЖИДАНИЕ КОДА (ИСПРАВЛЕНО)
# =============================================================================

def listen_for_code(account_email: str, account_password: str, timeout_seconds: int = 300) -> Optional[str]:
    imap_server = "bekommenmail.com" if "bekommenmail" in account_email else "imap.mail.ru"
    logger.info("Ожидание кода для %s (макс. %s сек)", mask_email(account_email), timeout_seconds)

    start_time = time.time()
    while time.time() - start_time < timeout_seconds and RUNNING:
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
            logger.debug("Ошибка проверки почты аккаунта: %s", e)
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
# ГЛАВНЫЙ ЦИКЛ
# =============================================================================

def main():
    logger.info("========================================")
    logger.info("🚀 PayGame Bot запущен")
    logger.info("========================================")

    if not health_check():
        logger.critical("❌ База данных недоступна!")
        return

    logger.info("✅ База данных подключена")
    logger.info("🔄 Ожидание новых заказов...")

    while RUNNING:
        try:
            chat_id = check_paygame_sales()

            if chat_id:
                logger.info("🛒 Новый заказ! Chat ID: %s", chat_id)

                account = reserve_and_get_account()
                if not account:
                    logger.error("❌ Нет свободных аккаунтов!")
                    send_paygame_chat(chat_id, "Товар временно закончился.")
                    continue

                welcome_text = (
                    "✅ Спасибо за покупку!\n\n"
                    f"📧 Ваша почта: `{account['email']}`\n\n"
                    "🔐 Введите её на сайте. Как только придёт код, бот перешлёт его сюда."
                )

                if send_paygame_chat(chat_id, welcome_text):
                    logger.info("✅ Почта отправлена")

                    code = listen_for_code(account['email'], account['password'])

                    if code:
                        send_paygame_chat(chat_id, f"🔑 Ваш код: `{code}`")
                        logger.info("✅ Код отправлен")
                        update_account_status(account['id'], ACCOUNT_SOLD)
                    else:
                        send_paygame_chat(chat_id, "⏰ Время ожидания кода истекло.")
                        update_account_status(account['id'], ACCOUNT_FREE)
                        logger.warning("Код не получен, аккаунт освобождён")
                else:
                    logger.error("❌ Не удалось отправить сообщение")
                    update_account_status(account['id'], ACCOUNT_FREE)

            time.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.error("💥 Ошибка: %s", e)
            time.sleep(POLL_INTERVAL)

    logger.info("⏹ Бот остановлен")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Остановка пользователем")

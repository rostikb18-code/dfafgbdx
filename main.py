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

# =============================================================================
# CONFIG
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
RESERVATION_TIMEOUT_MINUTES = 15
DB_RETRIES = 3

MASTER_EMAIL = "mhqizxqg@bekommenmail.com"
MASTER_PASSWORD = "12346789"
IMAP_MASTER_SERVER = "bekommenmail.com"  # без ://

PAYGAME_SESSION = os.getenv("PAYGAME_SESSION", "uHC4EvIUTBhxPMRqWs7S")

PAYGAME_COOKIES = {"session": PAYGAME_SESSION}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# =============================================================================
# STATES
# =============================================================================

ACCOUNT_FREE = 0
ACCOUNT_RESERVED = 1
ACCOUNT_DELIVERED = 2
ACCOUNT_SOLD = 3

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
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
# HELPERS
# =============================================================================

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def mask_email(value: str) -> str:
    if not value or "@" not in value:
        return "***"
    name, domain = value.split("@", 1)
    if len(name) <= 2:
        return "***@" + domain
    return name[:2] + "***@" + domain

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
# ACCOUNT MANAGEMENT
# =============================================================================


def reserve_and_get_account() -> Optional[Dict[str, str]]:
    for attempt in range(DB_RETRIES):
        try:
            with db_connection() as connection:
                with connection.cursor(DictCursor) as cursor:
                    cursor.execute(
                        """
                        SELECT id, `mhqizxqq@bekommenmail.com`, `1account1`
                        FROM account
                        WHERE status = %s
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (ACCOUNT_FREE,),
                    )
                    account = cursor.fetchone()

                    if account:
                        cursor.execute(
                            "UPDATE account SET status = %s WHERE id = %s",
                            (ACCOUNT_RESERVED, account["id"]),
                        )
                        connection.commit()
                        return {
                            "id": account["id"],
                            "email": account["mhqizxqq@bekommenmail.com"],
                            "password": account["1account1"],
                        }
                    return None
        except Exception as e:
            logger.warning(
                "Ошибка резервирования (попытка %s/%s): %s",
                attempt + 1,
                DB_RETRIES,
                e,
            )
            time.sleep(1)
    return None


def update_account_status(account_id: int, status: int) -> None:
    try:
        with db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE account SET status = %s WHERE id = %s",
                    (status, account_id),
                )
                connection.commit()
    except Exception:
        logger.exception("Не удалось обновить статус аккаунта %s", account_id)

# =============================================================================
# PAYGAME INTEGRATION
# =============================================================================


def check_paygame_sales() -> Optional[str]:
    mail = None
    chat_id = None

    try:
        mail = imaplib.IMAP4_SSL(IMAP_MASTER_SERVER, timeout=10)
        mail.login(MASTER_EMAIL, MASTER_PASSWORD)
        mail.select("inbox")

        status, messages = mail.search(None, '(UNSEEN FROM "noreply@paygame.ru")')

        if status == "OK" and messages:
            for msg_id in messages[0].split():
                _, data = mail.fetch(msg_id, "(RFC822)")
                msg = email.message_from_bytes(data[0][1])

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(
                                "utf-8", errors="ignore"
                            )
                            break
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                match = re.search(r"chats/(\d+)", body)
                if match:
                    chat_id = match.group(1)
                    mail.store(msg_id, "+FLAGS", "\\Seen")
                    break

        return chat_id

    except Exception as e:
        logger.error("Ошибка проверки почты PayGame: %s", e)
        return None
    finally:
        if mail:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass


def send_paygame_chat(chat_id: str, text: str) -> bool:
    if "ВСТАВЬТЕ" in PAYGAME_COOKIES["session"]:
        logger.error("Не заполнен session-кук PayGame!")
        return False

    url = f"https://paygame.ru{chat_id}/messages"

    try:
        response = requests.post(
            url,
            json={"message": text},
            cookies=PAYGAME_COOKIES,
            headers=HEADERS,
            timeout=10,
        )
        if response.status_code == 200:
            return True
        logger.error("Ошибка PayGame: %s", response.text)
        return False
    except Exception as e:
        logger.error("Ошибка отправки в чат %s: %s", chat_id, e)
        return False

# =============================================================================
# CODE LISTENER
# =============================================================================


def listen_for_code(email_addr: str, password: str) -> Optional[str]:
    imap_server = (
        "bekommenmail.com" if "bekommenmail" in email_addr else "imap.mail.ru"
    )

    logger.info("Ожидание кода для %s (до 5 мин)", mask_email(email_addr))

    start_time = time.time()

    while time.time() - start_time < 300 and RUNNING:
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(imap_server, timeout=10)
            mail.login(email_addr, password)
            mail.select("inbox")

            status, messages = mail.search(None, "UNSEEN")
            if status == "OK" and messages:
                latest_id = messages[0].split()[-1]
                _, data = mail.fetch(latest_id, "(RFC822)")
                msg = email.message_from_bytes(data[0][1])

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(
                                "utf-8", errors="ignore"
                            )
                            break
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                code_match = re.search(r"\b(\d{6})\b", body)
                if code_match:
                    code = code_match.group(1)
                    mail.store(latest_id, "+FLAGS", "\\Seen")
                    logger.info("Код найден: %s", code)
                    return code

        except Exception as e:
            logger.debug("Ошибка проверки почты аккаунта: %s", e)
        finally:
            if mail:
                try:
                    mail.close()
                    mail.logout()
                except Exception:
                    pass

        time.sleep(7)

    logger.warning("Время ожидания кода истекло")
    return None

# =============================================================================
# WORKER
# =============================================================================


def worker():
    logger.info("[+] Сервер запущен. Проверка базы данных...")

    if not health_check():
        logger.critical("[-] База данных недоступна. Завершение работы.")
        return

    logger.info("[+] Бот PayGame успешно запущен в режиме 24/7.")

    while RUNNING:
        try:
            chat_id = check_paygame_sales()

            if chat_id:
                logger.info("[!] Новая продажа! Chat ID: %s", chat_id)

                account = reserve_and_get_account()

                if not account:
                    logger.critical("[-] Товар закончился!")
                    send_paygame_chat(
                        chat_id,
                        "Товар временно закончился. Ожидайте пополнения.",
                    )
                    continue

                welcome = (
                    f"Приветствуем! Спасибо за покупку.\n"
                    f"Ваша почта для входа:\n"
                    f"• {account['email']}\n\n"
                    f"Введите её в окно авторизации. "
                    f"Как только придёт код — бот перешлёт его сюда!"
                )

                if send_paygame_chat(chat_id, welcome):
                    logger.info("[+] Почта отправлена.")

                    code = listen_for_code(account["email"], account["password"])

                    if code:
                        send_paygame_chat(chat_id, f"Ваш код для входа: {code}")
                        logger.info("[+] Код %s отправлен", code)
                        update_account_status(account["id"], ACCOUNT_SOLD)
                    else:
                        send_paygame_chat(
                            chat_id,
                            "Время ожидания кода истекло. Запросите код повторно.",
                        )
                        logger.warning("[-] Код не получен, аккаунт освобождён")
                        update_account_status(account["id"], ACCOUNT_FREE)
                else:
                    logger.error("[-] Не удалось отправить сообщение")
                    update_account_status(account["id"], ACCOUNT_FREE)

            time.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.error("Критическая ошибка: %s", e)
            time.sleep(POLL_INTERVAL)

    logger.info("Worker остановлен.")


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

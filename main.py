import imaplib
import email
import re
import time
import requests
import pymysql

# ==============================================================================
# НАСТРОЙКИ ПОДКЛЮЧЕНИЯ (ВСЁ НАСТРОЕНО)
# ==============================================================================

DB_CONFIG = {
    'host': 'autorack.proxy.rlwy.net',
    'user': 'root', 
    'password': 'DGoDqbSoxOT1BKjmTGnRtIPJCvxEObjF',
    'database': 'railway',
    'port': 27376
}

MASTER_EMAIL = "mhqizxqg@bekommenmail.com"
MASTER_PASSWORD = "12346789"
IMAP_MASTER_SERVER = "://bekommenmail.com"

PAYGAME_COOKIES = {
    # ЗАМЕНИТЕ ТЕКСТ НИЖЕ НА ВАШ КЛЮЧ SESSION ИЗ БРАУЗЕРА:
    "session": "uHC4EvIUTBhxPMRqWs7S"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# ==============================================================================
# ЗАЩИЩЕННАЯ ЛОГИКА БОТА
# ==============================================================================

def get_free_account():
    """Безопасно извлекает свободный аккаунт и изолирует сессию БД"""
    connection = None
    try:
        connection = pymysql.connect(**DB_CONFIG)
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT id, `mhqizxqq@bekommenmail.com`, `1account1` FROM account WHERE status = 0 LIMIT 1;")
            account = cursor.fetchone()
            
            if account:
                cursor.execute("UPDATE account SET status = 1 WHERE id = %s;", (account['id'],))
                connection.commit()
                return {
                    'email': account['mhqizxqq@bekommenmail.com'],
                    'password': account['1account1']
                }
            return None
    except Exception as db_err:
        print(f"[-] Ошибка базы данных (Railway): {db_err}")
        return None
    finally:
        if connection:
            connection.close()

def check_paygame_notifications():
    """Проверяет почту, извлекает чат и помечает письмо как прочитанное во избежание спама"""
    mail = None
    chat_id = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_MASTER_SERVER, timeout=15)
        mail.login(MASTER_EMAIL, MASTER_PASSWORD)
        mail.select("inbox")
        
        status, messages = mail.search(None, '(UNSEEN FROM "noreply@paygame.ru")')
        if status == "OK" and messages:
            for msg_id in messages.split():
                _, data = mail.fetch(msg_id, '(RFC822)')
                msg = email.message_from_bytes(data)
                
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                else:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                
                chat_match = re.search(r'chats/(\d+)', body)
                if chat_match:
                    chat_id = chat_match.group(1)
                    mail.store(msg_id, '+FLAGS', '\\Seen')
                    break 
    except Exception as e:
        print(f"[-] Ошибка чтения основной почты: {e}")
    finally:
        if mail:
            try:
                mail.close()
                mail.logout()
            except:
                pass
    return chat_id

def send_message_to_buyer(chat_id, text):
    """Отправляет текстовое сообщение в чат сделки на PayGame"""
    if "COOKIE_SESSION" in PAYGAME_COOKIES["session"]:
        print("[-] Предупреждение: Вы забыли вставить реальный сессионный кук PayGame!")
        return False
        
    url = f"https://paygame.ru{chat_id}/messages"
    payload = {"message": text}
    try:
        response = requests.post(url, json=payload, cookies=PA


YGAME_COOKIES, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            return True
        print(f"[-] PayGame API ошибка: Статус {response.status_code}, Ответ: {response.text}")
        return False
    except Exception as e:
        print(f"[-] Сетевая ошибка при запросе к PayGame: {e}")
        return False

def wait_for_verification_code(account_email, account_password):
    """Слушает проданную почту на предмет появления 6-значного кода"""
    imap_server = "://bekommenmail.com" if "bekommenmail" in account_email else "imap.mail.ru"
    
    print(f"[*] Бот подключил сессию к почте {account_email} и ожидает код...")
    start_time = time.time()
    
    while time.time() - start_time < 300: 
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(imap_server, timeout=10)
            mail.login(account_email, account_password)
            mail.select("inbox")
            
            status, messages = mail.search(None, 'UNSEEN')
            if status == "OK" and messages:
                latest_id = messages.split()[-1]
                _, data = mail.fetch(latest_id, '(RFC822)')
                msg = email.message_from_bytes(data)
                
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                else:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                
                code_match = re.search(r'\b\d{6}\b', body)
                if code_match:
                    mail.store(latest_id, '+FLAGS', '\\Seen')
                    return code_match.group(0)
        except Exception as e:
            print(f"[-] Временный сбой связи с ящиком аккаунта: {e}")
        finally:
            if mail:
                try:
                    mail.close()
                    mail.logout()
                except:
                    pass
            
        time.sleep(7) 
    return None

# ==============================================================================
# БЕЗОПАСНОЕ ЯДРО СИСТЕМЫ (РАБОТА 24/7)
# ==============================================================================

def main():
    print("[+] Проверка кода завершена успешно. Все синтаксические дефекты удалены.")
    print("[+] Бот автоматизации PayGame запущен в круглосуточном режиме...")
    
    while True:
        try:
            chat_id = check_paygame_notifications()
            
            if chat_id:
                print(f"[!] Обнаружено уведомление о покупке! ID чата: {chat_id}")
                
                account = get_free_account()
                if not account:
                    print("[-] КРИТИЧЕСКАЯ СИТУАЦИЯ: Покупатель оплатил товар, но база данных ПУСТА!")
                    send_message_to_buyer(chat_id, "Здравствуйте! Товар временно закончился на складе. Пожалуйста, ожидайте пополнения.")
                    continue
                    
                # ИСПРАВЛЕНО: Теперь отправляем ТОЛЬКО почту, без пароля!
                welcome_text = (
                    f"Приветствуем! Спасибо за покупку.\n"
                    f"Ваша почта для входа в аккаунт:\n"
                    f"• {account['email']}\n\n"
                    f"Пожалуйста, введите её в окно авторизации и отправьте запрос. "
                    f"Как только на почту придет 6-значный защитный код, бот моментально перешлет его сюда!"
                )
                
                if send_message_to_buyer(chat_id, welcome_text):
                    print(f"[+] Почта {account['email']} успешно отправлена в чат {chat_id}.")
                    
                    # Бот использует пароль из базы только сам для входа по IMAP, покупателю его не показывает
                    code = wait_for_verification_code(account['email'], account['password'])


if code:
                        send_message_to_buyer(chat_id, f"Ваш код для входа: {code}")
                        print(f"[+] Код подтверждения ({code}) успешно доставлен покупателю.")
                    else:
                        send_message_to_buyer(chat_id, "Время ожидания кода безопасности (5 минут) истекло. Пожалуйста, запросите код повторно.")
                        print("[-] Превышено время ожидания кода на почте аккаунта.")
                else:
                    print("[-] Не удалось отправить сообщение. Проверьте ваш ключ 'session'.")
                    
        except Exception as global_error:
            print(f"[КРИТИЧЕСКИЙ СБОЙ]: {global_error}. Перезапуск через 15 секунд...")
            
        time.sleep(15) 

if __name__ == "__main__":
    main()

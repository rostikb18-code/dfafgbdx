import imaplib
import email
import re
import time
import requests
import pymysql

# ==============================================================================
# НАСТРОЙКИ ПОДКЛЮЧЕНИЯ (ВСЕ ВАШИ ДАННЫЕ УЖЕ ВНЕСЕНЫ)
# ==============================================================================

# 1. Ваша внешняя база данных на Railway (Включая внешний хост, порт и пароль)
DB_CONFIG = {
    'host': 'autorack.proxy.rlwy.net',
    'user': 'root', 
    'password': 'DGoDqbSoxOT1BKjmTGnRtIPJCvxEObjF',
    'database': 'railway',
    'port': 27376  # Ваш рабочий внешний порт
}

# 2. Данные вашей ОСНОВНОЙ почты (куда приходят письма о покупках от PayGame)
MASTER_EMAIL = "mhqizxqg@bekommenmail.com"
MASTER_PASSWORD = "12346789"  # Пароль от вашей почты
IMAP_MASTER_SERVER = "://bekommenmail.com"  # Сервер входящей почты для домена bekommenmail

# 3. Куки авторизации PayGame (Чтобы бот мог писать сообщения покупателям в чат)
PAYGAME_COOKIES = {
    # СЮДА ВСТАВЬТЕ ДЛИННЫЙ ТЕКСТ ИЗ СТРОКИ "session", КОТОРЫЙ ВЫ НАШЛИ В БРАУЗЕРЕ:
    "session": "uHC4EvIUTBhxPMRqWs7S"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ==============================================================================
# АВТОМАТИЗИРОВАННАЯ ЛОГИКА БОТА
# ==============================================================================

def get_free_account():
    """Берет один свободный аккаунт из базы Railway и меняет статус на 1 (продан)"""
    connection = pymysql.connect(**DB_CONFIG)
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            # Делаем выборку строго по вашим точным названиям колонок в Railway
            cursor.execute("SELECT id, `mhqizxqq@bekommenmail.com`, `1account1` FROM account WHERE status = 0 LIMIT 1;")
            account = cursor.fetchone()
            
            if account:
                # Помечаем аккаунт как проданный, чтобы не выдать его дважды
                cursor.execute("UPDATE account SET status = 1 WHERE id = %s;", (account['id'],))
                connection.commit()
                
                return {
                    'email': account['mhqizxqq@bekommenmail.com'],
                    'password': account['1account1']
                }
            return None
    finally:
        connection.close()

def check_paygame_notifications():
    """Ищет на основной почте новые уведомления о продажах от PayGame"""
    try:
        mail = imaplib.IMAP4_SSL(IMAP_MASTER_SERVER)
        mail.login(MASTER_EMAIL, MASTER_PASSWORD)
        mail.select("inbox")
        
        # Ищем новые непрочитанные письма от робота PayGame
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
                
                # Извлекаем ID чата покупателя из текста письма
                chat_match = re.search(r'chats/(\d+)', body)
                if chat_match:
                    return chat_match.group(1)
        mail.logout()
    except Exception as e:
        print(f"[-] Ошибка проверки основной почты: {e}")
    return None

def send_message_to_buyer(chat_id, text):
    """Отправляет текстовое сообщение в чат сделки на PayGame от вашего лица"""
    url = f"https://paygame.ru{chat_id}/messages"
    payload = {"message": text}
    try:
        # ИСПРАВЛЕНО: Теперь здесь правильно


е слово payload, а не pay
        response = requests.post(url, json=payload, cookies=PAYGAME_COOKIES, headers=HEADERS)
        return response.status_code == 200
    except Exception as e:
        print(f"[-] Ошибка отправки сообщения на PayGame: {e}")
        return False

def wait_for_verification_code(account_email, account_password):
    """Мониторит проданную почту и ждет 6-значный код авторизации аккаунта"""
    # Определяем imap-сервер для проданного аккаунта автоматически
    imap_server = "://bekommenmail.com" if "bekommenmail" in account_email else "imap.mail.ru"
    
    print(f"[*] Бот успешно подключился к проданной почте {account_email}. Ожидаем код...")
    start_time = time.time()
    
    while time.time() - start_time < 300: # Ждем код ровно 5 минут
        try:
            mail = imaplib.IMAP4_SSL(imap_server)
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
                
                # Ищем строго 6 цифр подряд (код авторизации аккаунта)
                code_match = re.search(r'\b\d{6}\b', body)
                if code_match:
                    mail.logout()
                    return code_match.group(0)
            mail.logout()
        except Exception as e:
            print(f"[-] Временная ошибка чтения проданной почты: {e}")
            
        time.sleep(8) # Проверяем почту каждые 8 секунд
    return None

# ==============================================================================
# ГЛАВНЫЙ ЗАПУСК СИСТЕМЫ
# ==============================================================================

def main():
    print("[+] Бот автоматизации PayGame успешно запущен.")
    print("[+] Мониторинг базы Railway и вашей почты активен...")
    while True:
        # 1. Проверяем новые продажи на главной почте
        chat_id = check_paygame_notifications()
        
        if chat_id:
            print(f"[!] Обнаружена новая оплата заказа! Переходим в чат: {chat_id}")
            
            # 2. Извлекаем свободный товар из вашей базы Railway
            account = get_free_account()
            if not account:
                print("[-] Ошибка: На сервере Railway закончились доступные аккаунты!")
                send_message_to_buyer(chat_id, "Здравствуйте! Товар временно закончился, продавец скоро добавит новые аккаунты.")
                continue
                
            # Шаг 1: Автоматически отправляем данные аккаунта в чат PayGame
            welcome_text = (
                f"Приветствуем! Спасибо за покупку.\n"
                f"Вот ваши данные для входа в аккаунт:\n"
                f"• Логин/Почта: {account['email']}\n"
                f"• Пароль: {account['password']}\n\n"
                f"Пожалуйста, начните вход. Как только сервис запросит у вас 6-значный код, "
                f"нажмите отправить код, и наш бот моментально перешлит его сюда в чат!"
            )
            
            if send_message_to_buyer(chat_id, welcome_text):
                print(f"[+] Данные аккаунта {account['email']} успешно отправлены покупателю.")
                
                # Шаг 2: Включаем режим ожидания 6-значного кода на этой почте
                code = wait_for_verification_code(account['email'], account['password'])
                
                if code:
                    # Шаг 3:


Код пришел, отправляем его покупателю в чат
                    send_message_to_buyer(chat_id, f"Ваш защитный код для входа: {code}")
                    print(f"[+] Код подтверждения ({code}) отправлен в чат {chat_id}!")
                else:
                    send_message_to_buyer(chat_id, "Время ожидания кода безопасности (5 минут) истекло. Попробуйте запросить код заново.")
                    print("[-] Код не пришел на почту в течение 5 минут.")
            else:
                print("[-] Не удалось отправить сообщение на PayGame. Проверьте правильность Cookie сессии.")
                
        time.sleep(12) # Пауза в 12 секунд между проверками, чтобы избежать блокировок

if __name__ == "__main__":
    main()

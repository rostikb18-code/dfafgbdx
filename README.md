# 🤖 Paygame Rental Bot

Автоматическая выдача аккаунтов на аренду через Paygame.

## 🚀 Быстрый старт

### Переменные окружения (.env или Railway Variables)

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | MySQL URL (Railway) |
| `RAILWAY_PUBLIC_URL` | Твой публичный URL приложения |
| `MASTER_EMAIL` | Почта для уведомлений Paygame |
| `MASTER_PASSWORD` | Пароль от почты |
| `IMAP_MASTER_SERVER` | IMAP сервер (например, `bekommenmail.com`) |
| `PAYGAME_SESSION` | Session cookie из браузера |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram бота (опционально) |
| `TELEGRAM_ADMIN_CHAT_ID` | ID чата администратора (опционально) |
| `OPENAI_API_KEY` | OpenAI API ключ (опционально) |
| `PORT` | 8080 |

### Запуск

```bash
pip install -r requirements.txt
python main.py

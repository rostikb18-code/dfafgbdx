```python
from __future__ import annotations

from typing import Optional


class InfrastructureException(Exception):
    """
    Базовое исключение инфраструктурного слоя приложения.

    Используется для ошибок, связанных с:
    - конфигурацией;
    - базой данных;
    - внешними API;
    - электронной почтой;
    - Telegram;
    - другими инфраструктурными зависимостями.

    Не содержит автоматически никаких чувствительных данных.
    """

    DEFAULT_CODE = "INFRASTRUCTURE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
    ) -> None:
        if not isinstance(message, str):
            raise TypeError("Exception message must be a string.")

        cleaned_message = message.strip()

        if not cleaned_message:
            cleaned_message = "An infrastructure error occurred."

        self.message: str = cleaned_message
        self.code: str = (
            code.strip().upper()
            if code and code.strip()
            else self.DEFAULT_CODE
        )

        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"code={self.code!r}"
            ")"
        )


class ConfigurationError(InfrastructureException):
    """
    Ошибка конфигурации приложения.

    Обычно означает, что приложение не может безопасно продолжить
    запуск из-за отсутствующих, некорректных или противоречивых
    настроек.
    """

    DEFAULT_CODE = "CONFIGURATION_ERROR"


class DatabaseError(InfrastructureException):
    """
    Ошибка инфраструктуры базы данных.

    Используется для:
    - подключения;
    - выполнения инфраструктурных операций;
    - проблем пула соединений;
    - миграций;
    - транзакций на инфраструктурном уровне.

    Не следует помещать в message:
    - DATABASE_URL;
    - пароль;
    - connection URI;
    - SQL с чувствительными параметрами.
    """

    DEFAULT_CODE = "DATABASE_ERROR"


class PaygameAPIError(InfrastructureException):
    """
    Ошибка взаимодействия с Paygame API.

    Не следует помещать в message:
    - session cookie;
    - Authorization header;
    - API token;
    - полный URL с credentials.
    """

    DEFAULT_CODE = "PAYGAME_API_ERROR"


class EmailError(InfrastructureException):
    """
    Ошибка инфраструктуры электронной почты.

    Используется для:
    - IMAP;
    - SMTP;
    - авторизации;
    - получения сообщений;
    - проблем соединения.

    Пароли и содержимое писем не должны включаться в message.
    """

    DEFAULT_CODE = "EMAIL_ERROR"


class TelegramError(InfrastructureException):
    """
    Ошибка взаимодействия с Telegram.

    Не следует помещать в message:
    - bot token;
    - Authorization header;
    - webhook secret;
    - полный URL API с токеном.
    """

    DEFAULT_CODE = "TELEGRAM_ERROR"
```

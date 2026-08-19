from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Generic, List, Optional, Type, TypeVar
from urllib.parse import quote, urlparse

from dotenv import load_dotenv

from core.exceptions import InfrastructureException


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

# Загружаем .env только если переменная отсутствует в окружении.
# Переменные Railway / Docker / CI имеют приоритет над локальным .env.
load_dotenv(override=False)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").strip().upper()

_VALID_LOG_LEVELS = {
    "CRITICAL",
    "ERROR",
    "WARNING",
    "INFO",
    "DEBUG",
}

if LOG_LEVEL not in _VALID_LOG_LEVELS:
    LOG_LEVEL = "INFO"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger: logging.Logger = logging.getLogger("rental_bot.config")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

T = TypeVar("T", int, float, str)


# ---------------------------------------------------------------------------
# Secret container
# ---------------------------------------------------------------------------

class SecretStr:
    """
    Безопасный контейнер для секретов.

    Секрет никогда не отображается через str() / repr().
    Для фактического использования необходимо явно вызвать
    get_secret_value().
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("Secret value must be a string.")

        self._value = value

    def get_secret_value(self) -> str:
        """
        Возвращает реальное значение секрета.

        Пустые секреты считаются ошибкой только в момент использования.
        Это позволяет, например, использовать локальную MySQL без пароля.
        """
        if not self._value.strip():
            raise InfrastructureException(
                "Attempted to access an empty or blank secret value."
            )

        return self._value

    def is_set(self) -> bool:
        """Проверяет, задан ли непустой секрет."""
        return bool(self._value.strip())

    def __str__(self) -> str:
        return "**********"

    def __repr__(self) -> str:
        return "SecretStr(**********)"

    def __bool__(self) -> bool:
        return self.is_set()


# ---------------------------------------------------------------------------
# Generic environment parser
# ---------------------------------------------------------------------------

def _parse_env_value(
    key: str,
    default: Optional[T],
    cast_type: Type[T],
    min_val: Optional[T] = None,
    max_val: Optional[T] = None,
) -> T:
    """
    Читает, преобразует и валидирует переменную окружения.

    Никогда не включает фактическое значение переменной в исключение.
    Это особенно важно для секретов.
    """

    raw_value: Optional[str] = os.getenv(key)

    if raw_value is None or raw_value.strip() == "":
        if default is not None:
            return default

        raise InfrastructureException(
            f"Configuration Critical Error: "
            f"Required variable '{key}' is missing or empty."
        )

    cleaned_value = raw_value.strip()

    try:
        casted = cast_type(cleaned_value)
    except (ValueError, TypeError) as exc:
        raise InfrastructureException(
            f"Configuration Type Error: "
            f"Attribute '{key}' must be of type {cast_type.__name__}."
        ) from exc

    if min_val is not None and casted < min_val:
        raise InfrastructureException(
            f"Configuration Range Error: "
            f"Attribute '{key}' cannot be less than {min_val}."
        )

    if max_val is not None and casted > max_val:
        raise InfrastructureException(
            f"Configuration Range Error: "
            f"Attribute '{key}' cannot be greater than {max_val}."
        )

    return casted


def _parse_optional_secret(key: str) -> Optional[SecretStr]:
    """
    Возвращает SecretStr для заданной переменной либо None.

    Пустая переменная считается отсутствующей.
    """

    value = os.getenv(key)

    if value is None or not value.strip():
        return None

    return SecretStr(value.strip())


# ---------------------------------------------------------------------------
# Configuration sections
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SecurityConfig:
    """Конфигурация криптографической защиты и подписи сессий."""

    secret_key: SecretStr


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: SecretStr
    admin_chat_id: int


@dataclass(frozen=True, slots=True)
class PaygameConfig:
    session_cookie: SecretStr
    api_base_url: str
    poll_interval: int


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    host: str
    port: int
    user: str
    password: SecretStr
    database: str

    @property
    def connection_uri(self) -> str:
        """
        Формирует SQLAlchemy DSN для mysql-connector-python.

        Username/password URL-кодируются, чтобы специальные символы
        не ломали URI.
        """

        encoded_user = quote(self.user, safe="")
        encoded_password = quote(
            self.password.get_secret_value(),
            safe="",
        )

        return (
            f"mysql+mysqlconnector://"
            f"{encoded_user}:{encoded_password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    def __repr__(self) -> str:
        """
        Дополнительная защита от случайного вывода DSN.
        """

        return (
            "DatabaseConfig("
            f"host={self.host!r}, "
            f"port={self.port!r}, "
            f"user={self.user!r}, "
            "password=SecretStr(**********), "
            f"database={self.database!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class EmailConfig:
    master_email: str
    master_password: SecretStr
    imap_server: str
    check_interval: int
    code_timeout_minutes: int


@dataclass(frozen=True, slots=True)
class ExternalApiConfig:
    brawl_stars_token: Optional[SecretStr]
    openai_token: Optional[SecretStr]


@dataclass(frozen=True, slots=True)
class BusinessRulesConfig:
    min_rent_days: int
    daily_purchase_limit: int
    rest_days: int


@dataclass(frozen=True, slots=True)
class ResilienceConfig:
    max_retry_attempts: int
    retry_base_delay: float

    @property
    def proxy_pool(self) -> List[str]:
        """
        Возвращает актуальный пул прокси из PROXY_POOL_JSON.

        Важно:
        - НЕ очищает os.environ;
        - НЕ перезагружает .env;
        - не выводит полный malformed URL в лог, поскольку URL может
          содержать username/password;
        - возвращает новый список.
        """

        raw_pool = os.getenv("PROXY_POOL_JSON", "[]").strip()

        if not raw_pool:
            return []

        try:
            parsed: Any = json.loads(raw_pool)
        except json.JSONDecodeError:
            logger.error(
                "Failed to parse PROXY_POOL_JSON: invalid JSON."
            )
            return []

        if not isinstance(parsed, list):
            logger.error(
                "Failed to parse PROXY_POOL_JSON: expected a JSON array."
            )
            return []

        validated_proxies: List[str] = []

        for item in parsed:
            if not isinstance(item, str):
                logger.warning(
                    "Skipping proxy entry because it is not a string."
                )
                continue

            proxy = item.strip()

            if not proxy:
                continue

            try:
                parsed_url = urlparse(proxy)

                scheme = parsed_url.scheme.lower()

                valid_scheme = scheme in {
                    "http",
                    "https",
                    "socks5",
                    "socks5h",
                }

                valid_host = bool(parsed_url.hostname)

                if not valid_scheme or not valid_host:
                    logger.warning(
                        "Skipping structurally invalid proxy entry."
                    )
                    continue

                # urlparse может принять некоторые странные конструкции.
                # Проверяем наличие netloc дополнительно.
                if not parsed_url.netloc:
                    logger.warning(
                        "Skipping proxy entry without network location."
                    )
                    continue

                validated_proxies.append(proxy.rstrip("/"))

            except ValueError:
                logger.warning(
                    "Skipping malformed proxy entry."
                )

        return validated_proxies


@dataclass(frozen=True, slots=True)
class AppConfig:
    environment: str
    log_level: str
    security: SecurityConfig
    telegram: TelegramConfig
    paygame: PaygameConfig
    database: DatabaseConfig
    email: EmailConfig
    external_api: ExternalApiConfig
    business_rules: BusinessRulesConfig
    resilience: ResilienceConfig


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _build_database_config() -> DatabaseConfig:
    """
    Создаёт конфигурацию MySQL.

    Приоритет:
        1. DATABASE_URL
        2. DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME
    """

    db_url = os.getenv("DATABASE_URL")

    if db_url and db_url.strip():
        try:
            parsed = urlparse(db_url.strip())

            scheme = parsed.scheme.lower()

            if not scheme.startswith("mysql"):
                logger.warning(
                    "DATABASE_URL scheme is not MySQL. "
                    "Falling back to individual DB_* variables."
                )
            elif not parsed.hostname:
                logger.warning(
                    "DATABASE_URL does not contain a valid hostname. "
                    "Falling back to individual DB_* variables."
                )
            else:
                return DatabaseConfig(
                    host=parsed.hostname,
                    port=parsed.port or 3306,
                    user=parsed.username or "root",
                    password=SecretStr(parsed.password or ""),
                    database=(
                        parsed.path.lstrip("/")
                        if parsed.path
                        else "railway"
                    ),
                )

        except ValueError:
            # Не логируем сам URL.
            logger.warning(
                "DATABASE_URL could not be structurally parsed. "
                "Falling back to individual DB_* variables."
            )

    return DatabaseConfig(
        host=_parse_env_value(
            "DB_HOST",
            "127.0.0.1",
            str,
        ),
        port=_parse_env_value(
            "DB_PORT",
            3306,
            int,
            min_val=1,
            max_val=65535,
        ),
        user=_parse_env_value(
            "DB_USER",
            "root",
            str,
        ),
        password=SecretStr(
            _parse_env_value(
                "DB_PASSWORD",
                "",
                str,
            )
        ),
        database=_parse_env_value(
            "DB_NAME",
            "railway",
            str,
        ),
    )


# ---------------------------------------------------------------------------
# Main configuration builder
# ---------------------------------------------------------------------------

def _build_configuration() -> AppConfig:
    """
    Создаёт полную конфигурацию приложения.

    Здесь выполняется вся cross-field validation.
    """

    try:
        # ---------------------------------------------------------------
        # Optional API credentials
        # ---------------------------------------------------------------

        brawl_stars_token = _parse_optional_secret(
            "BRAWL_STARS_API_KEY"
        )

        openai_token = _parse_optional_secret(
            "OPENAI_API_KEY"
        )

        # ---------------------------------------------------------------
        # Email timing
        # ---------------------------------------------------------------

        check_interval = _parse_env_value(
            "EMAIL_CHECK_INTERVAL",
            5,
            int,
            min_val=1,
            max_val=60,
        )

        timeout_minutes = _parse_env_value(
            "CODE_TIMEOUT_MINUTES",
            5,
            int,
            min_val=1,
            max_val=30,
        )

        # Проверка имеет смысл только если timeout действительно
        # позволяет хотя бы один polling cycle.
        timeout_seconds = timeout_minutes * 60

        if check_interval > timeout_seconds:
            raise InfrastructureException(
                "EMAIL_CHECK_INTERVAL cannot exceed "
                "CODE_TIMEOUT_MINUTES converted to seconds."
            )

        # ---------------------------------------------------------------
        # Core configuration
        # ---------------------------------------------------------------

        environment = _parse_env_value(
            "ENVIRONMENT",
            "production",
            str,
        ).lower()

        if environment not in {
            "development",
            "dev",
            "testing",
            "test",
            "staging",
            "production",
            "prod",
        }:
            raise InfrastructureException(
                "Configuration Value Error: "
                "ENVIRONMENT contains an unsupported value."
            )

        # ---------------------------------------------------------------
        # Build immutable configuration tree
        # ---------------------------------------------------------------

        return AppConfig(
            environment=environment,
            log_level=LOG_LEVEL,

            security=SecurityConfig(
                secret_key=SecretStr(
                    _parse_env_value(
                        "SECRET_KEY",
                        None,
                        str,
                    )
                )
            ),

            telegram=TelegramConfig(
                bot_token=SecretStr(
                    _parse_env_value(
                        "TELEGRAM_BOT_TOKEN",
                        None,
                        str,
                    )
                ),
                admin_chat_id=_parse_env_value(
                    "TELEGRAM_ADMIN_CHAT_ID",
                    None,
                    int,
                ),
            ),

            paygame=PaygameConfig(
                session_cookie=SecretStr(
                    _parse_env_value(
                        "PAYGAME_SESSION",
                        None,
                        str,
                    )
                ),
                api_base_url=_parse_env_value(
                    "PAYGAME_API_BASE",
                    "https://paygame.ru",
                    str,
                ).rstrip("/"),
                poll_interval=_parse_env_value(
                    "POLL_INTERVAL",
                    10,
                    int,
                    min_val=2,
                    max_val=60,
                ),
            ),

            database=_build_database_config(),

            email=EmailConfig(
                master_email=_parse_env_value(
                    "MASTER_EMAIL",
                    None,
                    str,
                ),
                master_password=SecretStr(
                    _parse_env_value(
                        "MASTER_PASSWORD",
                        None,
                        str,
                    )
                ),
                imap_server=_parse_env_value(
                    "IMAP_MASTER_SERVER",
                    "imap.mail.ru",
                    str,
                ),
                check_interval=check_interval,
                code_timeout_minutes=timeout_minutes,
            ),

            external_api=ExternalApiConfig(
                brawl_stars_token=brawl_stars_token,
                openai_token=openai_token,
            ),

            business_rules=BusinessRulesConfig(
                min_rent_days=_parse_env_value(
                    "MIN_RENT_DAYS",
                    1,
                    int,
                    min_val=1,
                    max_val=365,
                ),
                daily_purchase_limit=_parse_env_value(
                    "DAILY_PURCHASE_LIMIT",
                    3,
                    int,
                    min_val=1,
                    max_val=100,
                ),
                rest_days=_parse_env_value(
                    "REST_DAYS",
                    1,
                    int,
                    min_val=0,
                    max_val=30,
                ),
            ),

            resilience=ResilienceConfig(
                max_retry_attempts=_parse_env_value(
                    "MAX_RETRY_ATTEMPTS",
                    3,
                    int,
                    min_val=0,
                    max_val=10,
                ),
                retry_base_delay=_parse_env_value(
                    "RETRY_BASE_DELAY",
                    1.0,
                    float,
                    min_val=0.01,
                    max_val=30.0,
                ),
            ),
        )

    except InfrastructureException:
        # Уже безопасное, контролируемое исключение.
        raise

    except Exception as exc:
        # Не выводим exc: потенциально он может содержать данные
        # внешнего источника или чувствительные параметры.
        logger.exception(
            "Fatal anomaly detected during configuration initialization."
        )

        raise InfrastructureException(
            "System settings mapping crashed due to "
            "an unhandled internal exception."
        ) from exc


# ---------------------------------------------------------------------------
# Public configuration instance
# ---------------------------------------------------------------------------

config: AppConfig = _build_configuration()

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _default_database_url() -> str:
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit

    # Default to local PostgreSQL for production-like behavior and better performance.
    host = os.getenv("PGHOST", "127.0.0.1")
    port = os.getenv("PGPORT", "5432")
    name = os.getenv("PGDATABASE", "taskplatform")
    user = os.getenv("PGUSER", "taskplatform")
    password = os.getenv("PGPASSWORD", "taskplatform")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    _database_url = _default_database_url()
    SQLALCHEMY_DATABASE_URI = _database_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_size": _env_int("DB_POOL_SIZE", 5),
        "max_overflow": _env_int("DB_MAX_OVERFLOW", 5),
        "pool_timeout": _env_int("DB_POOL_TIMEOUT", 30),
        "pool_recycle": _env_int("DB_POOL_RECYCLE", 1800),
        "pool_pre_ping": True,
    }

    # Debug auth responses (returns reason in 401 JSON). Enable only for troubleshooting.
    API_AUTH_DEBUG = os.getenv("API_AUTH_DEBUG", "0") == "1"

    UPLOAD_DIR = os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads"))
    MAX_ATTACHMENTS_PER_MESSAGE = 10

    REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    RQ_QUEUE_NAME = os.getenv("RQ_QUEUE_NAME", "taskplatform")
    TASK_SEND_BATCH_SIZE = _env_int("TASK_SEND_BATCH_SIZE", 200)

    # Руководитель (без UI редактирования)
    MANAGER_LOGIN = os.getenv("MANAGER_LOGIN", "manager")
    MANAGER_PASSWORD = os.getenv("MANAGER_PASSWORD", "manager123")
    MANAGER_FIRST = os.getenv("MANAGER_FIRST", "Manager")
    MANAGER_LAST = os.getenv("MANAGER_LAST", "User")
    MANAGER_MIDDLE = os.getenv("MANAGER_MIDDLE", "")

    # Админ (можно оставить дефолт для старта)
    ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
    ADMIN_FIRST = os.getenv("ADMIN_FIRST", "Admin")
    ADMIN_LAST = os.getenv("ADMIN_LAST", "User")
    ADMIN_MIDDLE = os.getenv("ADMIN_MIDDLE", "")

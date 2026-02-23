import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'app.db'}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Debug auth responses (returns reason in 401 JSON). Enable only for troubleshooting.
    API_AUTH_DEBUG = os.getenv("API_AUTH_DEBUG", "0") == "1"

    UPLOAD_DIR = os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads"))
    MAX_ATTACHMENTS_PER_MESSAGE = 10

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

# TaskPlatform

Платформа задач и чатов между руководителем и работниками.  
Есть веб‑панель (admin/manager/worker), мобильные приложения Android и iOS, общий API и чат в реальном времени.

---

## Основная функциональность

### Роли
- **Администратор**
  - управление структурой: районы, махалли, улицы
  - создание/управление пользователями
- **Руководитель**
  - отправка задач (текст + файлы) всем или выбранным районам
  - просмотр статистики (кто прочитал/не прочитал)
  - чат с каждым работником
- **Работник**
  - просмотр входящих задач
  - чат с руководителем

### Чат (web + Android + iOS)
- сообщения в реальном времени (WebSocket)
- поддержка файлов и изображений
- просмотр вложений
- отметка прочитанного
- счётчик непрочитанных

### Задачи
- руководитель создаёт задачу
- отправка задач всем или по районам
- аналитика прочтения по работникам

---

## Компоненты проекта

### Backend (Flask)
- авторизация
- API для Android/iOS
- HTML‑веб интерфейс
- хранение файлов
- WebSocket для чата

### Веб‑панель
Маршруты:
- `/admin` — управление структурой и пользователями
- `/manager` — задачи и чаты
- `/worker` — входящие и чат

### Android (Kotlin + Compose)
Функции:
- вход по логину/паролю
- вкладки: Входящие / Чат
- загрузка файлов и картинок
- просмотр изображений
- download/open файлов
- realtime через WebSocket + fallback polling

### iOS (SwiftUI)
Функции:
- вход по логину/паролю (роль определяется автоматически)
- вкладки для ролей:
  - **Worker:** Входящие + Чат
  - **Manager:** Чаты + Задачи
- отправка задач с файлами/фото
- чат с вложениями
- WebSocket обновления

---

## Запуск локально

### Backend (PostgreSQL)
```bash
cd task-platform03
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

1) Установить PostgreSQL и создать БД/пользователя:
```bash
# macOS (Homebrew)
brew install postgresql@16
brew services start postgresql@16
createuser -s taskplatform || true
psql postgres -c "ALTER USER taskplatform WITH PASSWORD 'taskplatform';"
createdb taskplatform -O taskplatform || true
```

2) Один раз мигрировать старую SQLite базу (если есть `app.db`):
```bash
SRC_SQLITE_PATH=app.db DST_DATABASE_URL=postgresql+psycopg://taskplatform:taskplatform@127.0.0.1:5432/taskplatform python scripts/migrate_sqlite_to_postgres.py --drop-existing
```

3) Запуск:
```bash
FLASK_DEBUG=0 python run.py
# или production-like:
# gunicorn -w 2 -k gevent --bind 127.0.0.1:5001 --timeout 600 "app:create_app()"
```

### Android
Открыть проект в Android Studio и запустить.  
BASE_URL настраивается в:
```
AndroidStudioProjects/TaskPlatform/app/src/main/java/uz/taskplatform/worker/AppConfig.kt
```

### iOS
Открыть в Xcode:
```
/Users/sultanbekkenesbaev/ios/TaskPlatform/TaskPlatform.xcodeproj
```
BASE_URL в:
```
TaskPlatform/AppConfig.swift
```

---

## Деплой на VPS (Ubuntu 22.04, без GitHub)

Для "чистого" сервера (1 CPU / 2 GB RAM) есть сценарий деплоя прямо с ноутбука:
- `deploy/deploy_from_laptop.sh` — загружает проект по SSH и на сервере полностью настраивает:
  - пакеты, nginx, systemd, PostgreSQL,
  - swap,
  - `.env`,
  - запуск приложения.

### Быстрый деплой с ноутбука
```bash
cd task-platform03
SERVER=root@95.46.96.156 \
DOMAIN=biychat.uz \
EMAIL=admin@biychat.uz \
ENABLE_SSL=1 \
COPY_SQLITE=1 \
COPY_UPLOADS=1 \
bash deploy/deploy_from_laptop.sh
```

Где:
- `COPY_SQLITE=1` — копирует `app.db` и запускает миграцию SQLite -> PostgreSQL.
- `COPY_UPLOADS=1` — копирует папку `uploads/`.
- `ENABLE_SSL=1` — пробует получить SSL через certbot.

### Что запустится на сервере
- `deploy/setup_server_nogit.sh` (автонастройка без ручных шагов).
- Gunicorn на слабом VPS по умолчанию:
  - `GUNICORN_WORKERS=1`
  - `GUNICORN_WORKER_CONNECTIONS=500`
- PostgreSQL с облегченными настройками памяти.

После деплоя:
- сайт: `https://<DOMAIN>`
- Android BASE_URL: `https://<DOMAIN>/`
- iOS baseURL: `https://<DOMAIN>`

---

## Файлы / папки

- `app/` — backend + templates + static
- `deploy/` — конфиги и скрипты для сервера
- `uploads/` — загруженные файлы
- `requirements.txt` — зависимости Python

---

## Примечания

- Проект по умолчанию использует PostgreSQL (через `DATABASE_URL`).
- Для iOS раздача тестовых сборок — через TestFlight.
- Для Android APK можно распространять напрямую.

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

### Backend
```bash
cd task-platform02
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python run.py
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

## Деплой на VPS (Ubuntu 22.04)

Готовые скрипты:
- `deploy/setup_server.sh` — деплой из GitHub
- `deploy/setup_server_nogit.sh` — деплой без Git

После деплоя:
- сайт: `https://mydomen.uz`
- Android BASE_URL: `https://mydomen.uz/`
- iOS baseURL: `https://mydomen.uz`

---

## Файлы / папки

- `app/` — backend + templates + static
- `deploy/` — конфиги и скрипты для сервера
- `uploads/` — загруженные файлы
- `requirements.txt` — зависимости Python

---

## Примечания

- В продакшене рекомендуется PostgreSQL вместо SQLite.
- Для iOS раздача тестовых сборок — через TestFlight.
- Для Android APK можно распространять напрямую.

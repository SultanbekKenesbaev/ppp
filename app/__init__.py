# app/__init__.py
import re

from flask import Flask
from flask_login import LoginManager
from flask_sock import Sock
from markupsafe import Markup, escape
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

from .config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
sock = Sock()


def _ensure_runtime_indexes() -> None:
    statements = [
        "CREATE INDEX IF NOT EXISTS ix_mahallas_district_id ON mahallas (district_id)",
        "CREATE INDEX IF NOT EXISTS ix_streets_mahalla_id ON streets (mahalla_id)",
        "CREATE INDEX IF NOT EXISTS ix_users_role_active ON users (role, is_active)",
        "CREATE INDEX IF NOT EXISTS ix_users_last_name ON users (last_name)",
        "CREATE INDEX IF NOT EXISTS ix_conversations_manager_id ON conversations (manager_id)",
        "CREATE INDEX IF NOT EXISTS ix_conversations_worker_id ON conversations (worker_id)",
        "CREATE INDEX IF NOT EXISTS ix_conversations_last_message_at ON conversations (last_message_at)",
        "CREATE INDEX IF NOT EXISTS ix_messages_sender_id ON messages (sender_id)",
        "CREATE INDEX IF NOT EXISTS ix_messages_conversation_id ON messages (conversation_id)",
        "CREATE INDEX IF NOT EXISTS ix_messages_conversation_sent_at ON messages (conversation_id, sent_at)",
        "CREATE INDEX IF NOT EXISTS ix_task_batch_recipients_batch_id ON task_batch_recipients (batch_id)",
        "CREATE INDEX IF NOT EXISTS ix_task_batch_recipients_worker_id ON task_batch_recipients (worker_id)",
        "CREATE INDEX IF NOT EXISTS ix_worker_assignments_worker_id ON worker_assignments (worker_id)",
        "CREATE INDEX IF NOT EXISTS ix_worker_assignments_street_id ON worker_assignments (street_id)",
        "CREATE INDEX IF NOT EXISTS ix_task_dispatch_jobs_manager_id ON task_dispatch_jobs (manager_id)",
        "CREATE INDEX IF NOT EXISTS ix_task_dispatch_jobs_status ON task_dispatch_jobs (status)",
        "CREATE INDEX IF NOT EXISTS ix_task_dispatch_jobs_created_at ON task_dispatch_jobs (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_task_dispatch_failures_job_id ON task_dispatch_failures (job_id)",
    ]

    for sql in statements:
        db.session.execute(text(sql))
    db.session.commit()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    url_re = re.compile(r"(https?://[^\s<>\"]+)")

    @app.template_filter("linkify")
    def linkify(value):
        if not value:
            return ""
        text_value = str(escape(value))

        def repl(match):
            raw = match.group(0)
            url = raw
            trail = ""
            while url and url[-1] in ".,!?:;)":
                trail = url[-1] + trail
                url = url[:-1]
            return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>{trail}'

        linked = url_re.sub(repl, text_value).replace("\n", "<br>")
        return Markup(linked)

    @app.template_filter("uztime")
    def uztime(value, fmt="%Y-%m-%d %H:%M"):
        from .utils import as_uz_time

        dt = as_uz_time(value)
        if not dt:
            return ""
        return dt.strftime(fmt)

    db.init_app(app)
    login_manager.init_app(app)
    sock.init_app(app)

    from .models import User  # импорт моделей только тут

    @login_manager.user_loader
    def load_user(user_id: str):
        return User.query.get(int(user_id))

    # ✅ импорт blueprints ТОЛЬКО внутри create_app()
    from .auth import bp as auth_bp
    from .admin import bp as admin_bp
    from .manager import bp as manager_bp
    from .worker import bp as worker_bp

    # если у тебя есть api.py
    try:
        from .api import bp as api_bp

        app.register_blueprint(api_bp, url_prefix="/api")
    except Exception:
        pass

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(manager_bp, url_prefix="/manager")
    app.register_blueprint(worker_bp, url_prefix="/worker")

    # register websocket routes
    try:
        from . import realtime  # noqa: F401
    except Exception:
        pass

    with app.app_context():
        db.create_all()
        _ensure_runtime_indexes()

    return app

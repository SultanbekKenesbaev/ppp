# app/__init__.py
import re

from flask import Flask
from flask_sock import Sock
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from markupsafe import Markup, escape
from .config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
sock = Sock()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    url_re = re.compile(r"(https?://[^\s<>\"]+)")

    @app.template_filter("linkify")
    def linkify(value):
        if not value:
            return ""
        text = str(escape(value))

        def repl(match):
            raw = match.group(0)
            url = raw
            trail = ""
            while url and url[-1] in ".,!?:;)":
                trail = url[-1] + trail
                url = url[:-1]
            return (
                f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>{trail}'
            )

        linked = url_re.sub(repl, text).replace("\n", "<br>")
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

    return app

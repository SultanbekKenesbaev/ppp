from flask import current_app
from . import db
from .models import User
from .utils import hash_password

def run_seed():
    cfg = current_app.config

    # Admin
    admin = User.query.filter_by(login=cfg["ADMIN_LOGIN"]).first()
    if not admin:
        admin = User(
            role="admin",
            login=cfg["ADMIN_LOGIN"],
            password_hash=hash_password(cfg["ADMIN_PASSWORD"]),
            first_name=cfg["ADMIN_FIRST"],
            last_name=cfg["ADMIN_LAST"],
            middle_name=cfg["ADMIN_MIDDLE"],
        )
        db.session.add(admin)

    # Manager
    manager = User.query.filter_by(login=cfg["MANAGER_LOGIN"]).first()
    if not manager:
        manager = User(
            role="manager",
            login=cfg["MANAGER_LOGIN"],
            password_hash=hash_password(cfg["MANAGER_PASSWORD"]),
            first_name=cfg["MANAGER_FIRST"],
            last_name=cfg["MANAGER_LAST"],
            middle_name=cfg["MANAGER_MIDDLE"],
        )
        db.session.add(manager)

    db.session.commit()

if __name__ == "__main__":
    # запуск: python -m app.seed
    from app import create_app
    app = create_app()
    with app.app_context():
        run_seed()
        print("Seed done: admin + manager created/updated.")

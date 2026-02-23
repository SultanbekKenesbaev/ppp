from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, current_user
from .models import User
from .utils import verify_password, save_avatar
from pathlib import Path
from flask import current_app, send_file, abort
from . import db
from flask_login import login_required, current_user
from .models import Attachment, Message, Conversation

bp = Blueprint("auth", __name__)

@bp.get("/login")
def login():
    if current_user.is_authenticated:
        return redirect_after_login(current_user.role)
    return render_template("login.html")

@bp.post("/login")
def login_post():
    login = request.form.get("login", "").strip()
    password = request.form.get("password", "")

    user = User.query.filter_by(login=login, is_active=True).first()
    if not user or not verify_password(user.password_hash, password):
        flash("Неверный логин или пароль", "error")
        return redirect(url_for("auth.login"))

    login_user(user)
    return redirect_after_login(user.role)

@bp.get("/")
def index():
    if current_user.is_authenticated:
        # если уже вошёл — отправляем в нужный раздел
        if current_user.role == "admin":
            return redirect(url_for("admin.structure"))
        if current_user.role == "manager":
            return redirect(url_for("manager.tasks"))
        return redirect(url_for("worker.inbox"))
    return redirect(url_for("auth.login"))


@bp.get("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))

def redirect_after_login(role: str):
    if role == "admin":
        return redirect(url_for("admin.structure"))
    if role == "manager":
        return redirect(url_for("manager.tasks"))
    return redirect(url_for("worker.inbox"))


@bp.get("/profile")
@login_required
def profile():
    return render_template("profile.html")


@bp.post("/profile")
@login_required
def profile_update():
    user = current_user
    user.first_name = request.form.get("first_name", user.first_name).strip()
    user.last_name = request.form.get("last_name", user.last_name).strip()
    user.middle_name = request.form.get("middle_name", user.middle_name).strip()

    file = request.files.get("avatar")
    if file and file.filename:
        save_avatar(file, user)

    db.session.add(user)
    db.session.commit()
    flash("Профиль обновлён", "ok")
    return redirect(url_for("auth.profile"))

@bp.get("/files/<int:attachment_id>")
@login_required
def file_get(attachment_id: int):
    a = Attachment.query.get_or_404(attachment_id)
    msg = Message.query.get_or_404(a.message_id)
    conv = Conversation.query.get_or_404(msg.conversation_id)

    # доступ только участникам чата
    if current_user.role == "admin":
        pass
    elif current_user.id not in (conv.manager_id, conv.worker_id):
        abort(403)

    full_path = Path(current_app.config["UPLOAD_DIR"]) / a.stored_path
    if not full_path.exists():
        abort(404)

    # as_attachment=False → картинки/видео откроются в браузере
    return send_file(full_path, as_attachment=False, download_name=a.original_name)


@bp.get("/avatar/<int:user_id>")
@login_required
def avatar_get(user_id: int):
    user = User.query.get_or_404(user_id)
    if not user.photo_path:
        abort(404)
    full_path = Path(current_app.config["UPLOAD_DIR"]) / user.photo_path
    if not full_path.exists():
        abort(404)
    return send_file(full_path, as_attachment=False)

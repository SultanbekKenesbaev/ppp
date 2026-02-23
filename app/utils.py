import os
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from flask import current_app, abort
from . import db
from .models import Conversation, ConversationMember, User, Attachment

UZ_TZ = ZoneInfo("Asia/Tashkent")


def as_uz_time(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(UZ_TZ)


def iso_uz_time(dt: datetime | None) -> str | None:
    local_dt = as_uz_time(dt)
    return local_dt.isoformat(timespec="seconds") if local_dt else None

def hash_password(p: str) -> str:
    return generate_password_hash(p, method="pbkdf2:sha256", salt_length=16)

def verify_password(hash_: str, p: str) -> bool:
    return check_password_hash(hash_, p)

def ensure_upload_dir():
    Path(current_app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)

def detect_kind(mime: str) -> str:
    if not mime:
        return "file"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    return "file"

def save_files(files, message_id: int):
    """
    files: list[FileStorage]
    """
    ensure_upload_dir()
    max_n = int(current_app.config["MAX_ATTACHMENTS_PER_MESSAGE"])
    if len(files) > max_n:
        abort(400, f"Too many attachments. Max = {max_n}")

    saved = []
    base = Path(current_app.config["UPLOAD_DIR"])
    date_dir = datetime.utcnow().strftime("%Y/%m/%d")
    target_dir = base / date_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        if not f or not getattr(f, "filename", ""):
            continue
        original = f.filename
        filename = secure_filename(original) or "file"
        unique = f"{datetime.utcnow().timestamp():.6f}".replace(".", "")
        stored_name = f"{unique}_{filename}"
        stored_path = str(Path(date_dir) / stored_name)

        full_path = target_dir / stored_name
        f.save(full_path)

        mime, _ = mimetypes.guess_type(str(full_path))
        mime = mime or (f.mimetype if hasattr(f, "mimetype") else "") or ""
        size = os.path.getsize(full_path)

        att = Attachment(
            message_id=message_id,
            original_name=original,
            stored_path=stored_path,
            mime_type=mime,
            size=size,
            kind=detect_kind(mime),
        )
        db.session.add(att)
        saved.append(att)

    return saved

def get_or_create_conversation(manager_id: int, worker_id: int) -> Conversation:
    conv = Conversation.query.filter_by(manager_id=manager_id, worker_id=worker_id).first()
    if conv:
        return conv
    conv = Conversation(manager_id=manager_id, worker_id=worker_id)
    db.session.add(conv)
    db.session.flush()

    # members
    db.session.add(ConversationMember(conversation_id=conv.id, user_id=manager_id))
    db.session.add(ConversationMember(conversation_id=conv.id, user_id=worker_id))
    return conv

def require_role(user: User, *roles):
    if user.role not in roles:
        abort(403)

def update_last_read(conversation_id: int, user_id: int):
    m = ConversationMember.query.filter_by(conversation_id=conversation_id, user_id=user_id).first()
    if not m:
        return
    m.last_read_at = datetime.utcnow()
    db.session.add(m)


def save_avatar(file, user):
    """
    Save avatar image for user. Expects FileStorage.
    """
    if not file or not getattr(file, "filename", ""):
        return ""

    ensure_upload_dir()
    mime = (file.mimetype or "").lower()
    if mime and not mime.startswith("image/"):
        abort(400, "Avatar must be an image")

    base = Path(current_app.config["UPLOAD_DIR"])
    target_dir = base / "avatars"
    target_dir.mkdir(parents=True, exist_ok=True)

    original = file.filename
    filename = secure_filename(original) or "avatar"
    unique = f"{datetime.utcnow().timestamp():.6f}".replace(".", "")
    stored_name = f"{user.id}_{unique}_{filename}"
    stored_path = str(Path("avatars") / stored_name)

    full_path = target_dir / stored_name
    file.save(full_path)

    # remove previous avatar
    if getattr(user, "photo_path", ""):
        old_path = base / user.photo_path
        if old_path.exists():
            try:
                old_path.unlink()
            except Exception:
                pass

    user.photo_path = stored_path
    return stored_path

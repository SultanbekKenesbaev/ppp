from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import jwt
from flask import Blueprint, current_app, jsonify, request, send_file, abort
from werkzeug.security import check_password_hash
from werkzeug.datastructures import FileStorage

from . import db
from .models import (
    User, Conversation, Message, Attachment, ConversationMember,
    WorkerAssignment, Street, Mahalla, District,
    TaskBatch, TaskBatchRecipient, DeviceToken
)
from .utils import (
    get_or_create_conversation,
    save_files,
    update_last_read,
    save_avatar,
    iso_uz_time,
)
from .realtime import notify_conversation
from .push import send_fcm

bp = Blueprint("api", __name__)


# =========================
# JWT helpers
# =========================
ACCESS_TTL_MIN = 15
REFRESH_TTL_DAYS = 30


def _jwt_secret():
    return current_app.config["SECRET_KEY"]


def _make_token(user_id: int, role: str, token_type: str, exp_dt: datetime):
    if exp_dt.tzinfo is None:
        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": token_type,
        "exp": int(exp_dt.timestamp()),
        "iat": int(now.timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def _decode_token(token: str):
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def _bearer_token():
    h = request.headers.get("Authorization", "")
    if h:
        parts = h.split()
        if len(parts) == 1:
            return parts[0].strip()
        if len(parts) >= 2 and parts[0].lower() == "bearer":
            return parts[1].strip()

    # fallback headers (useful for debugging clients)
    h2 = request.headers.get("X-Access-Token", "").strip()
    if h2:
        return h2

    # last-resort: query/form param (avoid for production)
    q = (request.args.get("access_token") or request.form.get("access_token") or "").strip()
    if q:
        return q

    return ""


def _auth_error(reason: str):
    # Keep details only in debug (or when explicitly enabled).
    if current_app.debug or current_app.config.get("API_AUTH_DEBUG"):
        return jsonify({"error": "unauthorized", "reason": reason}), 401
    return jsonify({"error": "unauthorized"}), 401


def _auth_log(reason: str, extra: str = ""):
    if not (current_app.debug or current_app.config.get("API_AUTH_DEBUG")):
        return
    msg = f"api_auth_failed: {reason}"
    if extra:
        msg += f" ({extra})"
    current_app.logger.warning(msg)


def _avatar_url(u: User):
    return f"/api/avatar/{u.id}" if getattr(u, "photo_path", "") else ""


def _iso_uz(dt: datetime | None):
    return iso_uz_time(dt)


def _push_to_user(user_id: int, title: str, body: str, data: dict | None = None):
    tokens = [t.token for t in DeviceToken.query.filter_by(user_id=user_id, platform="android").all()]
    if not tokens:
        return

    resp = send_fcm(tokens, title, body, data or {})
    if not resp or "results" not in resp:
        return

    bad = []
    for token, r in zip(tokens, resp.get("results", [])):
        err = r.get("error")
        if err in ("NotRegistered", "InvalidRegistration", "MismatchSenderId", "InvalidPackageName"):
            bad.append(token)
    if bad:
        DeviceToken.query.filter(DeviceToken.token.in_(bad)).delete(synchronize_session=False)
        db.session.commit()


def _parse_int_ids(values):
    out = []
    seen = set()
    for raw in values or []:
        try:
            n = int(raw)
        except Exception:
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _worker_ids_for_target(mode: str, target_ids):
    if mode == "all":
        return [w.id for w in User.query.filter_by(role="worker", is_active=True).all()]

    workers_q = (
        User.query
        .join(WorkerAssignment, WorkerAssignment.worker_id == User.id)
        .join(Street, WorkerAssignment.street_id == Street.id)
        .join(Mahalla, Street.mahalla_id == Mahalla.id)
        .filter(User.role == "worker", User.is_active == True)
    )

    if mode == "districts":
        workers_q = workers_q.filter(Mahalla.district_id.in_(target_ids))
    elif mode == "mahallas":
        workers_q = workers_q.filter(Street.mahalla_id.in_(target_ids))
    elif mode == "streets":
        workers_q = workers_q.filter(WorkerAssignment.street_id.in_(target_ids))
    else:
        return []

    return sorted({w.id for w in workers_q.all()})


def _manager_target_options():
    district_rows = District.query.order_by(District.name.asc()).all()
    mahalla_rows = Mahalla.query.order_by(Mahalla.name.asc()).all()
    street_rows = Street.query.order_by(Street.name.asc()).all()

    district_by_id = {d.id: d for d in district_rows}
    mahalla_by_id = {m.id: m for m in mahalla_rows}
    street_by_id = {s.id: s for s in street_rows}

    assignments = {a.worker_id: a for a in WorkerAssignment.query.all()}
    active_workers = User.query.filter_by(role="worker", is_active=True).all()

    district_counts = {}
    mahalla_counts = {}
    street_counts = {}
    for worker in active_workers:
        a = assignments.get(worker.id)
        if not a:
            continue
        street = street_by_id.get(a.street_id)
        if not street:
            continue
        mahalla = mahalla_by_id.get(street.mahalla_id)
        if not mahalla:
            continue

        street_counts[street.id] = street_counts.get(street.id, 0) + 1
        mahalla_counts[mahalla.id] = mahalla_counts.get(mahalla.id, 0) + 1
        if mahalla.district_id in district_by_id:
            district_counts[mahalla.district_id] = district_counts.get(mahalla.district_id, 0) + 1

    districts = [{
        "id": d.id,
        "name": d.name,
        "count": district_counts.get(d.id, 0),
    } for d in district_rows]

    mahallas = []
    for m in mahalla_rows:
        district = district_by_id.get(m.district_id)
        mahallas.append({
            "id": m.id,
            "name": m.name,
            "district_id": m.district_id,
            "district_name": district.name if district else "",
            "count": mahalla_counts.get(m.id, 0),
        })

    streets = []
    for s in street_rows:
        mahalla = mahalla_by_id.get(s.mahalla_id)
        district = district_by_id.get(mahalla.district_id) if mahalla else None
        streets.append({
            "id": s.id,
            "name": s.name,
            "mahalla_id": s.mahalla_id,
            "mahalla_name": mahalla.name if mahalla else "",
            "district_id": district.id if district else None,
            "district_name": district.name if district else "",
            "count": street_counts.get(s.id, 0),
        })

    return districts, mahallas, streets


def api_auth_required(*roles: str):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = _bearer_token()
            if not token:
                _auth_log("token_missing")
                return _auth_error("token_missing")

            data = _decode_token(token)
            if not data:
                _auth_log("token_invalid")
                return _auth_error("token_invalid")

            if data.get("type") != "access":
                _auth_log("token_wrong_type", f"type={data.get('type')}")
                return _auth_error("token_wrong_type")

            try:
                user_id = int(data["sub"])
            except Exception:
                _auth_log("token_bad_sub")
                return _auth_error("token_bad_sub")
            user = User.query.filter_by(id=user_id).first()
            # IMPORTANT: если пользователя удалили/деактивировали — сразу 401
            if not user or not user.is_active:
                if not user:
                    _auth_log("user_not_found", f"user_id={user_id}")
                    return _auth_error("user_not_found")
                if not user.is_active:
                    _auth_log("user_inactive", f"user_id={user_id}")
                    return _auth_error("user_inactive")

            if roles and user.role not in roles:
                _auth_log("role_mismatch", f"user_id={user_id}, role={user.role}")
                return _auth_error("role_mismatch")

            request.api_user = user
            return fn(*args, **kwargs)

        return wrapper
    return deco


# =========================
# Auth
# =========================
@bp.post("/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    login_ = (data.get("login") or "").strip()
    password = data.get("password") or ""
    role_req = (data.get("role") or "").strip()

    user = User.query.filter_by(login=login_).first()
    if not user or not user.is_active or user.role not in ("worker", "manager"):
        return jsonify({"error": "invalid_credentials"}), 401

    if role_req and role_req != user.role:
        return jsonify({"error": "invalid_credentials"}), 401

    if not check_password_hash(user.password_hash, password):
        return jsonify({"error": "invalid_credentials"}), 401

    now = datetime.now(timezone.utc)
    access = _make_token(user.id, user.role, "access", now + timedelta(minutes=ACCESS_TTL_MIN))
    refresh = _make_token(user.id, user.role, "refresh", now + timedelta(days=REFRESH_TTL_DAYS))

    return jsonify({
        "access_token": access,
        "refresh_token": refresh,
        "access_expires_in_sec": ACCESS_TTL_MIN * 60,
        "refresh_expires_in_sec": REFRESH_TTL_DAYS * 24 * 3600,
        "user": {
            "id": user.id,
            "full_name": user.full_name,
            "login": user.login,
            "role": user.role,
            "avatar_url": _avatar_url(user),
        }
    })


@bp.post("/auth/refresh")
def refresh():
    data = request.get_json(silent=True) or {}
    token = (data.get("refresh_token") or "").strip()
    payload = _decode_token(token) if token else None
    if not payload or payload.get("type") != "refresh":
        return jsonify({"error": "unauthorized"}), 401

    user_id = int(payload["sub"])
    user = User.query.filter_by(id=user_id).first()
    if not user or not user.is_active or user.role not in ("worker", "manager"):
        return jsonify({"error": "unauthorized"}), 401

    now = datetime.now(timezone.utc)
    access = _make_token(user.id, user.role, "access", now + timedelta(minutes=ACCESS_TTL_MIN))
    # refresh можно оставить тот же (или выдавать новый). Оставим новый — безопаснее.
    refresh2 = _make_token(user.id, user.role, "refresh", now + timedelta(days=REFRESH_TTL_DAYS))

    return jsonify({
        "access_token": access,
        "refresh_token": refresh2,
        "access_expires_in_sec": ACCESS_TTL_MIN * 60,
        "refresh_expires_in_sec": REFRESH_TTL_DAYS * 24 * 3600,
    })


# =========================
# Worker data
# =========================
@bp.get("/me")
@api_auth_required("worker", "manager")
def me():
    u = request.api_user
    return jsonify({
        "id": u.id,
        "login": u.login,
        "full_name": u.full_name,
        "role": u.role,
        "avatar_url": _avatar_url(u),
    })


@bp.post("/device_token")
@api_auth_required("worker", "manager")
def register_device_token():
    u = request.api_user
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    platform = (data.get("platform") or "android").strip()
    if not token:
        return jsonify({"error": "token_required"}), 400

    existing = DeviceToken.query.filter_by(token=token).first()
    if existing:
        existing.user_id = u.id
        existing.platform = platform
    else:
        db.session.add(DeviceToken(user_id=u.id, token=token, platform=platform))
    db.session.commit()
    return jsonify({"ok": True})


@bp.get("/profile")
@api_auth_required("worker", "manager")
def profile_get():
    u = request.api_user
    return jsonify({
        "id": u.id,
        "login": u.login,
        "role": u.role,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "middle_name": u.middle_name,
        "full_name": u.full_name,
        "avatar_url": _avatar_url(u),
    })


@bp.post("/profile")
@api_auth_required("worker", "manager")
def profile_update():
    u = request.api_user

    u.first_name = (request.form.get("first_name") or u.first_name).strip()
    u.last_name = (request.form.get("last_name") or u.last_name).strip()
    u.middle_name = (request.form.get("middle_name") or u.middle_name).strip()

    file = request.files.get("avatar")
    if file and file.filename:
        save_avatar(file, u)

    db.session.add(u)
    db.session.commit()
    return jsonify({
        "ok": True,
        "user": {
            "id": u.id,
            "login": u.login,
            "role": u.role,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "middle_name": u.middle_name,
            "full_name": u.full_name,
            "avatar_url": _avatar_url(u),
        }
    })


@bp.get("/chats")
@api_auth_required("worker")
def chats():
    u = request.api_user

    convs = Conversation.query.filter_by(worker_id=u.id).order_by(Conversation.last_message_at.desc()).all()
    out = []
    for c in convs:
        cm = ConversationMember.query.filter_by(conversation_id=c.id, user_id=u.id).first()
        last_read = cm.last_read_at if cm else datetime(1970, 1, 1)

        unread = Message.query.filter(
            Message.conversation_id == c.id,
            Message.sender_id == c.manager_id,
            Message.sent_at > last_read
        ).count()

        last_msg = Message.query.filter_by(conversation_id=c.id).order_by(Message.sent_at.desc()).first()

        out.append({
            "id": c.id,
            "manager_id": c.manager_id,
            "manager_name": c.manager.full_name,
            "manager_avatar_url": _avatar_url(c.manager),
            "last_message_at": _iso_uz(c.last_message_at),
            "unread": unread,
            "last_message_text": (last_msg.body or last_msg.title or "") if last_msg else "",
            "last_message_type": last_msg.type if last_msg else None,
            "last_message_time": _iso_uz(last_msg.sent_at) if last_msg else None,
        })

    return jsonify(out)


# =========================
# Manager data
# =========================
@bp.get("/manager/chats")
@api_auth_required("manager")
def manager_chats():
    u = request.api_user
    district_id = request.args.get("district_id", type=int)
    mahalla_id = request.args.get("mahalla_id", type=int)

    qs = Conversation.query.filter_by(manager_id=u.id).order_by(Conversation.last_message_at.desc())
    convs = qs.all()

    # mapping for location info
    assigns = {a.worker_id: a for a in WorkerAssignment.query.all()}
    streets = {s.id: s for s in Street.query.all()}
    mahallas = {m.id: m for m in Mahalla.query.all()}
    districts = {d.id: d for d in District.query.all()}

    items = []
    for c in convs:
        a = assigns.get(c.worker_id)
        street = streets.get(a.street_id) if a else None
        mah = mahallas.get(street.mahalla_id) if street else None
        dist = districts.get(mah.district_id) if mah else None

        if district_id and (not dist or dist.id != district_id):
            continue
        if mahalla_id and (not mah or mah.id != mahalla_id):
            continue

        cm = ConversationMember.query.filter_by(conversation_id=c.id, user_id=u.id).first()
        last_read = cm.last_read_at if cm else datetime(1970, 1, 1)

        unread = Message.query.filter(
            Message.conversation_id == c.id,
            Message.sender_id == c.worker_id,
            Message.sent_at > last_read
        ).count()

        last_msg = Message.query.filter_by(conversation_id=c.id).order_by(Message.sent_at.desc()).first()

        items.append({
            "id": c.id,
            "worker_id": c.worker_id,
            "worker_name": c.worker.full_name,
            "worker_avatar_url": _avatar_url(c.worker),
            "district": dist.name if dist else "",
            "mahalla": mah.name if mah else "",
            "street": street.name if street else "",
            "unread": unread,
            "last_message_at": _iso_uz(c.last_message_at),
            "last_message_text": (last_msg.body or last_msg.title or "") if last_msg else "",
            "last_message_type": last_msg.type if last_msg else None,
            "last_message_time": _iso_uz(last_msg.sent_at) if last_msg else None,
        })

    district_opts, mahalla_opts, street_opts = _manager_target_options()

    return jsonify({
        "items": items,
        "districts": district_opts,
        "mahallas": mahalla_opts,
        "streets": street_opts,
    })


@bp.get("/manager/tasks")
@api_auth_required("manager")
def manager_tasks():
    u = request.api_user
    batches = TaskBatch.query.filter_by(manager_id=u.id).order_by(TaskBatch.created_at.desc()).all()

    out = []
    for b in batches:
        recs = TaskBatchRecipient.query.filter_by(batch_id=b.id).all()
        total = len(recs)
        read = 0
        for r in recs:
            cm = ConversationMember.query.filter_by(conversation_id=r.conversation_id, user_id=r.worker_id).first()
            if cm and cm.last_read_at >= r.message.sent_at:
                read += 1
        percent = int((read / total) * 100) if total else 0
        out.append({
            "id": b.id,
            "title": b.title,
            "body": b.body,
            "created_at": _iso_uz(b.created_at),
            "total": total,
            "read": read,
            "percent": percent,
        })

    return jsonify({"items": out})


@bp.get("/manager/tasks/<int:batch_id>")
@api_auth_required("manager")
def manager_task_details(batch_id: int):
    u = request.api_user
    batch = TaskBatch.query.filter_by(id=batch_id, manager_id=u.id).first_or_404()
    recs = TaskBatchRecipient.query.filter_by(batch_id=batch.id).all()

    # build structure map for analytics
    assign = {a.worker_id: a for a in WorkerAssignment.query.all()}
    streets = {s.id: s for s in Street.query.all()}
    mahallas = {m.id: m for m in Mahalla.query.all()}
    districts = {d.id: d for d in District.query.all()}

    tree = {}
    for r in recs:
        a = assign.get(r.worker_id)
        street = streets.get(a.street_id) if a else None
        mahalla = mahallas.get(street.mahalla_id) if street else None
        district = districts.get(mahalla.district_id) if mahalla else None

        dname = district.name if district else "Без района"
        mname = mahalla.name if mahalla else "Без махалли"
        sname = street.name if street else "Без улицы"

        cm = ConversationMember.query.filter_by(conversation_id=r.conversation_id, user_id=r.worker_id).first()
        is_read = bool(cm and cm.last_read_at >= r.message.sent_at)

        tree.setdefault(dname, {"_total": 0, "_read": 0, "mahallas": {}})
        tree[dname]["_total"] += 1
        tree[dname]["_read"] += 1 if is_read else 0

        tree[dname]["mahallas"].setdefault(mname, {"_total": 0, "_read": 0, "streets": {}})
        tree[dname]["mahallas"][mname]["_total"] += 1
        tree[dname]["mahallas"][mname]["_read"] += 1 if is_read else 0

        tree[dname]["mahallas"][mname]["streets"].setdefault(sname, {"_total": 0, "_read": 0, "workers": []})
        tree[dname]["mahallas"][mname]["streets"][sname]["_total"] += 1
        tree[dname]["mahallas"][mname]["streets"][sname]["_read"] += 1 if is_read else 0

        tree[dname]["mahallas"][mname]["streets"][sname]["workers"].append({
            "worker_id": r.worker_id,
            "worker_name": r.worker.full_name,
            "read": is_read
        })

    return jsonify({
        "batch": {"id": batch.id, "title": batch.title, "created_at": _iso_uz(batch.created_at)},
        "tree": tree
    })


@bp.post("/manager/tasks/send")
@api_auth_required("manager")
def manager_tasks_send():
    u = request.api_user

    data = request.get_json(silent=True) or {}
    title = (request.form.get("title") or data.get("title") or "").strip()
    body = (request.form.get("body") or data.get("body") or "").strip()
    mode = (request.form.get("mode") or data.get("mode") or "all").strip()  # all | districts | mahallas | streets

    if not title:
        return jsonify({"error": "title_required"}), 400

    mode_to_param = {
        "districts": "district_ids",
        "mahallas": "mahalla_ids",
        "streets": "street_ids",
    }
    mode_to_error = {
        "districts": "districts_required",
        "mahallas": "mahallas_required",
        "streets": "streets_required",
    }

    if mode != "all" and mode not in mode_to_param:
        return jsonify({"error": "invalid_mode"}), 400

    target_ids = []
    if mode in mode_to_param:
        param = mode_to_param[mode]
        raw = request.form.getlist(param)
        if not raw:
            raw = data.get(param) or []
        if isinstance(raw, str):
            raw = [x for x in raw.replace(";", ",").split(",") if x.strip()]
        target_ids = _parse_int_ids(raw)
        if not target_ids:
            return jsonify({"error": mode_to_error[mode]}), 400

    worker_ids = _worker_ids_for_target(mode, target_ids)
    if not worker_ids:
        return jsonify({"error": "no_recipients"}), 400

    batch = TaskBatch(manager_id=u.id, title=title, body=body)
    db.session.add(batch)
    db.session.flush()

    files = request.files.getlist("attachments")
    notify_items = []

    for wid in worker_ids:
        worker = User.query.filter_by(id=wid, role="worker", is_active=True).first()
        if not worker:
            continue

        conv = get_or_create_conversation(u.id, worker.id)
        msg = Message(
            conversation_id=conv.id,
            sender_id=u.id,
            type="task",
            title=title,
            body=body,
            sent_at=datetime.utcnow(),
        )
        db.session.add(msg)
        db.session.flush()

        if files:
            re_files = []
            for f in files:
                if not f or not f.filename:
                    continue
                re_files.append((f.filename, f.mimetype, f.read()))
                f.stream.seek(0)
            from werkzeug.datastructures import FileStorage
            import io
            rebuilt = [FileStorage(stream=io.BytesIO(b), filename=n, content_type=m) for (n, m, b) in re_files]
            save_files(rebuilt, msg.id)

        conv.last_message_at = datetime.utcnow()
        db.session.add(conv)

        db.session.add(TaskBatchRecipient(
            batch_id=batch.id,
            worker_id=worker.id,
            conversation_id=conv.id,
            message_id=msg.id
        ))
        notify_items.append((conv.id, msg.id))

    db.session.commit()

    for conv_id, msg_id in notify_items:
        notify_conversation(conv_id, {"type": "new_message", "conversation_id": conv_id, "message_id": msg_id})
        try:
            conv = Conversation.query.get(conv_id)
            if conv:
                _push_to_user(
                    conv.worker_id,
                    "Новая задача",
                    title or "Задача",
                    {"type": "task", "conversation_id": conv_id, "message_id": msg_id},
                )
        except Exception:
            pass

    return jsonify({"ok": True, "batch_id": batch.id})


@bp.get("/inbox")
@api_auth_required("worker")
def inbox():
    u = request.api_user
    conv = Conversation.query.filter_by(worker_id=u.id).first()

    if not conv:
        return jsonify({"conversation": None, "tasks_new": [], "tasks_seen": []})

    cm = ConversationMember.query.filter_by(conversation_id=conv.id, user_id=u.id).first()
    last_read = cm.last_read_at if cm else datetime(1970, 1, 1)

    tasks = Message.query.filter_by(conversation_id=conv.id, type="task").order_by(Message.sent_at.desc()).all()
    tasks_new = []
    tasks_seen = []
    for t in tasks:
        item = {
            "id": t.id,
            "title": t.title or "",
            "body": t.body or "",
            "sent_at": _iso_uz(t.sent_at),
        }
        if t.sent_at > last_read:
            tasks_new.append(item)
        else:
            tasks_seen.append(item)

    return jsonify({
        "conversation": {
            "id": conv.id,
            "manager_name": conv.manager.full_name,
            "manager_avatar_url": _avatar_url(conv.manager),
        },
        "tasks_new": tasks_new,
        "tasks_seen": tasks_seen
    })


def _msg_to_json(m: Message):
    atts = Attachment.query.filter_by(message_id=m.id).order_by(Attachment.id.asc()).all()
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "sender_id": m.sender_id,
        "type": m.type,
        "title": m.title,
        "body": m.body,
        "sent_at": _iso_uz(m.sent_at),
        "attachments": [
            {
                "id": a.id,
                "kind": a.kind,
                "original_name": a.original_name,
                "size": a.size,
                "mime": a.mime_type,
                "url": f"/api/files/{a.id}",
            } for a in atts
        ]
    }


@bp.get("/chat/<int:conversation_id>/messages")
@api_auth_required("worker", "manager")
def chat_messages(conversation_id: int):
    u = request.api_user
    conv = Conversation.query.filter_by(id=conversation_id).first_or_404()
    if u.id not in (conv.worker_id, conv.manager_id):
        abort(403)

    msgs = Message.query.filter_by(conversation_id=conv.id).order_by(Message.sent_at.asc()).all()
    return jsonify({
        "conversation": {
            "id": conv.id,
            "manager_name": conv.manager.full_name,
            "worker_name": conv.worker.full_name,
            "manager_id": conv.manager_id,
            "worker_id": conv.worker_id,
            "manager_avatar_url": _avatar_url(conv.manager),
            "worker_avatar_url": _avatar_url(conv.worker),
        },
        "messages": [_msg_to_json(m) for m in msgs]
    })


@bp.post("/chat/<int:conversation_id>/read")
@api_auth_required("worker", "manager")
def chat_read(conversation_id: int):
    u = request.api_user
    conv = Conversation.query.filter_by(id=conversation_id).first_or_404()
    if u.id not in (conv.worker_id, conv.manager_id):
        abort(403)
    update_last_read(conv.id, u.id)
    db.session.commit()
    return jsonify({"ok": True})


@bp.post("/chat/<int:conversation_id>/send")
@api_auth_required("worker", "manager")
def chat_send(conversation_id: int):
    u = request.api_user
    conv = Conversation.query.filter_by(id=conversation_id).first_or_404()
    if u.id not in (conv.worker_id, conv.manager_id):
        abort(403)

    body = (request.form.get("body") or "").strip()
    files = request.files.getlist("attachments")

    if not body and not any(f and f.filename for f in files):
        return jsonify({"error": "empty"}), 400

    msg = Message(
        conversation_id=conv.id,
        sender_id=u.id,
        type="text",
        body=body,
        sent_at=datetime.utcnow()
    )
    db.session.add(msg)
    db.session.flush()

    if files:
        # ВАЖНО: FileStorage переиспользовать нельзя — читаем bytes и пересоздаём
        re_files = []
        for f in files:
            if not f or not f.filename:
                continue
            re_files.append((f.filename, f.mimetype, f.read()))
            f.stream.seek(0)

        import io
        rebuilt = [FileStorage(stream=io.BytesIO(b), filename=n, content_type=m) for (n, m, b) in re_files]
        save_files(rebuilt, msg.id)

    conv.last_message_at = datetime.utcnow()
    db.session.add(conv)
    db.session.commit()
    notify_conversation(conv.id, {"type": "new_message", "conversation_id": conv.id, "message_id": msg.id})
    try:
        other_id = conv.worker_id if u.id == conv.manager_id else conv.manager_id
        preview = body if body else "Файл"
        _push_to_user(
            other_id,
            u.full_name,
            preview[:120],
            {"type": "message", "conversation_id": conv.id, "message_id": msg.id},
        )
    except Exception:
        pass

    return jsonify({"ok": True, "message": _msg_to_json(msg)})


# =========================
# Protected file download
# =========================
@bp.get("/files/<int:attachment_id>")
@api_auth_required("worker", "manager")
def file_get_api(attachment_id: int):
    u = request.api_user
    a = Attachment.query.filter_by(id=attachment_id).first_or_404()
    msg = Message.query.filter_by(id=a.message_id).first_or_404()
    conv = Conversation.query.filter_by(id=msg.conversation_id).first_or_404()

    # участник чата
    if u.id not in (conv.worker_id, conv.manager_id):
        abort(403)

    full_path = Path(current_app.config["UPLOAD_DIR"]) / a.stored_path
    if not full_path.exists():
        abort(404)
    return send_file(full_path, as_attachment=False, download_name=a.original_name)


@bp.get("/avatar/<int:user_id>")
@api_auth_required("worker", "manager")
def api_avatar_get(user_id: int):
    user = User.query.get_or_404(user_id)
    if not user.photo_path:
        abort(404)
    full_path = Path(current_app.config["UPLOAD_DIR"]) / user.photo_path
    if not full_path.exists():
        abort(404)
    return send_file(full_path, as_attachment=False)

from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import jwt
from flask import Blueprint, current_app, jsonify, request, send_file, abort
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import aliased
from werkzeug.security import check_password_hash
from werkzeug.datastructures import FileStorage

from . import db
from .models import (
    User, Conversation, Message, Attachment, ConversationMember,
    WorkerAssignment, Street, Mahalla, District,
    TaskBatch, TaskBatchRecipient, DeviceToken, TaskDispatchJob
)
from .tasks import enqueue_retry_failed, enqueue_task_dispatch, get_job_payload_or_404, job_failures_payload
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
JOB_FINAL_STATUSES = {"succeeded", "partial", "failed"}


def _api_ok(payload: dict | None = None, status: int = 200):
    body = {"ok": True}
    if payload:
        body.update(payload)
    return jsonify(body), status


def _api_error(message: str, error_code: str, status: int = 400):
    return jsonify({"ok": False, "message": message, "error_code": error_code}), status


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
        return [
            worker_id
            for (worker_id,) in User.query.filter_by(role="worker", is_active=True).with_entities(User.id).all()
        ]

    workers_q = (
        User.query
        .join(WorkerAssignment, WorkerAssignment.worker_id == User.id)
        .join(Street, WorkerAssignment.street_id == Street.id)
        .join(Mahalla, Street.mahalla_id == Mahalla.id)
        .filter(User.role == "worker", User.is_active.is_(True))
    )

    if mode == "districts":
        workers_q = workers_q.filter(Mahalla.district_id.in_(target_ids))
    elif mode == "mahallas":
        workers_q = workers_q.filter(Street.mahalla_id.in_(target_ids))
    elif mode == "streets":
        workers_q = workers_q.filter(WorkerAssignment.street_id.in_(target_ids))
    else:
        return []

    return [
        worker_id
        for (worker_id,) in workers_q.with_entities(User.id).distinct().all()
    ]


def _manager_target_options():
    district_rows = District.query.order_by(District.name.asc()).all()
    mahalla_rows = Mahalla.query.order_by(Mahalla.name.asc()).all()
    street_rows = Street.query.order_by(Street.name.asc()).all()

    district_counts = {
        district_id: count
        for district_id, count in (
            db.session.query(Mahalla.district_id, func.count(User.id))
            .join(Street, Street.mahalla_id == Mahalla.id)
            .join(WorkerAssignment, WorkerAssignment.street_id == Street.id)
            .join(User, User.id == WorkerAssignment.worker_id)
            .filter(User.role == "worker", User.is_active.is_(True))
            .group_by(Mahalla.district_id)
            .all()
        )
    }
    mahalla_counts = {
        mahalla_id: count
        for mahalla_id, count in (
            db.session.query(Street.mahalla_id, func.count(User.id))
            .join(WorkerAssignment, WorkerAssignment.street_id == Street.id)
            .join(User, User.id == WorkerAssignment.worker_id)
            .filter(User.role == "worker", User.is_active.is_(True))
            .group_by(Street.mahalla_id)
            .all()
        )
    }
    street_counts = {
        street_id: count
        for street_id, count in (
            db.session.query(WorkerAssignment.street_id, func.count(User.id))
            .join(User, User.id == WorkerAssignment.worker_id)
            .filter(User.role == "worker", User.is_active.is_(True))
            .group_by(WorkerAssignment.street_id)
            .all()
        )
    }

    district_by_id = {d.id: d for d in district_rows}
    mahalla_by_id = {m.id: m for m in mahalla_rows}

    districts = [
        {
            "id": district.id,
            "name": district.name,
            "count": district_counts.get(district.id, 0),
        }
        for district in district_rows
    ]

    mahallas = []
    for mahalla in mahalla_rows:
        district = district_by_id.get(mahalla.district_id)
        mahallas.append(
            {
                "id": mahalla.id,
                "name": mahalla.name,
                "district_id": mahalla.district_id,
                "district_name": district.name if district else "",
                "count": mahalla_counts.get(mahalla.id, 0),
            }
        )

    streets = []
    for street in street_rows:
        mahalla = mahalla_by_id.get(street.mahalla_id)
        district = district_by_id.get(mahalla.district_id) if mahalla else None
        streets.append(
            {
                "id": street.id,
                "name": street.name,
                "mahalla_id": street.mahalla_id,
                "mahalla_name": mahalla.name if mahalla else "",
                "district_id": district.id if district else None,
                "district_name": district.name if district else "",
                "count": street_counts.get(street.id, 0),
            }
        )

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

    conv_q = Conversation.query.filter(Conversation.manager_id == u.id)
    if district_id or mahalla_id:
        conv_q = (
            conv_q
            .join(WorkerAssignment, WorkerAssignment.worker_id == Conversation.worker_id)
            .join(Street, Street.id == WorkerAssignment.street_id)
            .join(Mahalla, Mahalla.id == Street.mahalla_id)
        )
        if district_id:
            conv_q = conv_q.filter(Mahalla.district_id == district_id)
        if mahalla_id:
            conv_q = conv_q.filter(Street.mahalla_id == mahalla_id)

    convs = conv_q.order_by(Conversation.last_message_at.desc()).all()
    conv_ids = [conversation.id for conversation in convs]
    worker_ids = [conversation.worker_id for conversation in convs]

    workers_by_id = {}
    assign_by_worker = {}
    streets_by_id = {}
    mahallas_by_id = {}
    districts_by_id = {}
    unread_by_conv = {}
    last_msg_by_conv = {}

    if worker_ids:
        workers_by_id = {
            worker.id: worker
            for worker in User.query.filter(User.id.in_(worker_ids)).all()
        }

        assignments = WorkerAssignment.query.filter(WorkerAssignment.worker_id.in_(worker_ids)).all()
        assign_by_worker = {assignment.worker_id: assignment for assignment in assignments}

        street_ids = sorted({assignment.street_id for assignment in assignments})
        if street_ids:
            street_rows = Street.query.filter(Street.id.in_(street_ids)).all()
            streets_by_id = {street.id: street for street in street_rows}

            mahalla_ids = sorted({street.mahalla_id for street in street_rows})
            if mahalla_ids:
                mahalla_rows = Mahalla.query.filter(Mahalla.id.in_(mahalla_ids)).all()
                mahallas_by_id = {mahalla.id: mahalla for mahalla in mahalla_rows}

                district_ids = sorted({mahalla.district_id for mahalla in mahalla_rows})
                if district_ids:
                    district_rows = District.query.filter(District.id.in_(district_ids)).all()
                    districts_by_id = {district.id: district for district in district_rows}

    if conv_ids:
        cm_alias = aliased(ConversationMember)
        unread_rows = (
            db.session.query(Message.conversation_id, func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .outerjoin(
                cm_alias,
                and_(
                    cm_alias.conversation_id == Message.conversation_id,
                    cm_alias.user_id == u.id,
                ),
            )
            .filter(Message.conversation_id.in_(conv_ids))
            .filter(Message.sender_id == Conversation.worker_id)
            .filter(or_(cm_alias.last_read_at.is_(None), Message.sent_at > cm_alias.last_read_at))
            .group_by(Message.conversation_id)
            .all()
        )
        unread_by_conv = {conversation_id: unread for conversation_id, unread in unread_rows}

        last_msg_subq = (
            db.session.query(
                Message.conversation_id.label("conversation_id"),
                func.max(Message.id).label("max_message_id"),
            )
            .filter(Message.conversation_id.in_(conv_ids))
            .group_by(Message.conversation_id)
            .subquery()
        )
        last_messages = (
            db.session.query(Message)
            .join(last_msg_subq, Message.id == last_msg_subq.c.max_message_id)
            .all()
        )
        last_msg_by_conv = {message.conversation_id: message for message in last_messages}

    items = []
    for conversation in convs:
        worker = workers_by_id.get(conversation.worker_id)
        if not worker:
            continue

        assignment = assign_by_worker.get(conversation.worker_id)
        street = streets_by_id.get(assignment.street_id) if assignment else None
        mahalla = mahallas_by_id.get(street.mahalla_id) if street else None
        district = districts_by_id.get(mahalla.district_id) if mahalla else None
        last_msg = last_msg_by_conv.get(conversation.id)

        items.append(
            {
                "id": conversation.id,
                "worker_id": conversation.worker_id,
                "worker_name": worker.full_name,
                "worker_avatar_url": _avatar_url(worker),
                "district": district.name if district else "",
                "mahalla": mahalla.name if mahalla else "",
                "street": street.name if street else "",
                "unread": int(unread_by_conv.get(conversation.id, 0) or 0),
                "last_message_at": _iso_uz(conversation.last_message_at),
                "last_message_text": (last_msg.body or last_msg.title or "") if last_msg else "",
                "last_message_type": last_msg.type if last_msg else None,
                "last_message_time": _iso_uz(last_msg.sent_at) if last_msg else None,
            }
        )

    district_opts, mahalla_opts, street_opts = _manager_target_options()

    return _api_ok(
        {
            "message": "OK",
            "items": items,
            "districts": district_opts,
            "mahallas": mahalla_opts,
            "streets": street_opts,
        }
    )


@bp.get("/manager/tasks")
@api_auth_required("manager")
def manager_tasks():
    u = request.api_user
    batches = (
        TaskBatch.query
        .filter_by(manager_id=u.id)
        .order_by(TaskBatch.created_at.desc())
        .all()
    )

    batch_ids = [batch.id for batch in batches]
    stats_map = {}
    if batch_ids:
        stats_rows = (
            db.session.query(
                TaskBatchRecipient.batch_id.label("batch_id"),
                func.count(TaskBatchRecipient.id).label("total"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    ConversationMember.last_read_at.isnot(None),
                                    ConversationMember.last_read_at >= Message.sent_at,
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("read"),
            )
            .join(Message, Message.id == TaskBatchRecipient.message_id)
            .outerjoin(
                ConversationMember,
                and_(
                    ConversationMember.conversation_id == TaskBatchRecipient.conversation_id,
                    ConversationMember.user_id == TaskBatchRecipient.worker_id,
                ),
            )
            .filter(TaskBatchRecipient.batch_id.in_(batch_ids))
            .group_by(TaskBatchRecipient.batch_id)
            .all()
        )
        stats_map = {row.batch_id: (int(row.total or 0), int(row.read or 0)) for row in stats_rows}

    items = []
    for batch in batches:
        total, read = stats_map.get(batch.id, (0, 0))
        percent = int((read / total) * 100) if total else 0
        items.append(
            {
                "id": batch.id,
                "title": batch.title,
                "body": batch.body,
                "created_at": _iso_uz(batch.created_at),
                "total": total,
                "read": read,
                "percent": percent,
            }
        )

    return _api_ok({"message": "OK", "items": items})


@bp.get("/manager/tasks/<int:batch_id>")
@api_auth_required("manager")
def manager_task_details(batch_id: int):
    u = request.api_user
    batch = TaskBatch.query.filter_by(id=batch_id, manager_id=u.id).first_or_404()

    rows = (
        db.session.query(
            TaskBatchRecipient.worker_id.label("worker_id"),
            User.last_name.label("last_name"),
            User.first_name.label("first_name"),
            User.middle_name.label("middle_name"),
            District.name.label("district_name"),
            Mahalla.name.label("mahalla_name"),
            Street.name.label("street_name"),
            Message.sent_at.label("sent_at"),
            ConversationMember.last_read_at.label("last_read_at"),
        )
        .join(User, User.id == TaskBatchRecipient.worker_id)
        .join(Message, Message.id == TaskBatchRecipient.message_id)
        .outerjoin(WorkerAssignment, WorkerAssignment.worker_id == TaskBatchRecipient.worker_id)
        .outerjoin(Street, Street.id == WorkerAssignment.street_id)
        .outerjoin(Mahalla, Mahalla.id == Street.mahalla_id)
        .outerjoin(District, District.id == Mahalla.district_id)
        .outerjoin(
            ConversationMember,
            and_(
                ConversationMember.conversation_id == TaskBatchRecipient.conversation_id,
                ConversationMember.user_id == TaskBatchRecipient.worker_id,
            ),
        )
        .filter(TaskBatchRecipient.batch_id == batch.id)
        .all()
    )

    tree = {}
    for row in rows:
        district_name = row.district_name or "Без района"
        mahalla_name = row.mahalla_name or "Без махалли"
        street_name = row.street_name or "Без улицы"
        is_read = bool(row.last_read_at and row.sent_at and row.last_read_at >= row.sent_at)

        worker_name = " ".join(
            value for value in [row.last_name, row.first_name, row.middle_name] if value
        ).strip() or f"Worker #{row.worker_id}"

        tree.setdefault(district_name, {"_total": 0, "_read": 0, "mahallas": {}})
        tree[district_name]["_total"] += 1
        tree[district_name]["_read"] += 1 if is_read else 0

        tree[district_name]["mahallas"].setdefault(mahalla_name, {"_total": 0, "_read": 0, "streets": {}})
        tree[district_name]["mahallas"][mahalla_name]["_total"] += 1
        tree[district_name]["mahallas"][mahalla_name]["_read"] += 1 if is_read else 0

        tree[district_name]["mahallas"][mahalla_name]["streets"].setdefault(
            street_name,
            {"_total": 0, "_read": 0, "workers": []},
        )
        tree[district_name]["mahallas"][mahalla_name]["streets"][street_name]["_total"] += 1
        tree[district_name]["mahallas"][mahalla_name]["streets"][street_name]["_read"] += 1 if is_read else 0

        tree[district_name]["mahallas"][mahalla_name]["streets"][street_name]["workers"].append(
            {
                "worker_id": row.worker_id,
                "worker_name": worker_name,
                "read": is_read,
            }
        )

    return _api_ok(
        {
            "message": "OK",
            "batch": {"id": batch.id, "title": batch.title, "created_at": _iso_uz(batch.created_at)},
            "tree": tree,
        }
    )


@bp.post("/manager/tasks/send")
@api_auth_required("manager")
def manager_tasks_send():
    u = request.api_user

    data = request.get_json(silent=True) or {}
    title = (request.form.get("title") or data.get("title") or "").strip()
    body = (request.form.get("body") or data.get("body") or "").strip()
    mode = (request.form.get("mode") or data.get("mode") or "all").strip()

    if not title:
        return _api_error("Название задачи обязательно", "title_required", 400)

    mode_to_param = {
        "districts": "district_ids",
        "mahallas": "mahalla_ids",
        "streets": "street_ids",
    }
    mode_to_error = {
        "districts": "Выберите хотя бы один район",
        "mahallas": "Выберите хотя бы одну махаллю",
        "streets": "Выберите хотя бы одну улицу",
    }

    if mode != "all" and mode not in mode_to_param:
        return _api_error("Неверный режим отправки", "invalid_mode", 400)

    target_ids = []
    if mode in mode_to_param:
        param = mode_to_param[mode]
        raw = request.form.getlist(param)
        if not raw:
            raw = data.get(param) or []
        if isinstance(raw, str):
            raw = [value for value in raw.replace(";", ",").split(",") if value.strip()]
        target_ids = _parse_int_ids(raw)
        if not target_ids:
            return _api_error(mode_to_error[mode], "target_required", 400)

    worker_ids = _worker_ids_for_target(mode, target_ids)
    if not worker_ids:
        return _api_error("Нет работников для выбранной группы", "no_recipients", 400)

    files = request.files.getlist("attachments")

    try:
        job = enqueue_task_dispatch(
            manager_id=u.id,
            title=title,
            body=body,
            mode=mode,
            target_ids=target_ids,
            worker_ids=worker_ids,
            files=files,
        )
    except Exception as exc:
        return _api_error(f"Не удалось поставить задачу в очередь: {exc}", "queue_error", 500)

    return _api_ok(
        {
            "job_id": job.id,
            "status": job.status,
            "total_workers": int(job.total_workers or 0),
            "message": "Задача поставлена в очередь",
        },
        status=202,
    )


@bp.get("/manager/tasks/jobs/<job_id>")
@api_auth_required("manager")
def manager_task_job_status(job_id: str):
    u = request.api_user
    payload = get_job_payload_or_404(job_id, u.id)
    if not payload:
        return _api_error("Задача не найдена", "not_found", 404)
    payload["message"] = "OK"
    return _api_ok(payload)


@bp.get("/manager/tasks/jobs/<job_id>/failures")
@api_auth_required("manager")
def manager_task_job_failures(job_id: str):
    u = request.api_user
    job = TaskDispatchJob.query.filter_by(id=job_id, manager_id=u.id).first()
    if not job:
        return _api_error("Задача не найдена", "not_found", 404)

    page = request.args.get("page", default=1, type=int)
    per_page = request.args.get("per_page", default=50, type=int)
    payload = job_failures_payload(job, page=page, per_page=per_page)
    payload["job_id"] = job.id
    payload["message"] = "OK"
    return _api_ok(payload)


@bp.post("/manager/tasks/jobs/<job_id>/retry-failed")
@api_auth_required("manager")
def manager_task_job_retry_failed(job_id: str):
    u = request.api_user
    source_job = TaskDispatchJob.query.filter_by(id=job_id, manager_id=u.id).first()
    if not source_job:
        return _api_error("Задача не найдена", "not_found", 404)

    retry_job = enqueue_retry_failed(source_job=source_job, manager_id=u.id)
    if not retry_job:
        return _api_error("Нет неуспешных получателей", "nothing_to_retry", 400)

    return _api_ok({"retry_job_id": retry_job.id, "status": retry_job.status}, status=202)


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

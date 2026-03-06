from datetime import datetime

from flask import Blueprint, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import aliased

from . import db
from .models import Conversation, ConversationMember, District, Mahalla, Message, Street, TaskBatch, TaskBatchRecipient, TaskDispatchJob, User, WorkerAssignment
from .realtime import notify_conversation
from .tasks import enqueue_retry_failed, enqueue_task_dispatch, get_job_payload_or_404, job_failures_payload
from .utils import get_or_create_conversation, require_role, save_files, update_last_read

bp = Blueprint("manager", __name__)

def _manager_id():
    return current_user.id

def _all_workers_query():
    return User.query.filter_by(role="worker", is_active=True)


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


def _task_targets_payload():
    total_all = _all_workers_query().count()

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

    districts_json = [
        {"id": d.id, "name": d.name, "count": district_counts.get(d.id, 0)}
        for d in district_rows
    ]

    mahallas_json = []
    for m in mahalla_rows:
        district = district_by_id.get(m.district_id)
        mahallas_json.append({
            "id": m.id,
            "name": m.name,
            "district_id": m.district_id,
            "district_name": district.name if district else "",
            "count": mahalla_counts.get(m.id, 0),
        })

    streets_json = []
    for s in street_rows:
        mahalla = mahalla_by_id.get(s.mahalla_id)
        district = district_by_id.get(mahalla.district_id) if mahalla else None
        streets_json.append({
            "id": s.id,
            "name": s.name,
            "mahalla_id": s.mahalla_id,
            "mahalla_name": mahalla.name if mahalla else "",
            "district_id": district.id if district else None,
            "district_name": district.name if district else "",
            "count": street_counts.get(s.id, 0),
        })

    return total_all, districts_json, mahallas_json, streets_json




def _wants_json() -> bool:
    if request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest":
        return True
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    return best == "application/json" and request.accept_mimetypes[best] > request.accept_mimetypes["text/html"]

def _worker_ids_for_target(mode: str, target_ids):
    if mode == "all":
        return [worker_id for (worker_id,) in _all_workers_query().with_entities(User.id).all()]

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

    return [worker_id for (worker_id,) in workers_q.with_entities(User.id).distinct().all()]

@bp.get("/tasks")
@login_required
def tasks():
    require_role(current_user, "manager")
    batches = (
        TaskBatch.query
        .filter_by(manager_id=_manager_id())
        .order_by(TaskBatch.created_at.desc())
        .all()
    )

    batch_ids = [b.id for b in batches]
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

    data = []
    for batch in batches:
        total, read = stats_map.get(batch.id, (0, 0))
        percent = int((read / total) * 100) if total else 0
        data.append({"batch": batch, "total": total, "read": read, "percent": percent})

    total_all, districts_json, mahallas_json, streets_json = _task_targets_payload()

    return render_template(
        "manager_tasks.html",
        batches=data,
        districts_json=districts_json,
        mahallas_json=mahallas_json,
        streets_json=streets_json,
        total_all=total_all,
    )


@bp.post("/tasks/send")
@login_required
def send_task():
    require_role(current_user, "manager")

    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()
    mode = request.form.get("mode", "all").strip()

    if not title:
        if _wants_json():
            return jsonify({"ok": False, "error_code": "title_required", "message": "Название задачи обязательно"}), 400
        flash("Название задачи обязательно", "error")
        return redirect(url_for("manager.tasks"))

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
        if _wants_json():
            return jsonify({"ok": False, "error_code": "invalid_mode", "message": "Неверный режим отправки"}), 400
        flash("Неверный режим отправки", "error")
        return redirect(url_for("manager.tasks"))

    target_ids = []
    if mode in mode_to_param:
        target_ids = _parse_int_ids(request.form.getlist(mode_to_param[mode]))
        if not target_ids:
            if _wants_json():
                return jsonify({"ok": False, "error_code": "target_required", "message": mode_to_error[mode]}), 400
            flash(mode_to_error[mode], "error")
            return redirect(url_for("manager.tasks"))

    worker_ids = _worker_ids_for_target(mode, target_ids)
    if not worker_ids:
        if _wants_json():
            return jsonify({"ok": False, "error_code": "no_recipients", "message": "Нет работников для выбранной группы"}), 400
        flash("Нет работников для выбранной группы", "error")
        return redirect(url_for("manager.tasks"))

    files = request.files.getlist("attachments")

    try:
        job = enqueue_task_dispatch(
            manager_id=_manager_id(),
            title=title,
            body=body,
            mode=mode,
            target_ids=target_ids,
            worker_ids=worker_ids,
            files=files,
        )
    except Exception as exc:
        if _wants_json():
            return jsonify({"ok": False, "error_code": "queue_error", "message": str(exc)}), 500
        flash(f"Не удалось поставить задачу в очередь: {exc}", "error")
        return redirect(url_for("manager.tasks"))

    payload = {
        "ok": True,
        "job_id": job.id,
        "status": job.status,
        "total_workers": job.total_workers,
        "message": "Задача поставлена в очередь",
    }

    if _wants_json():
        return jsonify(payload), 202

    flash("Задача поставлена в очередь", "ok")
    return redirect(url_for("manager.tasks"))


@bp.get("/tasks/jobs/<job_id>")
@login_required
def task_job_status(job_id: str):
    require_role(current_user, "manager")
    payload = get_job_payload_or_404(job_id, _manager_id())
    if not payload:
        return jsonify({"ok": False, "error_code": "not_found", "message": "Задача не найдена"}), 404
    payload["ok"] = True
    return jsonify(payload)


@bp.get("/tasks/jobs/<job_id>/failures")
@login_required
def task_job_failures(job_id: str):
    require_role(current_user, "manager")
    job = TaskDispatchJob.query.filter_by(id=job_id, manager_id=_manager_id()).first_or_404()
    page = request.args.get("page", default=1, type=int)
    per_page = request.args.get("per_page", default=50, type=int)
    payload = job_failures_payload(job, page=page, per_page=per_page)
    payload["ok"] = True
    payload["job_id"] = job.id
    return jsonify(payload)


@bp.post("/tasks/jobs/<job_id>/retry-failed")
@login_required
def task_job_retry_failed(job_id: str):
    require_role(current_user, "manager")
    source_job = TaskDispatchJob.query.filter_by(id=job_id, manager_id=_manager_id()).first_or_404()

    retry_job = enqueue_retry_failed(source_job=source_job, manager_id=_manager_id())
    if not retry_job:
        return jsonify({"ok": False, "error_code": "nothing_to_retry", "message": "Нет неуспешных получателей"}), 400

    return jsonify({"ok": True, "retry_job_id": retry_job.id, "status": retry_job.status}), 202


@bp.get("/tasks/details/<int:batch_id>")
@login_required
def task_details(batch_id: int):
    require_role(current_user, "manager")
    batch = TaskBatch.query.filter_by(id=batch_id, manager_id=_manager_id()).first_or_404()

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

    return jsonify({
        "batch": {"id": batch.id, "title": batch.title, "created_at": batch.created_at.isoformat()},
        "tree": tree,
    })


@bp.get("/chats")
@login_required
def chats():
    require_role(current_user, "manager")

    district_id = request.args.get("district_id", type=int)
    mahalla_id = request.args.get("mahalla_id", type=int)
    page = request.args.get("page", default=1, type=int)
    per_page = request.args.get("per_page", default=50, type=int)
    per_page = max(20, min(per_page, 100))

    conv_q = Conversation.query.filter(Conversation.manager_id == _manager_id())

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

    conv_page = conv_q.order_by(Conversation.last_message_at.desc()).paginate(
        page=page,
        per_page=per_page,
        error_out=False,
    )
    convs = conv_page.items

    conv_ids = [c.id for c in convs]
    worker_ids = [c.worker_id for c in convs]

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
                    cm_alias.user_id == _manager_id(),
                ),
            )
            .filter(Message.conversation_id.in_(conv_ids))
            .filter(Message.sender_id == Conversation.worker_id)
            .filter(or_(cm_alias.last_read_at.is_(None), Message.sent_at > cm_alias.last_read_at))
            .group_by(Message.conversation_id)
            .all()
        )
        unread_by_conv = {conv_id: unread for conv_id, unread in unread_rows}

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
        last_msg_by_conv = {msg.conversation_id: msg for msg in last_messages}

    items = []
    for conv in convs:
        worker = workers_by_id.get(conv.worker_id)
        if not worker:
            continue

        assignment = assign_by_worker.get(conv.worker_id)
        street = streets_by_id.get(assignment.street_id) if assignment else None
        mahalla = mahallas_by_id.get(street.mahalla_id) if street else None
        district = districts_by_id.get(mahalla.district_id) if mahalla else None

        items.append(
            {
                "conv": conv,
                "worker": worker,
                "district": district.name if district else "",
                "mahalla": mahalla.name if mahalla else "",
                "street": street.name if street else "",
                "unread": unread_by_conv.get(conv.id, 0),
                "last_msg": last_msg_by_conv.get(conv.id),
            }
        )

    all_districts = District.query.order_by(District.name.asc()).all()
    mahalla_filter_q = Mahalla.query.order_by(Mahalla.name.asc())
    if district_id:
        mahalla_filter_q = mahalla_filter_q.filter(Mahalla.district_id == district_id)
    all_mahallas = mahalla_filter_q.all()

    return render_template(
        "manager_chats.html",
        items=items,
        chats_page=conv_page,
        per_page=per_page,
        districts=all_districts,
        mahallas=all_mahallas,
        district_id=district_id,
        mahalla_id=mahalla_id,
    )

@bp.get("/chat/<int:conversation_id>")
@login_required
def chat(conversation_id: int):
    require_role(current_user, "manager")
    conv = Conversation.query.filter_by(id=conversation_id, manager_id=_manager_id()).first_or_404()
    # mark as read for manager
    update_last_read(conv.id, _manager_id())
    db.session.commit()
    return render_template("chat.html", conv=conv, me=current_user)

@bp.get("/chat/<int:conversation_id>/poll")
@login_required
def chat_poll(conversation_id: int):
    require_role(current_user, "manager")
    conv = Conversation.query.filter_by(id=conversation_id, manager_id=_manager_id()).first_or_404()
    update_last_read(conv.id, _manager_id())
    db.session.commit()
    resp = make_response(render_template("partials/chat_messages.html", messages=conv.messages, me=current_user, conv=conv))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@bp.get("/workers.json")
@login_required
def workers_json():
    require_role(current_user, "manager")
    workers = User.query.filter_by(role="worker", is_active=True).order_by(User.last_name.asc()).all()
    return jsonify([{"id": w.id, "name": w.full_name, "login": w.login} for w in workers])


@bp.post("/chat/<int:conversation_id>/send")
@login_required
def chat_send(conversation_id: int):
    require_role(current_user, "manager")
    conv = Conversation.query.filter_by(id=conversation_id, manager_id=_manager_id()).first_or_404()

    body = request.form.get("body", "").strip()
    files = request.files.getlist("attachments")

    if not body and not any(f and f.filename for f in files):
        flash("Пустое сообщение", "error")
        return redirect(url_for("manager.chat", conversation_id=conv.id))

    msg = Message(conversation_id=conv.id, sender_id=_manager_id(), type="text", body=body, sent_at=datetime.utcnow())
    db.session.add(msg)
    db.session.flush()

    if files:
        # rebuild streams (как в tasks)
        re_files = []
        for f in files:
            if not f or not f.filename:
                continue
            re_files.append((f.filename, f.mimetype, f.read()))
            f.stream.seek(0)
        from werkzeug.datastructures import FileStorage
        import io
        rebuilt = [FileStorage(stream=io.BytesIO(b), filename=n, content_type=m) for (n,m,b) in re_files]
        save_files(rebuilt, msg.id)

    conv.last_message_at = datetime.utcnow()
    db.session.add(conv)
    db.session.commit()
    notify_conversation(conv.id, {"type": "new_message", "conversation_id": conv.id, "message_id": msg.id})

    return redirect(url_for("manager.chat", conversation_id=conv.id))

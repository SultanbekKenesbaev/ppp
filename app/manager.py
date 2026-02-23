from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, make_response
from flask_login import login_required, current_user
from . import db
from .models import User, WorkerAssignment, Street, Mahalla, District, Conversation, Message, TaskBatch, TaskBatchRecipient, ConversationMember
from .utils import require_role, get_or_create_conversation, save_files, update_last_read
from .realtime import notify_conversation

bp = Blueprint("manager", __name__)

def _manager_id():
    return current_user.id

def _all_workers_query():
    return User.query.filter_by(role="worker", is_active=True)

@bp.get("/tasks")
@login_required
def tasks():
    require_role(current_user, "manager")
    batches = TaskBatch.query.filter_by(manager_id=_manager_id()).order_by(TaskBatch.created_at.desc()).all()

    # рассчитать проценты
    data = []
    for b in batches:
        recs = TaskBatchRecipient.query.filter_by(batch_id=b.id).all()
        total = len(recs)
        read = 0
        for r in recs:
            cm = ConversationMember.query.filter_by(conversation_id=r.conversation_id, user_id=r.worker_id).first()
            if cm and cm.last_read_at >= r.message.sent_at:
                read += 1
        percent = int((read / total) * 100) if total else 0
        data.append({"batch": b, "total": total, "read": read, "percent": percent})

    # --- СЧЁТЧИКИ ДЛЯ UI (районы -> сколько работников) ---
    # active workers
    active_workers = _all_workers_query().all()
    total_all = len(active_workers)

    # mapping: worker_id -> district_id
    assigns = WorkerAssignment.query.all()
    streets = {s.id: s for s in Street.query.all()}
    mahallas = {m.id: m for m in Mahalla.query.all()}
    districts = District.query.order_by(District.name.asc()).all()

    worker_to_district = {}
    for a in assigns:
        st = streets.get(a.street_id)
        mh = mahallas.get(st.mahalla_id) if st else None
        did = mh.district_id if mh else None
        if did:
            worker_to_district[a.worker_id] = did

    # count workers per district (only active)
    counts = {}
    for w in active_workers:
        did = worker_to_district.get(w.id)
        if did:
            counts[did] = counts.get(did, 0) + 1

    districts_json = [{"id": d.id, "name": d.name, "count": counts.get(d.id, 0)} for d in districts]

    return render_template("manager_tasks.html",
                           batches=data,
                           districts_json=districts_json,
                           total_all=total_all)


@bp.post("/tasks/send")
@login_required
def send_task():
    require_role(current_user, "manager")

    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()
    mode = request.form.get("mode", "all")  # all | districts

    if not title:
        flash("Название задачи обязательно", "error")
        return redirect(url_for("manager.tasks"))

    # recipients
    worker_ids = []
    if mode == "districts":
        raw = request.form.getlist("district_ids")
        district_ids = [int(x) for x in raw if x.isdigit()]

        if not district_ids:
            flash("Выберите хотя бы один район", "error")
            return redirect(url_for("manager.tasks"))

        # workers in selected districts (active)
        workers = (
            User.query
            .join(WorkerAssignment, WorkerAssignment.worker_id == User.id)
            .join(Street, WorkerAssignment.street_id == Street.id)
            .join(Mahalla, Street.mahalla_id == Mahalla.id)
            .filter(User.role == "worker", User.is_active == True)
            .filter(Mahalla.district_id.in_(district_ids))
            .all()
        )
        worker_ids = [w.id for w in workers]
    else:
        worker_ids = [w.id for w in _all_workers_query().all()]


    # batch create
    batch = TaskBatch(manager_id=_manager_id(), title=title, body=body)
    db.session.add(batch)
    db.session.flush()

    # attachments: input name="attachments" multiple
    files = request.files.getlist("attachments")

    notify_items = []
    for wid in worker_ids:
        worker = User.query.filter_by(id=wid, role="worker", is_active=True).first()
        if not worker:
            continue

        conv = get_or_create_conversation(_manager_id(), worker.id)
        msg = Message(
            conversation_id=conv.id,
            sender_id=_manager_id(),
            type="task",
            title=title,
            body=body,
            sent_at=datetime.utcnow(),
        )
        db.session.add(msg)
        db.session.flush()

        # save attachments for this message (копия ссылок на те же файлы нам ок? для простоты: сохраняем один раз на message)
        if files:
            # ВАЖНО: FileStorage нельзя переиспользовать после save() для нескольких получателей.
            # Поэтому: сохраняем вложения только для первого получателя? НЕЛЬЗЯ — работник должен видеть.
            # Решение для MVP: сохраняем файлы в память bytes и пишем заново.
            # Сделаем простое: читаем bytes один раз и пишем для каждого.
            re_files = []
            for f in files:
                if not f or not f.filename:
                    continue
                re_files.append((f.filename, f.mimetype, f.read()))
                f.stream.seek(0)
            # сохранить байты для каждого получателя
            from werkzeug.datastructures import FileStorage
            import io
            rebuilt = []
            for (fname, mime, bts) in re_files:
                rebuilt.append(FileStorage(stream=io.BytesIO(bts), filename=fname, content_type=mime))
            save_files(rebuilt, msg.id)

        # update conversation last_message_at
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
    flash("Задача отправлена", "ok")
    return redirect(url_for("manager.tasks"))

@bp.get("/tasks/details/<int:batch_id>")
@login_required
def task_details(batch_id: int):
    require_role(current_user, "manager")
    batch = TaskBatch.query.filter_by(id=batch_id, manager_id=_manager_id()).first_or_404()
    recs = TaskBatchRecipient.query.filter_by(batch_id=batch.id).all()

    # build structure map for analytics
    # worker -> street -> mahalla -> district
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
        "batch": {"id": batch.id, "title": batch.title, "created_at": batch.created_at.isoformat()},
        "tree": tree
    })

@bp.get("/chats")
@login_required
def chats():
    require_role(current_user, "manager")

    # filters
    district_id = request.args.get("district_id", type=int)
    mahalla_id = request.args.get("mahalla_id", type=int)

    # all conversations with this manager
    qs = Conversation.query.filter_by(manager_id=_manager_id()).order_by(Conversation.last_message_at.desc())
    convs = qs.all()

    # attach worker structure for filtering
    assign = {a.worker_id: a for a in WorkerAssignment.query.all()}
    streets = {s.id: s for s in Street.query.all()}
    mahallas = {m.id: m for m in Mahalla.query.all()}
    districts = {d.id: d for d in District.query.all()}

    filtered = []
    for c in convs:
        a = assign.get(c.worker_id)
        street = streets.get(a.street_id) if a else None
        mah = mahallas.get(street.mahalla_id) if street else None
        dist = districts.get(mah.district_id) if mah else None

        if district_id and (not dist or dist.id != district_id):
            continue
        if mahalla_id and (not mah or mah.id != mahalla_id):
            continue

        # unread count for manager: messages from worker after manager last_read_at
        cm = ConversationMember.query.filter_by(conversation_id=c.id, user_id=_manager_id()).first()
        last_read = cm.last_read_at if cm else datetime(1970,1,1)
        unread = Message.query.filter(
            Message.conversation_id == c.id,
            Message.sender_id == c.worker_id,
            Message.sent_at > last_read
        ).count()

        last_msg = Message.query.filter_by(conversation_id=c.id).order_by(Message.sent_at.desc()).first()

        filtered.append({
            "conv": c,
            "worker": c.worker,
            "district": dist.name if dist else "",
            "mahalla": mah.name if mah else "",
            "street": street.name if street else "",
            "unread": unread,
            "last_msg": last_msg
        })

    all_districts = District.query.order_by(District.name.asc()).all()
    all_mahallas = Mahalla.query.order_by(Mahalla.name.asc()).all()

    return render_template("manager_chats.html",
                           items=filtered,
                           districts=all_districts,
                           mahallas=all_mahallas,
                           district_id=district_id,
                           mahalla_id=mahalla_id)

@bp.get("/chat/<int:conversation_id>")
@login_required
def chat(conversation_id: int):
    require_role(current_user, "manager")
    conv = Conversation.query.filter_by(id=conversation_id, manager_id=_manager_id()).first_or_404()
    # mark as read for manager
    from .utils import update_last_read
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

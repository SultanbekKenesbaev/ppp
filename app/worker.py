from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, make_response
from flask_login import login_required, current_user
from . import db
from .models import User, Conversation, Message, ConversationMember
from .utils import require_role, save_files, update_last_read
from .realtime import notify_conversation

bp = Blueprint("worker", __name__)

def _my_id():
    return current_user.id

def _my_conversation():
    # у работника один разговор с руководителем (по факту может быть один, но по БД — строго unique по (manager, worker))
    conv = Conversation.query.filter_by(worker_id=_my_id()).first()
    return conv

@bp.get("/inbox")
@login_required
def inbox():
    require_role(current_user, "worker")
    conv = _my_conversation()
    if not conv:
        # если админ создал работника, а руководитель еще не писал — чат может не существовать
        # Покажем пусто, но без падения
        return render_template("worker_inbox.html", tasks_new=[], tasks_seen=[], conv=None)

    cm = ConversationMember.query.filter_by(conversation_id=conv.id, user_id=_my_id()).first()
    last_read = cm.last_read_at if cm else datetime(1970,1,1)

    # все task-сообщения от руководителя
    tasks = Message.query.filter_by(conversation_id=conv.id, type="task").order_by(Message.sent_at.desc()).all()
    tasks_new = [t for t in tasks if t.sent_at > last_read]
    tasks_seen = [t for t in tasks if t.sent_at <= last_read]

    return render_template("worker_inbox.html", tasks_new=tasks_new, tasks_seen=tasks_seen, conv=conv)

@bp.get("/chat")
@login_required
def chat():
    require_role(current_user, "worker")
    conv = _my_conversation()
    if not conv:
        flash("Пока нет чата с руководителем", "error")
        return redirect(url_for("worker.inbox"))

    # mark read
    update_last_read(conv.id, _my_id())
    db.session.commit()

    return render_template("chat.html", conv=conv, me=current_user)

@bp.get("/chat/poll")
@login_required
def chat_poll():
    require_role(current_user, "worker")
    conv = _my_conversation()
    if not conv:
        resp = make_response("")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    update_last_read(conv.id, _my_id())
    db.session.commit()
    resp = make_response(render_template("partials/chat_messages.html", messages=conv.messages, me=current_user, conv=conv))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@bp.post("/chat/send")
@login_required
def chat_send():
    require_role(current_user, "worker")
    conv = _my_conversation()
    if not conv:
        flash("Чат не найден", "error")
        return redirect(url_for("worker.inbox"))

    body = request.form.get("body", "").strip()
    files = request.files.getlist("attachments")

    if not body and not any(f and f.filename for f in files):
        flash("Пустое сообщение", "error")
        return redirect(url_for("worker.chat"))

    msg = Message(conversation_id=conv.id, sender_id=_my_id(), type="text", body=body, sent_at=datetime.utcnow())
    db.session.add(msg)
    db.session.flush()

    if files:
        # rebuild once (не нужно для одного получателя, но оставим единообразно)
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

    return redirect(url_for("worker.chat"))

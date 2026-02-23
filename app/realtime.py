import json
import threading

import jwt
from flask import request, current_app
from flask_login import current_user

from . import sock
from .models import Conversation, User

_ws_lock = threading.Lock()
_ws_rooms: dict[int, set] = {}


def _socket_user() -> User | None:
    if current_user.is_authenticated:
        return current_user

    token = (request.args.get("access_token") or request.args.get("token") or "").strip()
    if not token:
        return None

    data = _decode_token(token)
    if not data or data.get("type") != "access":
        return None

    try:
        user_id = int(data.get("sub") or 0)
    except Exception:
        return None

    user = User.query.filter_by(id=user_id, is_active=True).first()
    return user


def _decode_token(token: str):
    try:
        return jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def _room_key(conversation_id: int) -> int:
    return int(conversation_id)


def _add_ws(conversation_id: int, ws):
    key = _room_key(conversation_id)
    with _ws_lock:
        _ws_rooms.setdefault(key, set()).add(ws)


def _remove_ws(conversation_id: int, ws):
    key = _room_key(conversation_id)
    with _ws_lock:
        conns = _ws_rooms.get(key)
        if not conns:
            return
        conns.discard(ws)
        if not conns:
            _ws_rooms.pop(key, None)


def notify_conversation(conversation_id: int, payload: dict):
    key = _room_key(conversation_id)
    data = json.dumps(payload)
    with _ws_lock:
        conns = list(_ws_rooms.get(key, set()))

    for ws in conns:
        try:
            ws.send(data)
        except Exception:
            _remove_ws(conversation_id, ws)


@sock.route("/ws/chat")
def chat_ws(ws):
    user = _socket_user()
    conv_id = request.args.get("conversation_id", type=int)
    if not user or not conv_id:
        ws.close()
        return

    conv = Conversation.query.filter_by(id=conv_id).first()
    if not conv or user.id not in (conv.manager_id, conv.worker_id):
        ws.close()
        return

    _add_ws(conv_id, ws)
    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
    finally:
        _remove_ws(conv_id, ws)

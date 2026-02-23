import json
import os
import urllib.request


FCM_URL = "https://fcm.googleapis.com/fcm/send"


def send_fcm(tokens, title, body, data=None):
    key = os.getenv("FCM_SERVER_KEY", "")
    if not key or not tokens:
        return None

    payload = {
        "registration_ids": tokens,
        "notification": {
            "title": title,
            "body": body,
        },
        "data": data or {},
        "priority": "high",
    }

    req = urllib.request.Request(
        FCM_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"key={key}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except Exception:
        return None

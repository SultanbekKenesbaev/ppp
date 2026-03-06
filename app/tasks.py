import io
import json
import secrets
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable

from flask import current_app
from redis import Redis
from rq import Queue
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from . import create_app, db
from .models import (
    Conversation,
    DeviceToken,
    Message,
    TaskBatch,
    TaskBatchRecipient,
    TaskDispatchFailure,
    TaskDispatchJob,
    User,
)
from .push import send_fcm
from .realtime import notify_conversation
from .utils import get_or_create_conversation, save_files

FINAL_STATUSES = {"succeeded", "partial", "failed"}


def _utcnow() -> datetime:
    return datetime.utcnow()


def _queue() -> Queue:
    redis_conn = Redis.from_url(current_app.config["REDIS_URL"])
    return Queue(current_app.config.get("RQ_QUEUE_NAME", "taskplatform"), connection=redis_conn)


def _tmp_dir_for_job(job_id: str) -> Path:
    base = Path(current_app.config["UPLOAD_DIR"]) / "task_dispatch_tmp" / job_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def _safe_filename(name: str) -> str:
    clean = secure_filename(name or "")
    return clean or f"file_{secrets.token_hex(4)}"


def _persist_uploaded_attachments(job_id: str, files: Iterable[FileStorage]) -> list[dict]:
    manifest: list[dict] = []
    tmp_dir = _tmp_dir_for_job(job_id)

    for f in files or []:
        if not f or not getattr(f, "filename", ""):
            continue

        original_name = f.filename
        mime_type = getattr(f, "mimetype", "") or "application/octet-stream"
        uniq = f"{_utcnow().timestamp():.6f}".replace(".", "")
        stored_name = f"{uniq}_{_safe_filename(original_name)}"
        full_path = tmp_dir / stored_name
        f.save(full_path)

        manifest.append(
            {
                "original_name": original_name,
                "mime_type": mime_type,
                "tmp_path": str(full_path),
            }
        )

    return manifest


def _load_manifest_files(manifest: list[dict]) -> list[FileStorage]:
    files: list[FileStorage] = []
    for item in manifest:
        path = Path(item.get("tmp_path") or "")
        if not path.exists() or not path.is_file():
            continue

        with path.open("rb") as f:
            data = f.read()

        files.append(
            FileStorage(
                stream=io.BytesIO(data),
                filename=item.get("original_name") or path.name,
                content_type=item.get("mime_type") or "application/octet-stream",
            )
        )
    return files


def _push_to_user(user_id: int, title: str, body: str, data: dict | None = None) -> None:
    tokens = [t.token for t in DeviceToken.query.filter_by(user_id=user_id, platform="android").all()]
    if not tokens:
        return

    resp = send_fcm(tokens, title, body, data or {})
    if not resp or "results" not in resp:
        return

    bad_tokens: list[str] = []
    for token, result in zip(tokens, resp.get("results", [])):
        err = result.get("error")
        if err in ("NotRegistered", "InvalidRegistration", "MismatchSenderId", "InvalidPackageName"):
            bad_tokens.append(token)

    if bad_tokens:
        DeviceToken.query.filter(DeviceToken.token.in_(bad_tokens)).delete(synchronize_session=False)
        db.session.commit()


def _job_payload(job: TaskDispatchJob) -> dict:
    total = int(job.total_workers or 0)
    processed = int(job.processed_workers or 0)
    progress = int((processed / total) * 100) if total else 0
    return {
        "job_id": job.id,
        "status": job.status,
        "progress_percent": progress,
        "total": total,
        "processed": processed,
        "sent": int(job.sent_count or 0),
        "failed": int(job.failed_count or 0),
        "stage": job.stage,
        "error_message": job.error_message or None,
        "batch_id": job.batch_id,
    }


def get_job_payload_or_404(job_id: str, manager_id: int) -> dict | None:
    job = TaskDispatchJob.query.filter_by(id=job_id, manager_id=manager_id).first()
    if not job:
        return None
    return _job_payload(job)


def enqueue_task_dispatch(
    *,
    manager_id: int,
    title: str,
    body: str,
    mode: str,
    target_ids: list[int],
    worker_ids: list[int],
    files: Iterable[FileStorage],
    retry_of_job_id: str | None = None,
) -> TaskDispatchJob:
    job_id = str(uuid.uuid4())
    manifest = _persist_uploaded_attachments(job_id, files)

    job = TaskDispatchJob(
        id=job_id,
        manager_id=manager_id,
        title=title,
        body=body,
        mode=mode,
        target_ids_json=json.dumps(target_ids, ensure_ascii=False),
        worker_ids_json=json.dumps(worker_ids, ensure_ascii=False),
        attachment_manifest_json=json.dumps(manifest, ensure_ascii=False),
        status="queued",
        stage="preparing",
        total_workers=len(worker_ids),
        processed_workers=0,
        sent_count=0,
        failed_count=0,
        retry_of_job_id=retry_of_job_id,
    )
    db.session.add(job)
    db.session.commit()

    try:
        _queue().enqueue(
            "app.tasks.run_task_dispatch",
            job_id,
            job_timeout=4 * 60 * 60,
            result_ttl=24 * 60 * 60,
            failure_ttl=7 * 24 * 60 * 60,
        )
    except Exception as exc:
        job.status = "failed"
        job.stage = "finalizing"
        job.error_message = f"Не удалось поставить задачу в очередь: {exc}"[:1000]
        job.finished_at = _utcnow()
        db.session.add(job)
        db.session.commit()
        raise

    return job


def enqueue_retry_failed(*, source_job: TaskDispatchJob, manager_id: int) -> TaskDispatchJob | None:
    if source_job.manager_id != manager_id:
        return None

    failed_worker_ids = sorted({row.worker_id for row in source_job.failures if row.worker_id})
    if not failed_worker_ids:
        return None

    try:
        source_manifest = json.loads(source_job.attachment_manifest_json or "[]")
        if not isinstance(source_manifest, list):
            source_manifest = []
    except Exception:
        source_manifest = []

    job_id = str(uuid.uuid4())
    dest_dir = _tmp_dir_for_job(job_id)
    copied_manifest: list[dict] = []

    for item in source_manifest:
        src = Path(item.get("tmp_path") or "")
        if not src.exists() or not src.is_file():
            continue
        dst = dest_dir / f"{secrets.token_hex(4)}_{src.name}"
        shutil.copy2(src, dst)
        copied_manifest.append(
            {
                "original_name": item.get("original_name") or src.name,
                "mime_type": item.get("mime_type") or "application/octet-stream",
                "tmp_path": str(dst),
            }
        )

    retry_job = TaskDispatchJob(
        id=job_id,
        manager_id=manager_id,
        title=source_job.title,
        body=source_job.body,
        mode=source_job.mode,
        target_ids_json=source_job.target_ids_json,
        worker_ids_json=json.dumps(failed_worker_ids, ensure_ascii=False),
        attachment_manifest_json=json.dumps(copied_manifest, ensure_ascii=False),
        status="queued",
        stage="preparing",
        total_workers=len(failed_worker_ids),
        retry_of_job_id=source_job.id,
    )
    db.session.add(retry_job)
    db.session.commit()

    _queue().enqueue(
        "app.tasks.run_task_dispatch",
        retry_job.id,
        job_timeout=4 * 60 * 60,
        result_ttl=24 * 60 * 60,
        failure_ttl=7 * 24 * 60 * 60,
    )

    return retry_job


def run_task_dispatch(job_id: str) -> None:
    app = create_app()
    with app.app_context():
        _run_task_dispatch(job_id)


def _run_task_dispatch(job_id: str) -> None:
    job = TaskDispatchJob.query.filter_by(id=job_id).first()
    if not job:
        return
    if job.status in FINAL_STATUSES | {"running"}:
        return

    try:
        worker_ids_raw = json.loads(job.worker_ids_json or "[]")
        worker_ids = [int(x) for x in worker_ids_raw if str(x).isdigit()]
    except Exception:
        worker_ids = []

    try:
        attachment_manifest = json.loads(job.attachment_manifest_json or "[]")
        if not isinstance(attachment_manifest, list):
            attachment_manifest = []
    except Exception:
        attachment_manifest = []

    job.status = "running"
    job.stage = "preparing"
    job.started_at = _utcnow()
    job.error_message = ""
    job.total_workers = len(worker_ids)
    job.processed_workers = 0
    job.sent_count = 0
    job.failed_count = 0
    db.session.add(job)
    db.session.commit()

    if not worker_ids:
        job.status = "failed"
        job.stage = "finalizing"
        job.error_message = "Нет получателей для отправки"
        job.finished_at = _utcnow()
        db.session.add(job)
        db.session.commit()
        return

    TaskDispatchFailure.query.filter_by(job_id=job.id).delete(synchronize_session=False)

    batch = TaskBatch(manager_id=job.manager_id, title=job.title, body=job.body)
    db.session.add(batch)
    db.session.flush()
    job.batch_id = batch.id
    job.stage = "sending"
    db.session.add(job)
    db.session.commit()

    batch_size = int(current_app.config.get("TASK_SEND_BATCH_SIZE", 200) or 200)
    batch_size = max(20, min(batch_size, 1000))

    pending_notify: list[tuple[int, int, int]] = []

    for idx, worker_id in enumerate(worker_ids, start=1):
        savepoint = db.session.begin_nested()
        try:
            worker = User.query.filter_by(id=worker_id, role="worker", is_active=True).first()
            if not worker:
                raise ValueError("Работник не найден или неактивен")

            conv = get_or_create_conversation(job.manager_id, worker.id)
            msg = Message(
                conversation_id=conv.id,
                sender_id=job.manager_id,
                type="task",
                title=job.title,
                body=job.body,
                sent_at=_utcnow(),
            )
            db.session.add(msg)
            db.session.flush()

            files = _load_manifest_files(attachment_manifest)
            if files:
                save_files(files, msg.id)

            conv.last_message_at = _utcnow()
            db.session.add(conv)

            db.session.add(
                TaskBatchRecipient(
                    batch_id=batch.id,
                    worker_id=worker.id,
                    conversation_id=conv.id,
                    message_id=msg.id,
                )
            )

            savepoint.commit()
            job.sent_count += 1
            pending_notify.append((conv.id, msg.id, worker.id))

        except Exception as exc:
            savepoint.rollback()
            db.session.add(
                TaskDispatchFailure(
                    job_id=job.id,
                    worker_id=worker_id,
                    reason=str(exc).strip()[:500] or "Ошибка отправки",
                )
            )
            job.failed_count += 1

        finally:
            job.processed_workers = idx

        is_flush_point = idx % batch_size == 0 or idx == len(worker_ids)
        if is_flush_point:
            db.session.add(job)
            db.session.commit()

            for conv_id, msg_id, uid in pending_notify:
                notify_conversation(
                    conv_id,
                    {"type": "new_message", "conversation_id": conv_id, "message_id": msg_id},
                )
                try:
                    _push_to_user(
                        uid,
                        "Новая задача",
                        job.title or "Задача",
                        {"type": "task", "conversation_id": conv_id, "message_id": msg_id},
                    )
                except Exception:
                    pass
            pending_notify.clear()

    job.stage = "finalizing"
    if job.sent_count <= 0 and job.failed_count > 0:
        job.status = "failed"
        job.error_message = "Не удалось отправить задачу ни одному получателю"
    elif job.failed_count > 0:
        job.status = "partial"
        job.error_message = "Часть получателей не получила задачу"
    else:
        job.status = "succeeded"
        job.error_message = ""

    job.finished_at = _utcnow()
    db.session.add(job)
    db.session.commit()


def job_failures_payload(job: TaskDispatchJob, *, page: int = 1, per_page: int = 50) -> dict:
    page = max(page, 1)
    per_page = min(max(per_page, 20), 200)

    page_obj = (
        TaskDispatchFailure.query
        .filter_by(job_id=job.id)
        .order_by(TaskDispatchFailure.id.asc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )

    return {
        "items": [
            {
                "id": item.id,
                "worker_id": item.worker_id,
                "worker_name": item.worker.full_name if item.worker else "",
                "reason": item.reason,
                "created_at": item.created_at.isoformat(timespec="seconds"),
            }
            for item in page_obj.items
        ],
        "page": page_obj.page,
        "per_page": page_obj.per_page,
        "pages": page_obj.pages,
        "total": page_obj.total,
    }

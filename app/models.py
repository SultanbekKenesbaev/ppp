from datetime import datetime
from flask_login import UserMixin
from . import db

class District(db.Model):
    __tablename__ = "districts"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)

class Mahalla(db.Model):
    __tablename__ = "mahallas"
    id = db.Column(db.Integer, primary_key=True)
    district_id = db.Column(db.Integer, db.ForeignKey("districts.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)

    district = db.relationship("District", backref=db.backref("mahallas", lazy=True))

class Street(db.Model):
    __tablename__ = "streets"
    id = db.Column(db.Integer, primary_key=True)
    mahalla_id = db.Column(db.Integer, db.ForeignKey("mahallas.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)

    mahalla = db.relationship("Mahalla", backref=db.backref("streets", lazy=True))

class User(db.Model, UserMixin):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(20), nullable=False)  # admin | manager | worker

    login = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    first_name = db.Column(db.String(120), nullable=False)
    last_name = db.Column(db.String(120), nullable=False)
    middle_name = db.Column(db.String(120), default="")

    photo_path = db.Column(db.String(255), default="")
    note = db.Column(db.String(255), default="")  # пожелание / заметка
    is_active = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def full_name(self):
        mid = f" {self.middle_name}" if self.middle_name else ""
        return f"{self.last_name} {self.first_name}{mid}"

class WorkerAssignment(db.Model):
    __tablename__ = "worker_assignments"
    id = db.Column(db.Integer, primary_key=True)
    street_id = db.Column(db.Integer, db.ForeignKey("streets.id"), unique=True, nullable=False)
    worker_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    street = db.relationship("Street")
    worker = db.relationship("User")

class Conversation(db.Model):
    __tablename__ = "conversations"
    id = db.Column(db.Integer, primary_key=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    worker_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_message_at = db.Column(db.DateTime, default=datetime.utcnow)

    manager = db.relationship("User", foreign_keys=[manager_id])
    worker = db.relationship("User", foreign_keys=[worker_id])

    __table_args__ = (
        db.UniqueConstraint("manager_id", "worker_id", name="uq_manager_worker_conversation"),
    )

class ConversationMember(db.Model):
    __tablename__ = "conversation_members"
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    last_read_at = db.Column(db.DateTime, default=datetime(1970, 1, 1))

    conversation = db.relationship("Conversation", backref=db.backref("members", lazy=True))
    user = db.relationship("User")

    __table_args__ = (
        db.UniqueConstraint("conversation_id", "user_id", name="uq_conversation_user"),
    )

class Message(db.Model):
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=False)

    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    type = db.Column(db.String(20), default="text")  # text | task
    title = db.Column(db.String(200), default="")    # for task
    body = db.Column(db.Text, default="")
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)

    conversation = db.relationship("Conversation", backref=db.backref("messages", lazy=True))
    sender = db.relationship("User")

class Attachment(db.Model):
    __tablename__ = "attachments"
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("messages.id"), nullable=False)

    original_name = db.Column(db.String(255), nullable=False)
    stored_path = db.Column(db.String(500), nullable=False)  # relative path from UPLOAD_DIR
    mime_type = db.Column(db.String(120), default="")
    size = db.Column(db.Integer, default=0)
    kind = db.Column(db.String(20), default="file")  # image | video | file

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    message = db.relationship("Message", backref=db.backref("attachments", lazy=True))

class DeviceToken(db.Model):
    __tablename__ = "device_tokens"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    token = db.Column(db.String(512), unique=True, nullable=False)
    platform = db.Column(db.String(20), default="android")  # android | ios
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")

class TaskBatch(db.Model):
    """
    Группа отправки задачи: руководитель отправил 'одну задачу' нескольким/всем.
    Нужна для аналитики и процента прочитавших.
    """
    __tablename__ = "task_batches"
    id = db.Column(db.Integer, primary_key=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    manager = db.relationship("User")

class TaskBatchRecipient(db.Model):
    __tablename__ = "task_batch_recipients"
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("task_batches.id"), nullable=False)

    worker_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=False)
    message_id = db.Column(db.Integer, db.ForeignKey("messages.id"), nullable=False)

    batch = db.relationship("TaskBatch", backref=db.backref("recipients", lazy=True))
    worker = db.relationship("User")
    conversation = db.relationship("Conversation")
    message = db.relationship("Message")

    __table_args__ = (
        db.UniqueConstraint("batch_id", "worker_id", name="uq_batch_worker"),
    )


class TaskDispatchJob(db.Model):
    __tablename__ = "task_dispatch_jobs"

    id = db.Column(db.String(36), primary_key=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    batch_id = db.Column(db.Integer, db.ForeignKey("task_batches.id"), nullable=True)
    retry_of_job_id = db.Column(db.String(36), db.ForeignKey("task_dispatch_jobs.id"), nullable=True)

    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, default="")
    mode = db.Column(db.String(20), nullable=False, default="all")
    target_ids_json = db.Column(db.Text, default="[]")
    worker_ids_json = db.Column(db.Text, default="[]")
    attachment_manifest_json = db.Column(db.Text, default="[]")

    status = db.Column(db.String(20), nullable=False, default="queued")
    stage = db.Column(db.String(32), nullable=False, default="preparing")
    error_message = db.Column(db.Text, default="")

    total_workers = db.Column(db.Integer, nullable=False, default=0)
    processed_workers = db.Column(db.Integer, nullable=False, default=0)
    sent_count = db.Column(db.Integer, nullable=False, default=0)
    failed_count = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    manager = db.relationship("User", foreign_keys=[manager_id])
    batch = db.relationship("TaskBatch")


class TaskDispatchFailure(db.Model):
    __tablename__ = "task_dispatch_failures"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(36), db.ForeignKey("task_dispatch_jobs.id"), nullable=False)
    worker_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reason = db.Column(db.String(500), nullable=False, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    job = db.relationship("TaskDispatchJob", backref=db.backref("failures", lazy=True, cascade="all, delete-orphan"))
    worker = db.relationship("User")

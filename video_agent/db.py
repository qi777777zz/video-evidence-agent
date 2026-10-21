from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import settings


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class CommonFields:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class TenantLock(Base):
    __tablename__ = "tenant_locks"
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True)


class Tenant(CommonFields, Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)


class User(CommonFields, Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(160))


class Workspace(CommonFields, Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    __table_args__ = (Index("ix_workspace_tenant_created", "tenant_id", "created_at", "id"),)


class Membership(CommonFields, Base):
    __tablename__ = "memberships"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(30), default="member", nullable=False)
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_membership_workspace_user"),)


class Video(Base):
    __tablename__ = "videos"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    filename: Mapped[str] = mapped_column(String(500), default="")
    sha256: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(255))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_type: Mapped[str] = mapped_column(String(20), default="local", nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2000))
    original_title: Mapped[str | None] = mapped_column(String(500))
    thumbnail_url: Mapped[str | None] = mapped_column(String(2000))
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    audio_key: Mapped[str | None] = mapped_column(String(500))
    audio_version: Mapped[str | None] = mapped_column(String(255))
    audio_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    summary_data: Mapped[dict | None] = mapped_column(JSON)
    summary_version: Mapped[str | None] = mapped_column(String(255))
    summary_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(30), default="uploaded")
    stage: Mapped[str] = mapped_column(String(40), default="uploaded")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    workflow_run: Mapped[int] = mapped_column(Integer, default=0)
    asr_status: Mapped[str] = mapped_column(String(20), default="pending")
    ocr_status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    webhook_url: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    original_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidences: Mapped[list["Evidence"]] = relationship(back_populates="video", cascade="all, delete-orphan")
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (UniqueConstraint("tenant_id", "sha256", "parser_version", name="uq_video_tenant_hash_parser"),
        Index("ix_video_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_video_tenant_status_created", "tenant_id", "status", "created_at", "id"),
        Index("ix_video_lifecycle", "status", "updated_at", "id"))


class Evidence(Base):
    __tablename__ = "evidences"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    modality: Mapped[str] = mapped_column(String(20))
    evidence_source: Mapped[str] = mapped_column(String(40), default="unknown", nullable=False)
    start_ms: Mapped[int] = mapped_column(BigInteger)
    end_ms: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float | None]
    source_key: Mapped[str | None] = mapped_column(String(500))
    parser_version: Mapped[str] = mapped_column(String(255))
    embedding: Mapped[list[float] | None] = mapped_column(JSON)
    embedding_model: Mapped[str | None] = mapped_column(String(160))
    index_status: Mapped[str] = mapped_column(String(20), default="pending")
    business_key: Mapped[str] = mapped_column(String(255), default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    video: Mapped[Video] = relationship(back_populates="evidences")
    __table_args__ = (Index("ix_evidence_time", "video_id", "start_ms"),
        UniqueConstraint("video_id", "business_key", name="uq_evidence_business_key"),
        UniqueConstraint("video_id", "evidence_source", "start_ms", "parser_version",
                         name="uq_evidence_source_time_parser"))


class StageExecution(Base):
    """User-visible stage snapshot; Temporal remains the execution source of truth."""
    __tablename__ = "stage_executions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    input_version: Mapped[str] = mapped_column(String(255))
    output_version: Mapped[str | None] = mapped_column(String(255))
    output_data: Mapped[dict | list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("video_id", "stage", "input_version",
                                      name="uq_stage_input_version"),)


class Submission(CommonFields, Base):
    __tablename__ = "submissions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    request_hash: Mapped[str] = mapped_column(String(64))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"))
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    multipart_id: Mapped[str] = mapped_column(String(500))
    filename: Mapped[str] = mapped_column(String(500), default="")
    declared_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __table_args__ = (Index("ix_upload_tenant_created", "tenant_id", "created_at", "id"),
                      Index("ix_upload_lifecycle", "status", "created_at", "id"))


class ProcessingTask(CommonFields, Base):
    __tablename__ = "processing_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(80), nullable=False)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    __mapper_args__ = {"version_id_col": CommonFields.version}
    __table_args__ = (Index("ix_task_tenant_status_created", "tenant_id", "status", "created_at", "id"),)


class ProcessingStage(CommonFields, Base):
    __tablename__ = "processing_stages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("processing_tasks.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    __table_args__ = (UniqueConstraint("task_id", "name", name="uq_task_stage"),)


class Conversation(CommonFields, Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(80), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"))
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id"))
    title: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (Index("ix_conversation_tenant_created", "tenant_id", "created_at", "id"),)


class Message(CommonFields, Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (Index("ix_message_conversation_created", "conversation_id", "created_at", "id"),)


class ConversationSummary(CommonFields, Base):
    """Durable boundary between summarized and still-recent messages."""
    __tablename__ = "conversation_summaries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), nullable=False, unique=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    through_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), nullable=False)
    __table_args__ = (Index("ix_conversation_summary_conversation", "conversation_id"),)


class AnalysisReport(CommonFields, Base):
    __tablename__ = "analysis_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(80), nullable=False)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)


class ReportVersion(CommonFields, Base):
    __tablename__ = "report_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    report_id: Mapped[str] = mapped_column(ForeignKey("analysis_reports.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint("report_id", "revision", name="uq_report_revision"),)


class OutboxEvent(CommonFields, Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    outbox_event_key: Mapped[str] = mapped_column(String(190), unique=True, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(80), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_outbox_lifecycle", "status", "available_at", "id"),)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(120))
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    prefix: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(255))
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_from_id: Mapped[str | None] = mapped_column(ForeignKey("api_keys.id"))


class ApiKeyAudit(Base):
    __tablename__ = "api_key_audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    key_id: Mapped[str | None] = mapped_column(ForeignKey("api_keys.id"), index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(80), index=True)
    event: Mapped[str] = mapped_column(String(40), index=True)
    success: Mapped[bool] = mapped_column(Boolean)
    request_id: Mapped[str | None] = mapped_column(String(80))
    method: Mapped[str | None] = mapped_column(String(10))
    path: Mapped[str | None] = mapped_column(String(500))
    remote_addr: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("video_id"),)


engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=1800)
Session = sessionmaker(engine, expire_on_commit=False)


def init_db():
    from .migrations import run_migrations
    run_migrations()

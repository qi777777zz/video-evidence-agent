from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from pgvector.sqlalchemy import Vector

from .config import settings


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Video(Base):
    __tablename__ = "videos"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(255))
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    stage: Mapped[str] = mapped_column(String(40), default="queued")
    asr_status: Mapped[str] = mapped_column(String(20), default="pending")
    ocr_status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    webhook_url: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    original_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidences: Mapped[list["Evidence"]] = relationship(back_populates="video", cascade="all, delete-orphan")
    __table_args__ = (Index("ix_video_hash", "tenant_id", "sha256", "parser_version"),)


class Evidence(Base):
    __tablename__ = "evidences"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    modality: Mapped[str] = mapped_column(String(20))
    start_ms: Mapped[int] = mapped_column(BigInteger)
    end_ms: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float | None]
    source_key: Mapped[str | None] = mapped_column(String(500))
    parser_version: Mapped[str] = mapped_column(String(255))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dimension))
    embedding_model: Mapped[str | None] = mapped_column(String(160))
    index_status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    video: Mapped[Video] = relationship(back_populates="evidences")
    __table_args__ = (Index("ix_evidence_time", "video_id", "start_ms"),)


class Submission(Base):
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
    declared_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


engine = create_engine(settings.database_url, pool_pre_ping=True)
Session = sessionmaker(engine, expire_on_commit=False)


def init_db():
    from .migrations import run_migrations
    run_migrations()

"""Adopt Alembic and reconcile both fresh and legacy installations."""

from alembic import op

from video_agent.db import Base


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind)
    statements = (
        "ALTER TABLE videos ALTER COLUMN parser_version TYPE varchar(255)",
        "ALTER TABLE evidences ALTER COLUMN parser_version TYPE varchar(255)",
        "ALTER TABLE evidences ADD COLUMN IF NOT EXISTS embedding vector(512)",
        "ALTER TABLE evidences ADD COLUMN IF NOT EXISTS embedding_model varchar(160)",
        "ALTER TABLE evidences ADD COLUMN IF NOT EXISTS index_status varchar(20) NOT NULL DEFAULT 'pending'",
        "ALTER TABLE videos ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()",
        "ALTER TABLE videos ADD COLUMN IF NOT EXISTS original_deleted_at timestamptz",
        "ALTER TABLE videos ADD COLUMN IF NOT EXISTS asr_status varchar(20) NOT NULL DEFAULT 'pending'",
        "ALTER TABLE videos ADD COLUMN IF NOT EXISTS ocr_status varchar(20) NOT NULL DEFAULT 'pending'",
        "ALTER TABLE evidences ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now()",
        "ALTER TABLE uploads ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now()",
        "ALTER TABLE uploads ADD COLUMN IF NOT EXISTS completed_at timestamptz",
        "CREATE INDEX IF NOT EXISTS ix_evidence_embedding_hnsw ON evidences USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_upload_lifecycle ON uploads (status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_video_lifecycle ON videos (status, updated_at)",
    )
    for statement in statements:
        bind.exec_driver_sql(statement)


def downgrade():
    # The baseline intentionally has no destructive automatic downgrade.
    pass

"""Add resumable job snapshots and artifact idempotency keys."""
from alembic import op
import sqlalchemy as sa

revision = "0002_resumable_job_state"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    video_columns = {column["name"] for column in inspector.get_columns("videos")}
    with op.batch_alter_table("videos") as batch:
        if "progress" not in video_columns:
            batch.add_column(sa.Column("progress", sa.Integer(), nullable=False, server_default="0"))
        if "workflow_run" not in video_columns:
            batch.add_column(sa.Column("workflow_run", sa.Integer(), nullable=False, server_default="0"))
    evidence_columns = {column["name"] for column in inspector.get_columns("evidences")}
    with op.batch_alter_table("evidences") as batch:
        if "business_key" not in evidence_columns:
            batch.add_column(sa.Column("business_key", sa.String(255), nullable=True))
    inspector = sa.inspect(bind)
    unique_names = {item.get("name") for item in inspector.get_unique_constraints("evidences")}
    if "uq_evidence_business_key" not in unique_names:
        with op.batch_alter_table("evidences") as batch:
            batch.create_unique_constraint("uq_evidence_business_key", ["video_id", "business_key"])
    if "stage_executions" not in inspector.get_table_names():
        op.create_table("stage_executions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("video_id", sa.String(36), sa.ForeignKey("videos.id"), nullable=False),
            sa.Column("stage", sa.String(40), nullable=False),
            sa.Column("input_version", sa.String(255), nullable=False),
            sa.Column("output_version", sa.String(255)),
            sa.Column("output_data", sa.JSON()),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("video_id", "stage", "input_version", name="uq_stage_input_version"))
        op.create_index("ix_stage_executions_video_id", "stage_executions", ["video_id"])


def downgrade():
    # State/history migrations are intentionally forward-only.
    pass

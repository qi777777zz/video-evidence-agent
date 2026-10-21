"""Add source metadata and reusable workbench artifacts."""
from alembic import op
import sqlalchemy as sa

revision = "0003_video_workbench"
down_revision = "0002_resumable_job_state"
branch_labels = None
depends_on = None


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("videos")}
    definitions = [
        ("source_type", sa.String(20), False, "local"),
        ("source_url", sa.String(2000), True, None),
        ("original_title", sa.String(500), True, None),
        ("thumbnail_url", sa.String(2000), True, None),
        ("favorite", sa.Boolean(), False, "0"),
        ("audio_key", sa.String(500), True, None),
        ("audio_version", sa.String(255), True, None),
        ("audio_status", sa.String(20), False, "pending"),
        ("summary_data", sa.JSON(), True, None),
        ("summary_version", sa.String(255), True, None),
        ("summary_status", sa.String(20), False, "pending"),
    ]
    with op.batch_alter_table("videos") as batch:
        for name, kind, nullable, default in definitions:
            if name not in existing:
                batch.add_column(sa.Column(name, kind, nullable=nullable, server_default=default))


def downgrade():
    pass

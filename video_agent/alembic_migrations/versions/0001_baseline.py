"""MySQL business schema baseline (Temporal has its own PostgreSQL)."""

from alembic import op

from video_agent.db import Base


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind)


def downgrade():
    # The baseline intentionally has no destructive automatic downgrade.
    pass

"""Ensure durable conversation tables exist on already-deployed MySQL databases."""

from alembic import op

from video_agent.db import Conversation, Message


revision = "0004_conversation_persistence"
down_revision = "0003_video_workbench"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    Conversation.__table__.create(bind, checkfirst=True)
    Message.__table__.create(bind, checkfirst=True)


def downgrade():
    # Conversation history is user data and must not be removed automatically.
    pass

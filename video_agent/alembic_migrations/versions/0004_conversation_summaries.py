"""Add durable conversation summaries.

Revision ID: 0004_conversation_summaries
Revises: 0004_conversation_persistence
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_conversation_summaries"
down_revision = "0004_conversation_persistence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conversation_summaries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("through_message_id", sa.String(36), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("conversation_id", name="uq_conversation_summaries_conversation_id"),
    )
    op.create_index("ix_conversation_summary_conversation", "conversation_summaries", ["conversation_id"])


def downgrade():
    op.drop_index("ix_conversation_summary_conversation", table_name="conversation_summaries")
    op.drop_table("conversation_summaries")

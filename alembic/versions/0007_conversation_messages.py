"""add compact conversation memory"""
from alembic import op
import sqlalchemy as sa


revision = "0007_conversation_messages"
down_revision = "0006_event_recurrence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_conversation_messages_user_id",
        "conversation_messages",
        ["user_id"],
    )
    op.create_index(
        "ix_conversation_messages_created_at",
        "conversation_messages",
        ["created_at"],
    )


def downgrade():
    op.drop_table("conversation_messages")

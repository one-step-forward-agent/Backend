"""keep assistant replies for the chat history and cache recommendations per user"""

import sqlalchemy as sa
from alembic import op

revision = "0024_chat_history_cache"
down_revision = "0023_encrypt_personal_data"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("conversation_messages")}
    if "reply" not in columns:
        op.add_column("conversation_messages", sa.Column("reply", sa.Text(), nullable=True))
    if "draft_id" not in columns:
        op.add_column("conversation_messages", sa.Column("draft_id", sa.Integer(), nullable=True))
        op.create_index("ix_conversation_messages_draft_id", "conversation_messages", ["draft_id"])
    if "recommendation_cache" not in inspector.get_table_names():
        op.create_table(
            "recommendation_cache",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
            sa.Column("key", sa.String(64), nullable=False),
            sa.Column("items", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade():
    op.drop_table("recommendation_cache")
    op.drop_index("ix_conversation_messages_draft_id", table_name="conversation_messages")
    op.drop_column("conversation_messages", "draft_id")
    op.drop_column("conversation_messages", "reply")

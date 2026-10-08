"""tokens spent on language model requests, per user and purpose"""

import sqlalchemy as sa
from alembic import op

revision = "0028_llm_usage"
down_revision = "0027_draft_awaiting_null"
branch_labels = None
depends_on = None


def upgrade():
    if "llm_usage" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("precached_prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_llm_usage_user_id", "llm_usage", ["user_id"])
    op.create_index("ix_llm_usage_purpose", "llm_usage", ["purpose"])
    op.create_index("ix_llm_usage_created_at", "llm_usage", ["created_at"])


def downgrade():
    op.drop_table("llm_usage")

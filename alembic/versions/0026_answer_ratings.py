"""ratings of assistant answers"""

import sqlalchemy as sa
from alembic import op

revision = "0026_answer_ratings"
down_revision = "0025_tags_deadlines_evening"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("conversation_messages")}
    if "rating" not in columns:
        op.add_column("conversation_messages", sa.Column("rating", sa.SmallInteger(), nullable=True))
        op.create_index("ix_conversation_messages_rating", "conversation_messages", ["rating"])
    if "rated_at" not in columns:
        op.add_column("conversation_messages", sa.Column("rated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("conversation_messages", "rated_at")
    op.drop_index("ix_conversation_messages_rating", table_name="conversation_messages")
    op.drop_column("conversation_messages", "rating")

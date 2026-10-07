"""tags, deadlines and fixed tasks; evening summary and deadline notifications; recommendations per scope"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_tags_deadlines_evening"
down_revision = "0024_chat_history_cache"
branch_labels = None
depends_on = None


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "tags" not in inspector.get_table_names():
        op.create_table(
            "tags",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("color", sa.String(20), nullable=False, server_default="indigo"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_tags_user_id", "tags", ["user_id"])

    events = _columns(inspector, "events")
    if "tag_ids" not in events:
        op.add_column("events", sa.Column("tag_ids", postgresql.ARRAY(sa.Integer()), nullable=False, server_default="{}"))
    if "deadline_at" not in events:
        op.add_column("events", sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index("ix_events_deadline_at", "events", ["deadline_at"])
    if "is_fixed" not in events:
        op.add_column("events", sa.Column("is_fixed", sa.Boolean(), nullable=False, server_default=sa.false()))

    settings = _columns(inspector, "reminder_settings")
    if "evening_enabled" not in settings:
        op.add_column("reminder_settings", sa.Column("evening_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    if "evening_time" not in settings:
        op.add_column("reminder_settings", sa.Column("evening_time", sa.Time(), nullable=False, server_default=sa.text("'21:00'")))
    if "deadline_enabled" not in settings:
        op.add_column("reminder_settings", sa.Column("deadline_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))

    # One cached set of recommendations per screen: today, a calendar week or month
    if "scope" not in _columns(inspector, "recommendation_cache"):
        op.add_column("recommendation_cache", sa.Column("scope", sa.String(40), nullable=False, server_default="today"))
        for constraint in inspector.get_unique_constraints("recommendation_cache"):
            if constraint["column_names"] == ["user_id"]:
                op.drop_constraint(constraint["name"], "recommendation_cache", type_="unique")
        op.create_index("ix_recommendation_cache_user_id", "recommendation_cache", ["user_id"])
        op.create_unique_constraint("uq_recommendation_cache_user_scope", "recommendation_cache", ["user_id", "scope"])


def downgrade():
    op.drop_constraint("uq_recommendation_cache_user_scope", "recommendation_cache", type_="unique")
    op.drop_index("ix_recommendation_cache_user_id", table_name="recommendation_cache")
    op.execute("DELETE FROM recommendation_cache WHERE scope <> 'today'")
    op.drop_column("recommendation_cache", "scope")
    op.create_unique_constraint("recommendation_cache_user_id_key", "recommendation_cache", ["user_id"])
    op.drop_column("reminder_settings", "deadline_enabled")
    op.drop_column("reminder_settings", "evening_time")
    op.drop_column("reminder_settings", "evening_enabled")
    op.drop_column("events", "is_fixed")
    op.drop_index("ix_events_deadline_at", table_name="events")
    op.drop_column("events", "deadline_at")
    op.drop_column("events", "tag_ids")
    op.drop_index("ix_tags_user_id", table_name="tags")
    op.drop_table("tags")

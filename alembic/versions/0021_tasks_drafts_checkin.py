"""task completion, recurring series, assistant drafts, onboarding profile and midday check-in"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0021_tasks_drafts_checkin"
down_revision = "0020_refresh_tokens"
branch_labels = None
depends_on = None


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade():
    inspector = sa.inspect(op.get_bind())
    events = _columns(inspector, "events")
    # recurrence_rule may already exist from 0006_event_recurrence
    if "recurrence_rule" not in events:
        op.add_column("events", sa.Column("recurrence_rule", sa.Text(), nullable=True))
    if "series_id" not in events:
        op.add_column("events", sa.Column("series_id", sa.String(36), nullable=True))
        op.create_index("ix_events_series_id", "events", ["series_id"])
    if "completed_at" not in events:
        op.add_column("events", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    if "profile" not in _columns(inspector, "users"):
        op.add_column("users", sa.Column("profile", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    settings = _columns(inspector, "reminder_settings")
    if "checkin_enabled" not in settings:
        op.add_column("reminder_settings", sa.Column("checkin_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    if "checkin_time" not in settings:
        op.add_column("reminder_settings", sa.Column("checkin_time", sa.Time(), nullable=False, server_default=sa.text("'13:00'")))
    if "payload" not in _columns(inspector, "notifications"):
        op.add_column("notifications", sa.Column("payload", postgresql.JSONB(), nullable=True))
    if "assistant_drafts" not in inspector.get_table_names():
        op.create_table(
            "assistant_drafts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("items", postgresql.JSONB(), nullable=False),
            sa.Column("awaiting", postgresql.JSONB(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_assistant_drafts_user_id", "assistant_drafts", ["user_id"])


def downgrade():
    op.drop_index("ix_assistant_drafts_user_id", table_name="assistant_drafts")
    op.drop_table("assistant_drafts")
    op.drop_column("notifications", "payload")
    op.drop_column("reminder_settings", "checkin_time")
    op.drop_column("reminder_settings", "checkin_enabled")
    op.drop_column("users", "profile")
    op.drop_column("events", "completed_at")
    op.drop_index("ix_events_series_id", table_name="events")
    op.drop_column("events", "series_id")

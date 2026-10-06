"""add integration config, event links, Telegram linking and reminder settings"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0018_integrations_reminders"
down_revision = "0017_user_jwt_auth"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    event_columns = {column["name"] for column in inspector.get_columns("events")}
    integration_columns = {column["name"] for column in inspector.get_columns("integrations")}

    for name, column in {
        "timezone": sa.Column("timezone", sa.Text(), nullable=True),
        "telegram_chat_id": sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        "telegram_username": sa.Column("telegram_username", sa.String(64), nullable=True),
        "telegram_linked_at": sa.Column("telegram_linked_at", sa.DateTime(timezone=True), nullable=True),
        "telegram_link_code": sa.Column("telegram_link_code", sa.String(64), nullable=True),
        "telegram_link_expires_at": sa.Column("telegram_link_expires_at", sa.DateTime(timezone=True), nullable=True),
    }.items():
        if name not in user_columns:
            op.add_column("users", column)
    if "telegram_chat_id" not in user_columns:
        op.create_unique_constraint("uq_users_telegram_chat_id", "users", ["telegram_chat_id"])
    if "telegram_link_code" not in user_columns:
        op.create_unique_constraint("uq_users_telegram_link_code", "users", ["telegram_link_code"])

    if "all_day" not in event_columns:
        op.add_column("events", sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "reminder_minutes" not in event_columns:
        op.add_column("events", sa.Column("reminder_minutes", sa.Integer(), nullable=True))

    if "config" not in integration_columns:
        op.add_column(
            "integrations",
            sa.Column("config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        )

    if "event_links" not in tables:
        op.create_table(
            "event_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
            sa.Column("integration_id", sa.Integer(), sa.ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("external_id", sa.String(255), nullable=False),
            sa.Column("url", sa.String(1000), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("event_id", "integration_id", name="uq_event_links_event_integration"),
        )
        op.create_index("ix_event_links_event_id", "event_links", ["event_id"])
        op.create_index("ix_event_links_integration_id", "event_links", ["integration_id"])

    if "reminder_settings" not in tables:
        op.create_table(
            "reminder_settings",
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("lead_times", postgresql.ARRAY(sa.Integer()), nullable=False, server_default="{15}"),
            sa.Column("daily_digest_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("daily_digest_time", sa.Time(), nullable=False, server_default="09:00"),
            sa.Column("quiet_hours_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("quiet_hours_start", sa.Time(), nullable=False, server_default="23:00"),
            sa.Column("quiet_hours_end", sa.Time(), nullable=False, server_default="08:00"),
            sa.Column("sources", postgresql.ARRAY(sa.String(20)), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    if "notifications" not in tables:
        op.create_table(
            "notifications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=True),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("dedupe_key", sa.String(255), nullable=False, unique=True),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
        op.create_index("ix_notifications_event_id", "notifications", ["event_id"])
        op.create_index("ix_notifications_status", "notifications", ["status"])
        op.create_index("ix_notifications_scheduled_for", "notifications", ["scheduled_for"])


def downgrade():
    op.drop_table("notifications")
    op.drop_table("reminder_settings")
    op.drop_table("event_links")
    op.drop_column("integrations", "config")
    op.drop_column("events", "all_day")
    op.drop_constraint("uq_users_telegram_link_code", "users", type_="unique")
    op.drop_constraint("uq_users_telegram_chat_id", "users", type_="unique")
    for name in ("telegram_link_expires_at", "telegram_link_code", "telegram_linked_at", "telegram_username", "telegram_chat_id"):
        op.drop_column("users", name)

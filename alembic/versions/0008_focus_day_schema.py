"""add Focus Day calendars, event metadata and integrations"""

from alembic import op
import sqlalchemy as sa


revision = "0008_focus_day_schema"
down_revision = "0007_conversation_messages"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "calendars" not in tables:
        op.create_table(
            "calendars",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("provider", sa.String(20), nullable=False, server_default="local"),
            sa.Column("external_id", sa.String(255), unique=True),
            sa.Column("description", sa.Text()),
            sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        op.create_index("ix_calendars_user_id", "calendars", ["user_id"])
    if "email" not in {column["name"] for column in inspector.get_columns("users")}:
        op.add_column("users", sa.Column("email", sa.String(320), nullable=True))
        op.execute("UPDATE users SET email = CAST(tg_id AS VARCHAR(320)) || '@local.invalid' WHERE email IS NULL")
        op.alter_column("users", "email", nullable=False)
    for column, definition in (
        ("calendar_id", sa.Column("calendar_id", sa.Integer(), nullable=True)),
        ("end_at", sa.Column("end_at", sa.DateTime(timezone=True), nullable=True)),
        ("timezone", sa.Column("timezone", sa.String(64), nullable=True)),
        ("status", sa.Column("status", sa.String(20), nullable=True)),
        ("priority", sa.Column("priority", sa.String(20), nullable=True)),
        ("source", sa.Column("source", sa.String(20), nullable=True)),
        ("sync_status", sa.Column("sync_status", sa.String(20), nullable=True)),
        ("updated_at", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)),
    ):
        if column not in {item["name"] for item in inspector.get_columns("events")}:
            op.add_column("events", definition)
    if "event_metadata" not in tables:
        op.create_table(
            "event_metadata",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), unique=True, nullable=False),
            sa.Column("notes", sa.Text()),
            sa.Column("tags", sa.Text()),
            sa.Column("estimated_duration", sa.Integer()),
            sa.Column("actual_duration", sa.Integer()),
        )
    if "event_files" not in tables:
        op.create_table(
            "event_files",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
            sa.Column("original_filename", sa.String(255), nullable=False),
            sa.Column("stored_filename", sa.String(255), unique=True, nullable=False),
            sa.Column("mime_type", sa.String(100), nullable=False),
            sa.Column("file_size", sa.Integer(), nullable=False),
            sa.Column("storage_path", sa.String(1000), nullable=False),
        )
    if "integrations" not in tables:
        op.create_table(
            "integrations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("provider", sa.String(30), nullable=False),
            sa.Column("access_token", sa.Text(), nullable=False),
            sa.Column("refresh_token", sa.Text()),
            sa.Column("token_expires_at", sa.DateTime(timezone=True)),
        )


def downgrade():
    op.drop_table("integrations")
    op.drop_table("event_files")
    op.drop_table("event_metadata")
    for column in ("updated_at", "sync_status", "source", "priority", "status", "timezone", "end_at", "calendar_id"):
        op.drop_column("events", column)
    op.drop_column("users", "email")
    op.drop_index("ix_calendars_user_id", table_name="calendars")
    op.drop_table("calendars")

"""add calendar events"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0002_events"
down_revision = "ae123ae0bbc0"
branch_labels = None
depends_on = None


def upgrade():
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tg_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "events" not in tables:
        op.create_table(
            "events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ends_at", sa.DateTime(timezone=True)),
            sa.Column("location", sa.Text()),
            sa.Column("reminder_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_events_user_id", "events", ["user_id"])
        op.create_index("ix_events_starts_at", "events", ["starts_at"])


def downgrade():
    inspector = inspect(op.get_bind())
    if "events" in inspector.get_table_names():
        op.drop_table("events")

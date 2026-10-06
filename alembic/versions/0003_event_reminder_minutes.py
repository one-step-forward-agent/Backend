"""add per-event reminder interval"""
from alembic import op
import sqlalchemy as sa


revision = "0003_event_reminder_minutes"
down_revision = "0002_events"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "events",
        sa.Column("reminder_minutes", sa.Integer(), nullable=False, server_default="30"),
    )


def downgrade():
    op.drop_column("events", "reminder_minutes")

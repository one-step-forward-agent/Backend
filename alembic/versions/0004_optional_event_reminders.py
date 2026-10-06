"""make event reminders optional"""
from alembic import op


revision = "0004_optional_event_reminders"
down_revision = "0003_event_reminder_minutes"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("events", "reminder_minutes", nullable=True, server_default=None)
    op.execute("UPDATE events SET reminder_minutes = NULL")


def downgrade():
    op.execute("UPDATE events SET reminder_minutes = 30 WHERE reminder_minutes IS NULL")
    op.alter_column("events", "reminder_minutes", nullable=False, server_default="30")

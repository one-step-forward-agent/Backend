"""add event recurrence rules"""
from alembic import op
import sqlalchemy as sa


revision = "0006_event_recurrence"
down_revision = "0005_user_profile"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("recurrence_rule", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("events", "recurrence_rule")

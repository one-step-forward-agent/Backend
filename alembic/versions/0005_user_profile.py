"""add user name and timezone"""
from alembic import op
import sqlalchemy as sa


revision = "0005_user_profile"
down_revision = "0004_optional_event_reminders"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("name", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("timezone", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("users", "timezone")
    op.drop_column("users", "name")

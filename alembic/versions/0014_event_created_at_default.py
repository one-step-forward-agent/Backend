"""add a default for legacy event timestamps"""

from alembic import op
import sqlalchemy as sa


revision = "0014_event_created_at_default"
down_revision = "0013_optional_legacy_event_times"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "events",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.func.now(),
    )


def downgrade():
    op.alter_column(
        "events",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=None,
    )

"""The calendar the assistant adds tasks to: chosen on a draft, remembered for the user"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0033_draft_calendar_target"
down_revision = "0032_user_identities"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("calendar_target", sa.String(30), nullable=True))
    op.add_column("assistant_drafts", sa.Column("target", sa.String(30), nullable=True))
    op.add_column("assistant_drafts", sa.Column("targets", JSONB(), nullable=True))


def downgrade():
    op.drop_column("assistant_drafts", "targets")
    op.drop_column("assistant_drafts", "target")
    op.drop_column("users", "calendar_target")

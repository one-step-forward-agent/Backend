"""Several calendars for the assistant's new tasks: ticked on a draft, remembered for the user"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0034_draft_calendars"
down_revision = "0033_draft_calendar_target"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("calendar_targets", JSONB(), nullable=True))
    op.add_column("assistant_drafts", sa.Column("calendars", JSONB(), nullable=True))
    # One chosen calendar becomes a list of one; "dayla" (Dayla only) an empty list
    op.execute("UPDATE users SET calendar_targets = CASE WHEN calendar_target = 'dayla' THEN '[]'::jsonb ELSE jsonb_build_array(calendar_target) END WHERE calendar_target IS NOT NULL")
    op.execute("UPDATE assistant_drafts SET calendars = CASE WHEN target = 'dayla' THEN '[]'::jsonb ELSE jsonb_build_array(target) END WHERE target IS NOT NULL")
    # The offered choices no longer include "Dayla only": tasks are always in Dayla
    op.execute(
        "UPDATE assistant_drafts SET targets = COALESCE((SELECT jsonb_agg(item) FROM jsonb_array_elements(targets) item WHERE item->>'slug' <> 'dayla'), '[]'::jsonb) "
        "WHERE targets IS NOT NULL"
    )
    op.drop_column("users", "calendar_target")
    op.drop_column("assistant_drafts", "target")


def downgrade():
    op.add_column("users", sa.Column("calendar_target", sa.String(30), nullable=True))
    op.add_column("assistant_drafts", sa.Column("target", sa.String(30), nullable=True))
    op.execute("UPDATE users SET calendar_target = COALESCE(calendar_targets->>0, 'dayla') WHERE calendar_targets IS NOT NULL")
    op.execute("UPDATE assistant_drafts SET target = COALESCE(calendars->>0, 'dayla') WHERE calendars IS NOT NULL")
    op.drop_column("assistant_drafts", "calendars")
    op.drop_column("users", "calendar_targets")

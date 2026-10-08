"""Notion and Jira connections created empty stub calendars (external_id = workspace or cloud id) that sync never
used — it imports into the "primary" calendar of the integration"""

from alembic import op

revision = "0030_drop_stub_calendars"
down_revision = "0029_yandex_calendar_primary"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "DELETE FROM calendars c WHERE c.provider IN ('notion', 'jira') AND c.external_id IS DISTINCT FROM 'primary' "
        "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.calendar_id = c.id)"
    )


def downgrade():
    pass

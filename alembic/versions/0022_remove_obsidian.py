"""remove the Obsidian integration; data imported from it stays as local calendar data"""

from alembic import op


revision = "0022_remove_obsidian"
down_revision = "0021_tasks_drafts_checkin"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE calendars SET integration_id = NULL, provider = 'local', external_id = NULL "
        "WHERE provider = 'obsidian' OR integration_id IN (SELECT id FROM integrations WHERE provider = 'obsidian')"
    )
    op.execute("UPDATE events SET source = 'local' WHERE source = 'obsidian'")
    op.execute("DELETE FROM event_links WHERE integration_id IN (SELECT id FROM integrations WHERE provider = 'obsidian')")
    op.execute("DELETE FROM integrations WHERE provider = 'obsidian'")
    op.execute("UPDATE reminder_settings SET sources = array_remove(sources, 'obsidian') WHERE 'obsidian' = ANY(sources)")


def downgrade():
    pass

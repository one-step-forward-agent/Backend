"""Yandex calendars created on connection got external_id "placeholder"; sync looks for "primary\""""

from alembic import op

revision = "0029_yandex_calendar_primary"
down_revision = "0028_llm_usage"
branch_labels = None
depends_on = None


def upgrade():
    # Where a user already has a "primary" Yandex calendar, the empty placeholder is dropped; otherwise it becomes primary
    op.execute(
        "DELETE FROM calendars c WHERE c.provider = 'yandex' AND c.external_id = 'placeholder' "
        "AND EXISTS (SELECT 1 FROM calendars p WHERE p.user_id = c.user_id AND p.provider = 'yandex' AND p.external_id = 'primary') "
        "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.calendar_id = c.id)"
    )
    op.execute("UPDATE calendars SET external_id = 'primary', name = 'Яндекс Календарь' WHERE provider = 'yandex' AND external_id = 'placeholder'")


def downgrade():
    pass

"""store "not waiting for an edit" as SQL NULL: JSON null made every draft capture the next message"""

from alembic import op

revision = "0027_draft_awaiting_null"
down_revision = "0026_answer_ratings"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE assistant_drafts SET awaiting = NULL WHERE awaiting IS NOT NULL AND jsonb_typeof(awaiting) <> 'object'")


def downgrade():
    pass

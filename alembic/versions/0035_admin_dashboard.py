"""Admin dashboard: the users.is_admin flag in the model, and the admins' shared note with its lock"""

import sqlalchemy as sa
from alembic import op

revision = "0035_admin_dashboard"
down_revision = "0034_draft_calendars"
branch_labels = None
depends_on = None


def upgrade():
    # 0002 created the column; databases that started from the legacy baseline may not have it
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin boolean NOT NULL DEFAULT false")
    op.create_table(
        "admin_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("locked_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_table("admin_notes")

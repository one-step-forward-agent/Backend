"""make the legacy Telegram identifier optional for web users"""

from alembic import op
import sqlalchemy as sa


revision = "0010_legacy_user_compatibility"
down_revision = "0009_user_timestamps"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("users", "tg_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade():
    op.alter_column("users", "tg_id", existing_type=sa.BigInteger(), nullable=False)

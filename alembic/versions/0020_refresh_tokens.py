"""store issued refresh tokens so they can be rotated and revoked"""

from alembic import op
import sqlalchemy as sa


revision = "0020_refresh_tokens"
down_revision = "0019_encrypt_google_tokens"
branch_labels = None
depends_on = None


def upgrade():
    if "refresh_tokens" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "refresh_tokens",
        sa.Column("jti", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])


def downgrade():
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")

"""add first-party JWT authentication fields"""

from alembic import op
import sqlalchemy as sa


revision = "0017_user_jwt_auth"
down_revision = "0016_calendar_external_ids"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "password_hash" not in columns:
        op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=True))
    if "is_active" not in columns:
        op.add_column("users", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "is_active" in columns:
        op.drop_column("users", "is_active")
    if "password_hash" in columns:
        op.drop_column("users", "password_hash")

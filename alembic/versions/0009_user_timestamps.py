"""add timestamps required by the user model"""

from alembic import op
import sqlalchemy as sa


revision = "0009_user_timestamps"
down_revision = "0008_focus_day_schema"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "created_at" not in columns:
        op.add_column(
            "users",
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    if "updated_at" not in columns:
        op.add_column(
            "users",
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "updated_at" in columns:
        op.drop_column("users", "updated_at")
    if "created_at" in columns:
        op.drop_column("users", "created_at")

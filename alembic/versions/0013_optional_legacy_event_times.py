"""make legacy event time columns optional"""

from alembic import op
import sqlalchemy as sa


revision = "0013_optional_legacy_event_times"
down_revision = "0012_event_external_id"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"]: column for column in sa.inspect(op.get_bind()).get_columns("events")}
    if "starts_at" in columns:
        op.alter_column("events", "starts_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    if "ends_at" in columns:
        op.alter_column("events", "ends_at", existing_type=sa.DateTime(timezone=True), nullable=True)


def downgrade():
    columns = {column["name"]: column for column in sa.inspect(op.get_bind()).get_columns("events")}
    if "starts_at" in columns:
        op.alter_column("events", "starts_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    if "ends_at" in columns:
        op.alter_column("events", "ends_at", existing_type=sa.DateTime(timezone=True), nullable=True)

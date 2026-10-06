"""add external provider identifier to events"""

from alembic import op
import sqlalchemy as sa


revision = "0012_event_external_id"
down_revision = "0011_event_time_columns"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("events")}
    if "external_id" not in columns:
        op.add_column("events", sa.Column("external_id", sa.String(255), nullable=True))
        op.create_unique_constraint("uq_events_external_id", "events", ["external_id"])


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("events")}
    if "external_id" in columns:
        op.drop_constraint("uq_events_external_id", "events", type_="unique")
        op.drop_column("events", "external_id")

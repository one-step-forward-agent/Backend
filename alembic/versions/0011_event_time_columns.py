"""align legacy event time columns with the web API"""

from alembic import op
import sqlalchemy as sa


revision = "0011_event_time_columns"
down_revision = "0010_legacy_user_compatibility"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("events")}
    if "start_at" not in columns:
        op.add_column("events", sa.Column("start_at", sa.DateTime(timezone=True), nullable=True))
        if "starts_at" in columns:
            op.execute("UPDATE events SET start_at = starts_at WHERE start_at IS NULL")
    if "end_at" not in columns:
        op.add_column("events", sa.Column("end_at", sa.DateTime(timezone=True), nullable=True))
        if "ends_at" in columns:
            op.execute("UPDATE events SET end_at = ends_at WHERE end_at IS NULL")


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("events")}
    if "end_at" in columns:
        op.drop_column("events", "end_at")
    if "start_at" in columns:
        op.drop_column("events", "start_at")

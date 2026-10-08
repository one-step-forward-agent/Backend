"""events.external_id unique per user, not globally: a Google meeting two Dayla users are invited to has the same
event id in both calendars, and the second user's copy was never imported"""

import sqlalchemy as sa
from alembic import op

revision = "0031_event_external_id_per_user"
down_revision = "0030_drop_stub_calendars"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("events"):
        if constraint["column_names"] == ["external_id"]:
            op.drop_constraint(constraint["name"], "events", type_="unique")
    if "uq_events_user_external" not in {item["name"] for item in inspector.get_unique_constraints("events")}:
        op.create_unique_constraint("uq_events_user_external", "events", ["user_id", "external_id"])


def downgrade():
    op.drop_constraint("uq_events_user_external", "events", type_="unique")
    op.create_unique_constraint("uq_events_external_id", "events", ["external_id"])

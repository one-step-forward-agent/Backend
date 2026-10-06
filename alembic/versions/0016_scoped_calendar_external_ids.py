"""scope external calendar ids to their provider integration"""

from alembic import op
import sqlalchemy as sa


revision = "0016_calendar_external_ids"
down_revision = "0015_provider_integrations"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    constraints = {item["name"] for item in inspector.get_unique_constraints("calendars")}
    if "calendars_external_id_key" in constraints:
        op.drop_constraint("calendars_external_id_key", "calendars", type_="unique")
    if "uq_calendars_integration_external" not in constraints:
        op.create_unique_constraint(
            "uq_calendars_integration_external",
            "calendars",
            ["integration_id", "external_id"],
        )


def downgrade():
    op.drop_constraint("uq_calendars_integration_external", "calendars", type_="unique")
    op.create_unique_constraint("calendars_external_id_key", "calendars", ["external_id"])

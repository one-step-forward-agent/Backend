"""link calendars to provider integrations and prepare multi-provider accounts"""

from alembic import op
import sqlalchemy as sa


revision = "0015_provider_integrations"
down_revision = "0014_event_created_at_default"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    user_columns = {column["name"] for column in sa.inspect(bind).get_columns("integrations")}
    calendar_columns = {column["name"] for column in sa.inspect(bind).get_columns("calendars")}

    additions = {
        "account_email": sa.Column("account_email", sa.String(320), nullable=True),
        "credentials_encrypted": sa.Column("credentials_encrypted", sa.Text(), nullable=True),
        "status": sa.Column("status", sa.String(20), nullable=False, server_default="connected"),
        "last_sync_at": sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        "last_sync_error": sa.Column("last_sync_error", sa.Text(), nullable=True),
    }
    for name, column in additions.items():
        if name not in user_columns:
            op.add_column("integrations", column)
    if "access_token" in user_columns:
        op.alter_column("integrations", "access_token", existing_type=sa.Text(), nullable=True)

    if "integration_id" not in calendar_columns:
        op.add_column("calendars", sa.Column("integration_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_calendars_integration_id",
            "calendars",
            "integrations",
            ["integration_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index("ix_calendars_integration_id", "calendars", ["integration_id"])
        op.execute(
            "UPDATE calendars AS c SET integration_id = i.id "
            "FROM integrations AS i "
            "WHERE c.provider = 'google' AND i.provider = 'google' AND i.user_id = c.user_id"
        )


def downgrade():
    op.drop_index("ix_calendars_integration_id", table_name="calendars")
    op.drop_constraint("fk_calendars_integration_id", "calendars", type_="foreignkey")
    op.drop_column("calendars", "integration_id")
    for name in ("last_sync_error", "last_sync_at", "status", "credentials_encrypted", "account_email"):
        op.drop_column("integrations", name)
    op.alter_column("integrations", "access_token", existing_type=sa.Text(), nullable=False)

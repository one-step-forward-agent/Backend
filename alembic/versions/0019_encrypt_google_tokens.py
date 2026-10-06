"""move Google OAuth tokens from plaintext columns into credentials_encrypted"""

from alembic import op
import sqlalchemy as sa

from app.core.crypto import decrypt_json, encrypt_json


revision = "0019_encrypt_google_tokens"
down_revision = "0018_integrations_reminders"
branch_labels = None
depends_on = None

TOKEN_COLUMNS = ("access_token", "refresh_token")


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("integrations")}
    present = [name for name in TOKEN_COLUMNS if name in columns]
    if not present:
        return
    integrations = sa.table("integrations", sa.column("id"), sa.column("credentials_encrypted"), *(sa.column(name) for name in present))
    for row in bind.execute(sa.select(integrations)).mappings().all():
        tokens = {name: row[name] for name in present if row[name]}
        if tokens:
            secrets = {**decrypt_json(row["credentials_encrypted"]), **tokens}
            bind.execute(integrations.update().where(integrations.c.id == row["id"]).values(credentials_encrypted=encrypt_json(secrets)))
    for name in present:
        op.drop_column("integrations", name)


def downgrade():
    bind = op.get_bind()
    for name in TOKEN_COLUMNS:
        op.add_column("integrations", sa.Column(name, sa.Text(), nullable=True))
    integrations = sa.table("integrations", sa.column("id"), sa.column("provider"), sa.column("credentials_encrypted"), *(sa.column(name) for name in TOKEN_COLUMNS))
    for row in bind.execute(sa.select(integrations).where(integrations.c.provider == "google")).mappings().all():
        secrets = decrypt_json(row["credentials_encrypted"])
        bind.execute(
            integrations.update()
            .where(integrations.c.id == row["id"])
            .values(**{name: secrets.get(name) for name in TOKEN_COLUMNS})
        )

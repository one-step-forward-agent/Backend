"""Encrypt what was still plain: reminders' payload (a custom reminder's own text), delivery errors, the
logins and addresses in integrations.config and their sync errors"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.dataenc import decrypt, encrypt, is_encrypted

revision = "0036_encrypt_more"
down_revision = "0035_admin_dashboard"
branch_labels = None
depends_on = None

BATCH = 500
COLUMNS = (
    ("notifications", "payload", "json"),
    ("notifications", "error", "text"),
    ("integrations", "config", "json"),
    ("integrations", "last_sync_error", "text"),
)


def _rewrite(table: str, column: str, transform) -> None:
    bind = op.get_bind()
    last_id = 0
    while True:
        rows = bind.execute(
            sa.text(f"SELECT id, {column} FROM {table} WHERE id > :last AND {column} IS NOT NULL ORDER BY id LIMIT {BATCH}"),  # nosec B608 - names from COLUMNS
            {"last": last_id},
        ).all()
        if not rows:
            return
        for row_id, value in rows:
            new_value = transform(value)
            if new_value != value:
                bind.execute(sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :id"), {"value": new_value, "id": row_id})  # nosec B608 - names from COLUMNS
        last_id = rows[-1][0]


def upgrade():
    for table, column, kind in COLUMNS:
        if kind == "json":
            op.alter_column(table, column, type_=sa.Text(), postgresql_using=f"{column}::text", server_default=None)

    for table, column, kind in COLUMNS:
        context = f"{table}.{column}"

        def seal(value, context=context, kind=kind):
            if is_encrypted(value):
                return value
            if kind == "json":
                value = json.dumps(json.loads(value), ensure_ascii=False)
            return encrypt(value, context)

        _rewrite(table, column, seal)


def downgrade():
    for table, column, _kind in COLUMNS:
        _rewrite(table, column, lambda value, context=f"{table}.{column}": decrypt(value, context))
    for table, column, kind in COLUMNS:
        if kind == "json":
            op.alter_column(table, column, type_=postgresql.JSONB(), postgresql_using=f"{column}::jsonb")

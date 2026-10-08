"""encrypt personal data at rest (app/core/dataenc.py) and look users up by an email hash"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.dataenc import decrypt, email_index, encrypt, is_encrypted

revision = "0023_encrypt_personal_data"
down_revision = "0022_remove_obsidian"
branch_labels = None
depends_on = None

BATCH = 500
# table, column, kind; the context string is "table.column" (see the models)
COLUMNS = (
    ("users", "email", "text"),
    ("users", "name", "text"),
    ("users", "telegram_username", "text"),
    ("users", "profile", "json"),
    ("calendars", "name", "text"),
    ("calendars", "description", "text"),
    ("events", "title", "text"),
    ("events", "description", "text"),
    ("events", "location", "text"),
    ("event_metadata", "notes", "text"),
    ("event_metadata", "tags", "text"),
    ("event_files", "original_filename", "text"),
    ("integrations", "account_email", "text"),
    ("notifications", "text", "text"),
    ("conversation_messages", "content", "text"),
    ("assistant_drafts", "items", "json"),
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
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "email_hash" not in {column["name"] for column in inspector.get_columns("users")}:
        op.add_column("users", sa.Column("email_hash", sa.String(64), nullable=True))
    _hash_emails()
    op.alter_column("users", "email_hash", nullable=False)
    for constraint in inspector.get_unique_constraints("users"):
        if constraint["column_names"] == ["email"]:
            op.drop_constraint(constraint["name"], "users", type_="unique")
    for index in inspector.get_indexes("users"):
        if index["column_names"] == ["email"]:
            op.drop_index(index["name"], table_name="users")
    op.create_index("ix_users_email_hash", "users", ["email_hash"], unique=True)

    # Ciphertext is longer than the original limits, and JSON is stored as encrypted text
    for table, column, kind in COLUMNS:
        if kind == "json":
            op.alter_column(table, column, type_=sa.Text(), postgresql_using=f"{column}::text", server_default=None)
        else:
            op.alter_column(table, column, type_=sa.Text())
    op.alter_column("users", "profile", server_default=sa.text("'{}'"))

    for table, column, kind in COLUMNS:
        context = f"{table}.{column}"

        def seal(value, context=context, kind=kind):
            if is_encrypted(value):
                return value
            if kind == "json":
                value = json.dumps(json.loads(value), ensure_ascii=False)
            return encrypt(value, context)

        _rewrite(table, column, seal)


def _hash_emails():
    bind = op.get_bind()
    for row_id, email in bind.execute(sa.text("SELECT id, email FROM users")).all():
        plain = decrypt(email, "users.email")
        bind.execute(sa.text("UPDATE users SET email_hash = :hash WHERE id = :id"), {"hash": email_index(plain), "id": row_id})


def downgrade():
    for table, column, kind in COLUMNS:
        _rewrite(table, column, lambda value, context=f"{table}.{column}": decrypt(value, context))
    op.alter_column("users", "profile", server_default=None)
    op.alter_column("users", "profile", type_=postgresql.JSONB(), postgresql_using="profile::jsonb", server_default=sa.text("'{}'::jsonb"))
    op.alter_column("assistant_drafts", "items", type_=postgresql.JSONB(), postgresql_using="items::jsonb")
    op.drop_index("ix_users_email_hash", table_name="users")
    op.drop_column("users", "email_hash")
    op.create_index("ix_users_email", "users", ["email"], unique=True)

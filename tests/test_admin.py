"""The admin dashboard: only admins, every table without secrets or Google's content, and the shared note
that one admin at a time edits after locking it."""

import uuid

from app.core import make_admin
from app.api.admin import GOOGLE_HIDDEN
from tests.test_integrations import oauth_connect, services  # noqa: F401 - services is a fixture


async def account(client, name: str, admin: bool) -> tuple[dict, str]:
    email = f"{name}-{uuid.uuid4().hex[:8]}@example.com"
    registered = await client.post("/auth/register", json={"email": email, "password": "password-123", "name": name, "timezone": "Europe/Moscow"})
    assert registered.status_code == 201, registered.text
    if admin:
        assert await make_admin.main(email, True) == 0
    return {"Authorization": f"Bearer {registered.json()['access_token']}"}, email


async def test_only_admins_open_the_dashboard(client):
    headers, _ = await account(client, "user", admin=False)
    assert (await client.get("/api/me", headers=headers)).json()["is_admin"] is False
    for path in ("/api/admin/tables", "/api/admin/tables/users", "/api/admin/notes"):
        response = await client.get(path, headers=headers)
        assert response.status_code == 403 and response.json()["detail"] == "Раздел только для администраторов"
    client.cookies.clear()
    assert (await client.get("/api/admin/tables")).status_code == 401


async def test_tables_without_secrets_or_google_content(client, services):
    headers, email = await account(client, "admin", admin=True)
    assert (await client.get("/api/me", headers=headers)).json()["is_admin"] is True
    # Created before Google is connected, the task stays Dayla's own; with Google it would go there too
    await client.post("/api/events", json={"title": "Купить хлеб", "start_at": "2030-01-01T10:00:00+03:00", "end_at": "2030-01-01T11:00:00+03:00"}, headers=headers)
    await oauth_connect(client, headers, "google")

    tables = {table["name"]: table for table in (await client.get("/api/admin/tables", headers=headers)).json()}
    assert "admin_notes" not in tables and {"users", "events", "integrations", "llm_usage"} <= set(tables)
    users = {column["name"]: column["type"] for column in tables["users"]["columns"]}
    assert users["id"] == "number" and users["created_at"] == "datetime" and users["profile"] == "json"
    assert not {"password_hash", "email_hash", "telegram_link_code"} & set(users)
    assert "credentials_encrypted" not in {column["name"] for column in tables["integrations"]["columns"]}
    assert {"column": "user_id", "table": "users", "target": "id"} in tables["events"]["foreign_keys"]

    sheet = (await client.get("/api/admin/tables/users", headers=headers)).json()
    names = [column["name"] for column in sheet["columns"]]
    assert email in [row[names.index("email")] for row in sheet["data"]] and sheet["truncated"] is False

    events = (await client.get("/api/admin/tables/events", headers=headers)).json()
    names = [column["name"] for column in events["columns"]]
    me = (await client.get("/api/me", headers=headers)).json()["id"]
    mine = {row[names.index("title")]: row[names.index("source")] for row in events["data"] if row[names.index("user_id")] == me}
    # The event imported from Google keeps its times and ids; its title, description and place are not shown
    assert mine == {GOOGLE_HIDDEN: "google", "Купить хлеб": "local"}
    assert (await client.get("/api/admin/tables/admin_notes", headers=headers)).status_code == 404


async def test_note_is_edited_by_one_admin_at_a_time(client):
    olga, _ = await account(client, "Ольга", admin=True)
    ivan, _ = await account(client, "Иван", admin=True)
    note = (await client.get("/api/admin/notes", headers=olga)).json()
    # Saving without the lock is refused
    refused = await client.put("/api/admin/notes", json={"text": "план", "version": note["version"]}, headers=olga)
    assert refused.status_code == 409 and "Заблокировать" in refused.json()["detail"]

    locked = (await client.post("/api/admin/notes/lock", headers=olga)).json()
    assert locked["locked_by_me"] and locked["locked_by"] == "Ольга"
    seen = (await client.get("/api/admin/notes", headers=ivan)).json()
    assert seen["locked"] and not seen["locked_by_me"] and seen["locked_by"] == "Ольга"
    taken = await client.post("/api/admin/notes/lock", headers=ivan)
    assert taken.status_code == 409 and taken.json()["detail"] == "Заметку сейчас редактирует «Ольга»"
    assert (await client.put("/api/admin/notes", json={"text": "чужое", "version": note["version"]}, headers=ivan)).status_code == 409

    saved = (await client.put("/api/admin/notes", json={"text": "Созвон в пятницу", "version": note["version"]}, headers=olga)).json()
    assert saved["text"] == "Созвон в пятницу" and saved["version"] == note["version"] + 1
    assert saved["updated_by"] == "Ольга" and not saved["locked"]

    # Saved, the note is free: Ivan takes it, and a save over an older version is refused
    assert (await client.post("/api/admin/notes/lock", headers=ivan)).status_code == 200
    stale = await client.put("/api/admin/notes", json={"text": "старое", "version": note["version"]}, headers=ivan)
    assert stale.status_code == 409 and "уже изменили" in stale.json()["detail"]
    released = (await client.post("/api/admin/notes/unlock", headers=ivan)).json()
    assert not released["locked"] and released["text"] == "Созвон в пятницу"


async def test_admin_emails_make_existing_accounts_admins(client):
    headers, email = await account(client, "olga", admin=False)
    # Upper case and an address with no account yet: the account still gets it, the missing one is skipped
    await make_admin.grant_listed((email.upper(), f"nobody-{uuid.uuid4().hex[:8]}@example.com"))
    assert (await client.get("/api/me", headers=headers)).json()["is_admin"] is True
    assert (await client.get("/api/admin/tables", headers=headers)).status_code == 200

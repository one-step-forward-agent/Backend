# Backend tests

`test_dates.py` needs no database. The other tests need PostgreSQL migrated to head:

```bash
docker run -d --name dayla-test-db -e POSTGRES_PASSWORD=test -e POSTGRES_DB=focus_day -p 55432:5432 postgres:16-alpine
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql+asyncpg://postgres:test@127.0.0.1:55432/focus_day
alembic upgrade head
pytest
```

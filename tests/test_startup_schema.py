"""Al arrancar, la API aplica schema.sql (idempotente) para no depender de pasos manuales."""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://unused")

from pathlib import Path

from fastapi.testclient import TestClient

import main


class FakePool:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append(sql)

    async def close(self):
        pass


def test_startup_applies_schema(monkeypatch):
    pool = FakePool()

    async def fake_create_pool(*args, **kwargs):
        return pool

    monkeypatch.setattr(main.asyncpg, "create_pool", fake_create_pool)

    with TestClient(main.app):
        pass

    schema = (Path(main.__file__).parent / "schema.sql").read_text()
    assert pool.executed == [schema]
    assert "CREATE TABLE IF NOT EXISTS discovery_reports" in schema

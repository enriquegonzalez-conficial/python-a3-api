"""Los endpoints de ingesta solo aceptan agentes con id y secreto válidos."""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://unused")

import pytest
from fastapi.testclient import TestClient

import main


class _Conn:
    def __init__(self, calls):
        self.calls = calls

    async def execute(self, sql, *args):
        self.calls.append(args)

    async def executemany(self, sql, rows):
        self.calls.append(rows)


class _Acquire:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return _Conn(self.calls)

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self):
        self.calls = []

    def acquire(self):
        return _Acquire(self.calls)


@pytest.fixture
def pool(monkeypatch):
    fake = FakePool()
    monkeypatch.setattr(main, "_pool", fake)
    monkeypatch.setenv("AGENT_CREDENTIALS", "agente-1:secreto-1,agente-2:secreto-2")
    return fake


# TestClient sin "with" no ejecuta el lifespan, así que no abre conexión real.
client = TestClient(main.app)

ENDPOINTS = [
    ("/ingest/empresas", [{"id": 1, "codigo": "001", "nombre": "Empresa"}]),
    ("/diagnostic", {"machineName": "SRV"}),
    ("/discovery", {"diagnostic": {"machineName": "SRV"}, "sources": []}),
]


@pytest.mark.parametrize("path,body", ENDPOINTS)
def test_accepts_valid_agent(pool, path, body):
    r = client.post(path, json=body, headers={"X-Agent-Id": "agente-2", "X-Agent-Secret": "secreto-2"})

    assert r.status_code == 200
    assert len(pool.calls) == 1


@pytest.mark.parametrize("path,body", ENDPOINTS)
@pytest.mark.parametrize("headers", [
    {},
    {"X-Agent-Id": "agente-1"},
    {"X-Agent-Id": "agente-1", "X-Agent-Secret": "secreto-2"},
    {"X-Agent-Id": "intruso", "X-Agent-Secret": "secreto-1"},
])
def test_rejects_missing_or_wrong_credentials(pool, path, body, headers):
    r = client.post(path, json=body, headers=headers)

    assert r.status_code == 401
    assert pool.calls == []


def test_rejects_everyone_when_no_credentials_configured(pool, monkeypatch):
    monkeypatch.delenv("AGENT_CREDENTIALS")

    r = client.post("/diagnostic", json={}, headers={"X-Agent-Id": "agente-1", "X-Agent-Secret": "secreto-1"})

    assert r.status_code == 401

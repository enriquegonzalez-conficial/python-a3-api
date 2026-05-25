from __future__ import annotations

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import asyncpg
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

_pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=5)
    logger.info("Database pool ready")
    yield
    await _pool.close()
    logger.info("Database pool closed")


# --- Auth ---

security = HTTPBasic()


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(
        credentials.username, os.environ.get("DASHBOARD_USER", "")
    )
    ok_pass = secrets.compare_digest(
        credentials.password, os.environ.get("DASHBOARD_PASSWORD", "")
    )
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


# --- Shared base ---

class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# --- Request models ---

class Empresa(CamelModel):
    id: int
    codigo: str
    nombre: str


class Asiento(CamelModel):
    id: int
    codigo_empresa: str
    fecha: date
    descripcion: str
    importe: Decimal


class Nomina(CamelModel):
    id: int
    codigo_empresa: str
    codigo_empleado: str
    nombre_empleado: str
    periodo: str
    importe_neto: Decimal


# --- Response models ---

class IngestResponse(BaseModel):
    status: str
    received: int


class DiagnosticResponse(BaseModel):
    status: str


# --- Helpers ---

def _serialize(row: dict) -> dict:
    result = {}
    for key, val in row.items():
        if isinstance(val, Decimal):
            result[key] = float(val)
        elif isinstance(val, (date, datetime)):
            result[key] = val.isoformat()
        else:
            result[key] = val
    return result


# --- App ---

app = FastAPI(title="A3 API", version="0.3.0", lifespan=lifespan)


# ── Dashboard ────────────────────────────────────────────────────────────────

@app.get("/", dependencies=[Depends(verify_credentials)], include_in_schema=False)
async def dashboard():
    response = FileResponse(STATIC_DIR / "dashboard.html")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'"
    )
    return response


@app.get("/api/empresas", dependencies=[Depends(verify_credentials)])
async def get_empresas(limit: int = 200):
    rows = await _pool.fetch(
        "SELECT * FROM empresas ORDER BY received_at DESC LIMIT $1", limit
    )
    return [_serialize(dict(r)) for r in rows]


@app.get("/api/asientos", dependencies=[Depends(verify_credentials)])
async def get_asientos(limit: int = 200):
    rows = await _pool.fetch(
        "SELECT * FROM asientos ORDER BY received_at DESC LIMIT $1", limit
    )
    return [_serialize(dict(r)) for r in rows]


@app.get("/api/nominas", dependencies=[Depends(verify_credentials)])
async def get_nominas(limit: int = 200):
    rows = await _pool.fetch(
        "SELECT * FROM nominas ORDER BY received_at DESC LIMIT $1", limit
    )
    return [_serialize(dict(r)) for r in rows]


@app.get("/api/diagnostic", dependencies=[Depends(verify_credentials)])
async def get_diagnostic(limit: int = 50):
    rows = await _pool.fetch(
        "SELECT * FROM diagnostic_reports ORDER BY received_at DESC LIMIT $1", limit
    )
    return [_serialize(dict(r)) for r in rows]


# ── Ingest (sin dashboard auth — usan X-Agent-Id) ───────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/ingest/empresas", response_model=IngestResponse)
async def ingest_empresas(
    empresas: list[Empresa],
    x_agent_id: Optional[str] = Header(default=None),
) -> IngestResponse:
    logger.info("Agent=%s | %d empresas", x_agent_id, len(empresas))
    async with _pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO empresas (id, agent_id, codigo, nombre)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id, agent_id) DO UPDATE
                SET codigo = EXCLUDED.codigo, nombre = EXCLUDED.nombre
            """,
            [(e.id, x_agent_id, e.codigo, e.nombre) for e in empresas],
        )
    return IngestResponse(status="ok", received=len(empresas))


@app.post("/ingest/asientos", response_model=IngestResponse)
async def ingest_asientos(
    asientos: list[Asiento],
    x_agent_id: Optional[str] = Header(default=None),
) -> IngestResponse:
    logger.info("Agent=%s | %d asientos", x_agent_id, len(asientos))
    async with _pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO asientos (id, agent_id, codigo_empresa, fecha, descripcion, importe)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (id, agent_id) DO UPDATE
                SET codigo_empresa = EXCLUDED.codigo_empresa,
                    fecha          = EXCLUDED.fecha,
                    descripcion    = EXCLUDED.descripcion,
                    importe        = EXCLUDED.importe
            """,
            [(a.id, x_agent_id, a.codigo_empresa, a.fecha, a.descripcion, a.importe)
             for a in asientos],
        )
    return IngestResponse(status="ok", received=len(asientos))


@app.post("/ingest/nominas", response_model=IngestResponse)
async def ingest_nominas(
    nominas: list[Nomina],
    x_agent_id: Optional[str] = Header(default=None),
) -> IngestResponse:
    logger.info("Agent=%s | %d nominas", x_agent_id, len(nominas))
    async with _pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO nominas
                (id, agent_id, codigo_empresa, codigo_empleado, nombre_empleado, periodo, importe_neto)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id, agent_id) DO UPDATE
                SET codigo_empresa  = EXCLUDED.codigo_empresa,
                    codigo_empleado = EXCLUDED.codigo_empleado,
                    nombre_empleado = EXCLUDED.nombre_empleado,
                    periodo         = EXCLUDED.periodo,
                    importe_neto    = EXCLUDED.importe_neto
            """,
            [(n.id, x_agent_id, n.codigo_empresa, n.codigo_empleado,
              n.nombre_empleado, n.periodo, n.importe_neto)
             for n in nominas],
        )
    return IngestResponse(status="ok", received=len(nominas))


@app.post("/diagnostic", response_model=DiagnosticResponse)
async def diagnostic(
    report: dict[str, Any],
    x_agent_id: Optional[str] = Header(default=None),
) -> DiagnosticResponse:
    logger.info("Agent=%s | Diagnostic from %s", x_agent_id, report.get("machineName"))

    raw_ts = report.get("timestamp")
    ts: datetime | None = None
    if raw_ts:
        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            logger.warning("Agent=%s | Could not parse timestamp: %r", x_agent_id, raw_ts)

    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO diagnostic_reports
                (agent_id, machine_name, os_version, agent_timestamp,
                 executables, scheduled_tasks, com_prog_ids, odbc_dsns, install_paths)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            x_agent_id,
            report.get("machineName"),
            report.get("osVersion"),
            ts,
            json.dumps(report.get("executables", [])),
            json.dumps(report.get("scheduledTasks", [])),
            json.dumps(report.get("comProgIds", [])),
            json.dumps(report.get("odbcDsns", [])),
            json.dumps(report.get("installPaths", [])),
        )
    return DiagnosticResponse(status="ok")

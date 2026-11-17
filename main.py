from __future__ import annotations

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, List, Literal, Optional

import asyncpg
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SCHEMA_FILE = Path(__file__).parent / "schema.sql"

_pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=5)
    # schema.sql solo usa CREATE ... IF NOT EXISTS: aplicarlo en cada arranque
    # crea lo que falte sin tocar lo que ya existe.
    await _pool.execute(SCHEMA_FILE.read_text())
    logger.info("Database pool ready, schema applied")
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


def _agent_credentials() -> dict[str, str]:
    """AGENT_CREDENTIALS="agente1:secreto1,agente2:secreto2" (una pareja por agente)."""
    pairs = (item.split(":", 1) for item in os.environ.get("AGENT_CREDENTIALS", "").split(",") if ":" in item)
    return {agent.strip(): secret.strip() for agent, secret in pairs}


def verify_agent(
    x_agent_id: Optional[str] = Header(default=None),
    x_agent_secret: Optional[str] = Header(default=None),
):
    # Sin AGENT_CREDENTIALS configurado no entra nadie (falla cerrado).
    expected = _agent_credentials().get(x_agent_id or "")
    if expected is None or not secrets.compare_digest(
        (x_agent_secret or "").encode(), expected.encode()
    ):
        raise HTTPException(status_code=401)


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


@app.get("/api/discovery", dependencies=[Depends(verify_credentials)])
async def get_discovery(limit: int = 10):
    rows = await _pool.fetch(
        "SELECT * FROM discovery_reports ORDER BY received_at DESC LIMIT $1", limit
    )
    return [_serialize(dict(r)) for r in rows]


# ── Ingest (auth por X-Agent-Id + X-Agent-Secret) ────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/ingest/empresas", response_model=IngestResponse, dependencies=[Depends(verify_agent)])
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


@app.post("/ingest/asientos", response_model=IngestResponse, dependencies=[Depends(verify_agent)])
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


@app.post("/ingest/nominas", response_model=IngestResponse, dependencies=[Depends(verify_agent)])
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


@app.post("/diagnostic", response_model=DiagnosticResponse, dependencies=[Depends(verify_agent)])
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


@app.post("/discovery", response_model=DiagnosticResponse, dependencies=[Depends(verify_agent)])
async def discovery(
    report: dict[str, Any],
    x_agent_id: Optional[str] = Header(default=None),
) -> DiagnosticResponse:
    machine = (report.get("diagnostic") or {}).get("machineName")
    logger.info("Agent=%s | Discovery from %s, %d sources",
                x_agent_id, machine, len(report.get("sources", [])))

    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO discovery_reports (agent_id, machine_name, report)
            VALUES ($1, $2, $3)
            """,
            x_agent_id,
            machine,
            json.dumps(report),
        )
    return DiagnosticResponse(status="ok")


# ── Contabilidad (ingesta por ámbito: cada envío sustituye el anterior) ──────

Importe = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]
Mes = Annotated[int, Field(ge=1, le=12)]
Ejercicio = Annotated[int, Field(ge=1900, le=2200)]
Huella = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")]
Doce = Annotated[List[Importe], Field(min_length=12, max_length=12)]


class Estricto(CamelModel):
    # Un campo con otro nombre no se pierde en silencio: la ingesta lo rechaza con 422.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Apunte(Estricto):
    fecha: date
    asiento: int
    num_lin: int
    tipo_linea: int
    num_dh: int = Field(alias="numDH")  # el contrato dice numDH, no numDh
    cuenta: Optional[str] = None
    concepto: Optional[str] = None
    documento: Optional[str] = None
    debe_haber: Literal["D", "H"]
    importe: Importe


class Factura(Estricto):
    tipo: str
    fecha: date
    num_lin: int
    cuenta: Optional[str] = None
    documento: Optional[str] = None
    porcentaje_iva: Annotated[Decimal, Field(max_digits=5, decimal_places=2)]
    base: Importe
    iva: Importe
    irpf: Importe
    tipo_operacion: Annotated[int, Field(ge=0, le=99)]  # 1, 3, 4, 6… (0 en las líneas A)


class ContabilidadMes(Estricto):
    empresa: str = Field(min_length=1)
    ejercicio: Ejercicio
    mes: Mes
    huella: Huella
    apuntes: List[Apunte]
    facturas: List[Factura]


class Cuenta(Estricto):
    codigo: str
    nombre: str
    es_grupo: bool
    debe: Doce
    haber: Doce


class ContabilidadCuentas(Estricto):
    empresa: str = Field(min_length=1)
    ejercicio: Ejercicio
    huella: Huella
    cuentas: List[Cuenta]


class Vencimiento(Estricto):
    cobro_pago: Literal["C", "P"]
    fecha: date
    num_lin: int
    importe: Importe


class ContabilidadVencimientos(Estricto):
    empresa: str = Field(min_length=1)
    huella: Huella
    vencimientos: List[Vencimiento]


class ContabilidadResponse(BaseModel):
    recibidos: int
    sustituidos: int


def _deleted(status: str) -> int:
    """asyncpg devuelve 'DELETE n' al ejecutar un DELETE."""
    return int(status.split()[-1])


async def _registrar_envio(conn, empresa: str, ambito: str, ejercicio: int, mes: int,
                           agent_id: Optional[str], huella: str) -> None:
    await conn.execute(
        "DELETE FROM contabilidad_envios "
        "WHERE empresa = $1 AND ambito = $2 AND ejercicio = $3 AND mes = $4",
        empresa, ambito, ejercicio, mes,
    )
    await conn.execute(
        "INSERT INTO contabilidad_envios (empresa, ambito, ejercicio, mes, agent_id, huella) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        empresa, ambito, ejercicio, mes, agent_id, huella,
    )


@app.post("/ingest/contabilidad/mes", response_model=ContabilidadResponse,
          dependencies=[Depends(verify_agent)])
async def ingest_contabilidad_mes(
    body: ContabilidadMes,
    x_agent_id: Optional[str] = Header(default=None),
) -> ContabilidadResponse:
    logger.info("Agent=%s | contabilidad %s %d-%02d: %d apuntes, %d facturas",
                x_agent_id, body.empresa, body.ejercicio, body.mes,
                len(body.apuntes), len(body.facturas))
    async with _pool.acquire() as conn:
        async with conn.transaction():
            borrados = _deleted(await conn.execute(
                "DELETE FROM apuntes WHERE empresa = $1 AND ejercicio = $2 AND mes = $3",
                body.empresa, body.ejercicio, body.mes,
            ))
            borrados += _deleted(await conn.execute(
                "DELETE FROM facturas WHERE empresa = $1 AND ejercicio = $2 AND mes = $3",
                body.empresa, body.ejercicio, body.mes,
            ))
            await conn.executemany(
                """
                INSERT INTO apuntes (empresa, ejercicio, mes, fecha, asiento, num_lin, tipo_linea,
                                     num_dh, cuenta, concepto, documento, debe_haber, importe)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                [(body.empresa, body.ejercicio, body.mes, a.fecha, a.asiento, a.num_lin,
                  a.tipo_linea, a.num_dh, a.cuenta, a.concepto, a.documento, a.debe_haber,
                  a.importe) for a in body.apuntes],
            )
            await conn.executemany(
                """
                INSERT INTO facturas (empresa, ejercicio, mes, tipo, fecha, num_lin, cuenta,
                                      documento, porcentaje_iva, base, iva, irpf, tipo_operacion)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                [(body.empresa, body.ejercicio, body.mes, f.tipo, f.fecha, f.num_lin, f.cuenta,
                  f.documento, f.porcentaje_iva, f.base, f.iva, f.irpf, f.tipo_operacion) for f in body.facturas],
            )
            await _registrar_envio(conn, body.empresa, "mes", body.ejercicio, body.mes,
                                   x_agent_id, body.huella)
    return ContabilidadResponse(recibidos=len(body.apuntes) + len(body.facturas),
                                sustituidos=borrados)


@app.post("/ingest/contabilidad/cuentas", response_model=ContabilidadResponse,
          dependencies=[Depends(verify_agent)])
async def ingest_contabilidad_cuentas(
    body: ContabilidadCuentas,
    x_agent_id: Optional[str] = Header(default=None),
) -> ContabilidadResponse:
    logger.info("Agent=%s | contabilidad %s %d: %d cuentas",
                x_agent_id, body.empresa, body.ejercicio, len(body.cuentas))
    async with _pool.acquire() as conn:
        async with conn.transaction():
            borrados = _deleted(await conn.execute(
                "DELETE FROM cuentas WHERE empresa = $1 AND ejercicio = $2",
                body.empresa, body.ejercicio,
            ))
            await conn.executemany(
                """
                INSERT INTO cuentas (empresa, ejercicio, codigo, nombre, es_grupo, debe, haber)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                [(body.empresa, body.ejercicio, c.codigo, c.nombre, c.es_grupo, c.debe, c.haber)
                 for c in body.cuentas],
            )
            await _registrar_envio(conn, body.empresa, "cuentas", body.ejercicio, 0,
                                   x_agent_id, body.huella)
    return ContabilidadResponse(recibidos=len(body.cuentas), sustituidos=borrados)


@app.post("/ingest/contabilidad/vencimientos", response_model=ContabilidadResponse,
          dependencies=[Depends(verify_agent)])
async def ingest_contabilidad_vencimientos(
    body: ContabilidadVencimientos,
    x_agent_id: Optional[str] = Header(default=None),
) -> ContabilidadResponse:
    logger.info("Agent=%s | contabilidad %s: %d vencimientos",
                x_agent_id, body.empresa, len(body.vencimientos))
    async with _pool.acquire() as conn:
        async with conn.transaction():
            borrados = _deleted(await conn.execute(
                "DELETE FROM vencimientos WHERE empresa = $1", body.empresa,
            ))
            await conn.executemany(
                """
                INSERT INTO vencimientos (empresa, cobro_pago, fecha, num_lin, importe)
                VALUES ($1, $2, $3, $4, $5)
                """,
                [(body.empresa, v.cobro_pago, v.fecha, v.num_lin, v.importe)
                 for v in body.vencimientos],
            )
            await _registrar_envio(conn, body.empresa, "vencimientos", 0, 0,
                                   x_agent_id, body.huella)
    return ContabilidadResponse(recibidos=len(body.vencimientos), sustituidos=borrados)


@app.get("/api/contabilidad/resumen", dependencies=[Depends(verify_credentials)])
async def get_contabilidad_resumen(empresa: str = Query(min_length=1), ejercicio: Ejercicio = Query()):
    apuntes = await _pool.fetch(
        """
        SELECT mes,
               COUNT(*) AS n,
               COALESCE(SUM(importe) FILTER (WHERE debe_haber = 'D'), 0) AS debe,
               COALESCE(SUM(importe) FILTER (WHERE debe_haber = 'H'), 0) AS haber
        FROM apuntes WHERE empresa = $1 AND ejercicio = $2 GROUP BY mes
        """,
        empresa, ejercicio,
    )
    facturas = await _pool.fetch(
        """
        SELECT mes, COUNT(*) AS n, COALESCE(SUM(base), 0) AS base, COALESCE(SUM(iva), 0) AS iva
        FROM facturas WHERE empresa = $1 AND ejercicio = $2 GROUP BY mes
        """,
        empresa, ejercicio,
    )
    por_mes = {m: {"mes": m, "apuntes": 0, "debe": 0.0, "haber": 0.0,
                   "facturas": 0, "base": 0.0, "iva": 0.0} for m in range(1, 13)}
    for r in apuntes:
        por_mes[r["mes"]].update(apuntes=r["n"], debe=float(r["debe"]), haber=float(r["haber"]))
    for r in facturas:
        por_mes[r["mes"]].update(facturas=r["n"], base=float(r["base"]), iva=float(r["iva"]))
    return {"empresa": empresa, "ejercicio": ejercicio, "meses": list(por_mes.values())}

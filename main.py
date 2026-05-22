import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import FastAPI, Header
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


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


# --- Response model ---

class IngestResponse(BaseModel):
    status: str
    received: int


# --- App ---

app = FastAPI(title="A3 Mock API", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/ingest/empresas", response_model=IngestResponse)
async def ingest_empresas(
    empresas: list[Empresa],
    x_agent_id: Optional[str] = Header(default=None),
) -> IngestResponse:
    logger.info("Agent=%s | Received %d empresas", x_agent_id, len(empresas))
    for e in empresas:
        logger.info("  [empresa] id=%d codigo=%s nombre=%s", e.id, e.codigo, e.nombre)
    return IngestResponse(status="ok", received=len(empresas))


@app.post("/ingest/asientos", response_model=IngestResponse)
async def ingest_asientos(
    asientos: list[Asiento],
    x_agent_id: Optional[str] = Header(default=None),
) -> IngestResponse:
    logger.info("Agent=%s | Received %d asientos", x_agent_id, len(asientos))
    for a in asientos:
        logger.info(
            "  [asiento] id=%d empresa=%s fecha=%s desc=%s importe=%.2f",
            a.id, a.codigo_empresa, a.fecha, a.descripcion, a.importe,
        )
    return IngestResponse(status="ok", received=len(asientos))


@app.post("/ingest/nominas", response_model=IngestResponse)
async def ingest_nominas(
    nominas: list[Nomina],
    x_agent_id: Optional[str] = Header(default=None),
) -> IngestResponse:
    logger.info("Agent=%s | Received %d nominas", x_agent_id, len(nominas))
    for n in nominas:
        logger.info(
            "  [nomina] id=%d empleado=%s nombre=%s periodo=%s neto=%.2f",
            n.id, n.codigo_empleado, n.nombre_empleado, n.periodo, n.importe_neto,
        )
    return IngestResponse(status="ok", received=len(nominas))

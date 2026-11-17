"""Ingesta de contabilidad: cada envío sustituye su ámbito en una sola transacción.

Se usa un pool falso con tablas en memoria que interpreta justo lo que main.py ejecuta:
DELETE ... WHERE col = $n AND ..., INSERT INTO t (cols) VALUES ... y las transacciones
(con rollback). No hay base real.
"""
import copy
import hashlib
import json
import os
import re
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql://unused")

import pytest
from fastapi.testclient import TestClient

import main

HUELLA = hashlib.sha256(b"envio").hexdigest()
AGENTE = {"X-Agent-Id": "agente-1", "X-Agent-Secret": "secreto-1"}
DASHBOARD = ("admin", "clave")


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.pool.tx_started += 1
        self.conn.snapshot = copy.deepcopy(self.conn.pool.tables)
        self.conn.in_tx = True

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.in_tx = False
        if exc_type is None:
            self.conn.pool.tx_committed += 1
        else:
            self.conn.pool.tables = self.conn.snapshot
        return False


class _Conn:
    def __init__(self, pool):
        self.pool = pool
        self.in_tx = False
        self.snapshot = None

    def transaction(self):
        return _Tx(self)

    def _check_tx(self):
        assert self.in_tx, "escritura fuera de transacción"

    async def execute(self, sql, *args):
        self._check_tx()
        self.pool.statements.append((sql, args))
        m = re.match(r"\s*DELETE FROM (\w+) WHERE (.+)", sql, re.S)
        if m:
            table, cond = m.group(1), m.group(2)
            filtros = [(col, args[int(n) - 1]) for col, n in re.findall(r"(\w+) = \$(\d+)", cond)]
            rows = self.pool.tables.setdefault(table, [])
            keep = [r for r in rows if not all(r[c] == v for c, v in filtros)]
            self.pool.tables[table] = keep
            return f"DELETE {len(rows) - len(keep)}"
        m = re.match(r"\s*INSERT INTO (\w+)\s*\(([^)]*)\)", sql, re.S)
        assert m, sql
        cols = [c.strip() for c in m.group(2).split(",")]
        self.pool.tables.setdefault(m.group(1), []).append(dict(zip(cols, args)))
        return "INSERT 0 1"

    async def executemany(self, sql, rows):
        self._check_tx()
        if self.pool.fail_on_insert and f"INSERT INTO {self.pool.fail_on_insert}" in sql:
            raise RuntimeError("fallo simulado")
        for args in rows:
            await self.execute(sql, *args)


class _Acquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self):
        self.tables = {}
        self.statements = []
        self.tx_started = 0
        self.tx_committed = 0
        self.fail_on_insert = None
        self.fetch_results = {}

    def acquire(self):
        return _Acquire(self)

    async def fetch(self, sql, *args):
        for tabla, rows in self.fetch_results.items():
            if f"FROM {tabla}" in sql:
                return rows
        return []


@pytest.fixture
def pool(monkeypatch):
    fake = FakePool()
    monkeypatch.setattr(main, "_pool", fake)
    monkeypatch.setenv("AGENT_CREDENTIALS", "agente-1:secreto-1")
    monkeypatch.setenv("DASHBOARD_USER", DASHBOARD[0])
    monkeypatch.setenv("DASHBOARD_PASSWORD", DASHBOARD[1])
    return fake


client = TestClient(main.app)


def apunte(**extra):
    base = {"fecha": "2026-05-31", "asiento": 1, "numLin": 100100, "tipoLinea": 1, "numDH": 1,
            "cuenta": "465000000000", "concepto": "Nomina mayo", "documento": "N-1",
            "debeHaber": "H", "importe": 800.00}
    return {**base, **extra}


def factura(**extra):
    base = {"tipo": "R", "fecha": "2026-05-20", "numLin": 100100, "cuenta": "410000010000",
            "documento": "F-1", "porcentajeIva": 21.00, "base": 9.36, "iva": 1.97, "irpf": 0.00,
            "tipoOperacion": 1}
    return {**base, **extra}


def mes_body(mes=5, empresa="00569", ejercicio=2026, apuntes=None, facturas=None):
    return {"empresa": empresa, "ejercicio": ejercicio, "mes": mes, "huella": HUELLA,
            "apuntes": [apunte()] if apuntes is None else apuntes,
            "facturas": [factura()] if facturas is None else facturas}


def enviar_mes(**kw):
    return client.post("/ingest/contabilidad/mes", json=mes_body(**kw), headers=AGENTE)


def doce(valor):
    return [valor] * 12


# --- /ingest/contabilidad/mes ---

def test_ingesta_de_un_mes_inserta_lo_esperado(pool):
    r = enviar_mes(apuntes=[apunte(), apunte(numLin=100200, debeHaber="D", cuenta=None)])

    assert r.status_code == 200
    assert r.json() == {"recibidos": 3, "sustituidos": 0}
    apuntes = pool.tables["apuntes"]
    assert len(apuntes) == 2
    assert apuntes[0]["empresa"] == "00569"
    assert apuntes[0]["ejercicio"] == 2026
    assert apuntes[0]["mes"] == 5
    assert apuntes[0]["num_lin"] == 100100
    assert apuntes[0]["debe_haber"] == "H"
    assert apuntes[0]["cuenta"] == "465000000000"
    facturas = pool.tables["facturas"]
    assert len(facturas) == 1
    assert (facturas[0]["tipo"], facturas[0]["mes"], facturas[0]["documento"]) == ("R", 5, "F-1")
    envio = pool.tables["contabilidad_envios"]
    assert len(envio) == 1
    assert (envio[0]["empresa"], envio[0]["ambito"], envio[0]["ejercicio"], envio[0]["mes"]) == ("00569", "mes", 2026, 5)
    assert envio[0]["huella"] == HUELLA
    assert envio[0]["agent_id"] == "agente-1"


def test_reenvio_del_mismo_mes_sustituye_sin_duplicar(pool):
    enviar_mes(apuntes=[apunte(), apunte(numLin=100200)])
    r = enviar_mes(apuntes=[apunte(importe=5.50)])

    assert r.json() == {"recibidos": 2, "sustituidos": 3}
    assert len(pool.tables["apuntes"]) == 1
    assert pool.tables["apuntes"][0]["importe"] == Decimal("5.50")
    assert len(pool.tables["facturas"]) == 1
    assert len(pool.tables["contabilidad_envios"]) == 1


def test_el_envio_entero_va_dentro_de_una_transaccion(pool):
    enviar_mes()

    assert pool.tx_started == 1
    assert pool.tx_committed == 1
    # El fake rechaza cualquier escritura fuera de transacción (assert en _check_tx).
    assert any(sql.lstrip().startswith("DELETE FROM apuntes") for sql, _ in pool.statements)


def test_si_falla_el_insert_se_conserva_lo_anterior(pool):
    enviar_mes(apuntes=[apunte(importe=1.00)])
    pool.fail_on_insert = "facturas"

    with pytest.raises(RuntimeError):
        enviar_mes(apuntes=[apunte(importe=2.00)])

    assert [a["importe"] for a in pool.tables["apuntes"]] == [Decimal("1.00")]


def test_otro_mes_no_se_borra(pool):
    enviar_mes(mes=4, apuntes=[apunte(fecha="2026-04-30")], facturas=[factura(fecha="2026-04-20")])
    enviar_mes(mes=5)

    enviar_mes(mes=5, apuntes=[], facturas=[])

    meses_apuntes = [a["mes"] for a in pool.tables["apuntes"]]
    meses_facturas = [f["mes"] for f in pool.tables["facturas"]]
    assert meses_apuntes == [4]
    assert meses_facturas == [4]


def test_otra_empresa_u_otro_ejercicio_no_se_borra(pool):
    enviar_mes(empresa="00001")
    enviar_mes(ejercicio=2025)

    enviar_mes(mes=5, apuntes=[], facturas=[])

    assert sorted((a["empresa"], a["ejercicio"]) for a in pool.tables["apuntes"]) == [
        ("00001", 2026), ("00569", 2025)]


def test_cuenta_null_aceptada(pool):
    r = enviar_mes(apuntes=[apunte(cuenta=None)], facturas=[factura(cuenta=None)])

    assert r.status_code == 200
    assert pool.tables["apuntes"][0]["cuenta"] is None
    assert pool.tables["facturas"][0]["cuenta"] is None


def test_importe_con_centimos_llega_exacto_como_decimal(pool):
    # Se manda el JSON a mano para que 9.36 y 0.10 viajen tal cual, sin pasar por float de Python.
    texto = json.dumps(mes_body()).replace('"importe": 800.0', '"importe": 0.10')
    r = client.post("/ingest/contabilidad/mes", content=texto,
                    headers={**AGENTE, "Content-Type": "application/json"})

    assert r.status_code == 200
    assert pool.tables["apuntes"][0]["importe"] == Decimal("0.10")
    assert isinstance(pool.tables["apuntes"][0]["importe"], Decimal)
    factura_guardada = pool.tables["facturas"][0]
    assert factura_guardada["base"] == Decimal("9.36")
    assert factura_guardada["iva"] == Decimal("1.97")
    assert factura_guardada["porcentaje_iva"] == Decimal("21.00")


def test_importe_con_mas_de_dos_decimales_se_rechaza(pool):
    r = enviar_mes(apuntes=[apunte(importe=1.005)])

    assert r.status_code == 422
    assert pool.tx_started == 0


@pytest.mark.parametrize("mes", [0, 13, -1])
def test_mes_fuera_de_rango_es_422(pool, mes):
    r = enviar_mes(mes=mes)

    assert r.status_code == 422
    assert pool.tx_started == 0


def test_debe_haber_invalido_es_422(pool):
    assert enviar_mes(apuntes=[apunte(debeHaber="X")]).status_code == 422


def test_huella_que_no_es_sha256_es_422(pool):
    body = {**mes_body(), "huella": "abc"}

    assert client.post("/ingest/contabilidad/mes", json=body, headers=AGENTE).status_code == 422


# --- autenticación ---

RUTAS_INGESTA = [
    ("/ingest/contabilidad/mes", mes_body()),
    ("/ingest/contabilidad/cuentas", {"empresa": "00569", "ejercicio": 2026, "huella": HUELLA, "cuentas": []}),
    ("/ingest/contabilidad/vencimientos", {"empresa": "00569", "huella": HUELLA, "vencimientos": []}),
]


@pytest.mark.parametrize("path,body", RUTAS_INGESTA)
def test_sin_credenciales_401(pool, path, body):
    r = client.post(path, json=body)

    assert r.status_code == 401
    assert pool.tx_started == 0


@pytest.mark.parametrize("path,body", RUTAS_INGESTA)
@pytest.mark.parametrize("headers", [
    {"X-Agent-Id": "agente-1", "X-Agent-Secret": "mala"},
    {"X-Agent-Id": "intruso", "X-Agent-Secret": "secreto-1"},
])
def test_credenciales_malas_401(pool, path, body, headers):
    r = client.post(path, json=body, headers=headers)

    assert r.status_code == 401
    assert pool.tx_started == 0
    assert pool.tables == {}


@pytest.mark.parametrize("path,body", RUTAS_INGESTA)
def test_con_credenciales_buenas_200(pool, path, body):
    assert client.post(path, json=body, headers=AGENTE).status_code == 200


# --- /ingest/contabilidad/cuentas ---

def cuenta(codigo="4650", **extra):
    return {"codigo": codigo, "nombre": "Remuneraciones pendientes", "esGrupo": False,
            "debe": doce(0.00), "haber": doce(800.00), **extra}


def enviar_cuentas(empresa="00569", ejercicio=2026, cuentas=None):
    body = {"empresa": empresa, "ejercicio": ejercicio, "huella": HUELLA,
            "cuentas": [cuenta()] if cuentas is None else cuentas}
    return client.post("/ingest/contabilidad/cuentas", json=body, headers=AGENTE)


def test_cuentas_sustituyen_su_ambito_empresa_y_ejercicio(pool):
    enviar_cuentas(cuentas=[cuenta("4650"), cuenta("4750")])
    enviar_cuentas(ejercicio=2025, cuentas=[cuenta("1000")])
    enviar_cuentas(empresa="00001", cuentas=[cuenta("2000")])

    r = enviar_cuentas(cuentas=[cuenta("5720")])

    assert r.json() == {"recibidos": 1, "sustituidos": 2}
    guardadas = sorted((c["empresa"], c["ejercicio"], c["codigo"]) for c in pool.tables["cuentas"])
    assert guardadas == [("00001", 2026, "2000"), ("00569", 2025, "1000"), ("00569", 2026, "5720")]


def test_cuentas_guardan_los_doce_importes_exactos(pool):
    enviar_cuentas(cuentas=[cuenta(debe=[0.10] * 12, haber=[1.25] * 12)])

    guardada = pool.tables["cuentas"][0]
    assert guardada["debe"] == [Decimal("0.10")] * 12
    assert guardada["haber"] == [Decimal("1.25")] * 12
    assert pool.tables["contabilidad_envios"][0]["ambito"] == "cuentas"


@pytest.mark.parametrize("n", [0, 11, 13])
def test_cuentas_con_meses_distintos_de_12_es_422(pool, n):
    r = enviar_cuentas(cuentas=[cuenta(debe=[0.0] * n)])

    assert r.status_code == 422


# --- /ingest/contabilidad/vencimientos ---

def venc(num_lin=1, cobro_pago="C", importe=10.50):
    return {"cobroPago": cobro_pago, "fecha": "2026-06-30", "numLin": num_lin, "importe": importe}


def enviar_vencimientos(empresa="00569", vencimientos=None):
    body = {"empresa": empresa, "huella": HUELLA,
            "vencimientos": [venc()] if vencimientos is None else vencimientos}
    return client.post("/ingest/contabilidad/vencimientos", json=body, headers=AGENTE)


def test_vencimientos_sustituyen_solo_su_empresa(pool):
    enviar_vencimientos(vencimientos=[venc(1), venc(2, "P")])
    enviar_vencimientos(empresa="00001", vencimientos=[venc(9)])

    r = enviar_vencimientos(vencimientos=[venc(3, "P", 99.99)])

    assert r.json() == {"recibidos": 1, "sustituidos": 2}
    guardados = sorted((v["empresa"], v["num_lin"]) for v in pool.tables["vencimientos"])
    assert guardados == [("00001", 9), ("00569", 3)]
    assert [e["ambito"] for e in pool.tables["contabilidad_envios"] if e["empresa"] == "00569"] == ["vencimientos"]


def test_cobro_pago_invalido_es_422(pool):
    assert enviar_vencimientos(vencimientos=[venc(cobro_pago="Z")]).status_code == 422


def test_envios_de_ambitos_distintos_conviven(pool):
    enviar_mes()
    enviar_cuentas()
    enviar_vencimientos()

    assert sorted(e["ambito"] for e in pool.tables["contabilidad_envios"]) == ["cuentas", "mes", "vencimientos"]


# --- GET /api/contabilidad/resumen ---

def test_resumen_requiere_credenciales_del_dashboard(pool):
    assert client.get("/api/contabilidad/resumen", params={"empresa": "00569", "ejercicio": 2026}).status_code == 401
    assert client.get("/api/contabilidad/resumen", params={"empresa": "00569", "ejercicio": 2026},
                      auth=("admin", "mala")).status_code == 401


def test_resumen_junta_apuntes_y_facturas_por_mes(pool):
    pool.fetch_results = {
        "apuntes": [{"mes": 5, "n": 3, "debe": Decimal("100.10"), "haber": Decimal("100.10")}],
        "facturas": [{"mes": 5, "n": 2, "base": Decimal("9.36"), "iva": Decimal("1.97")},
                     {"mes": 6, "n": 1, "base": Decimal("50.00"), "iva": Decimal("10.50")}],
    }

    r = client.get("/api/contabilidad/resumen", params={"empresa": "00569", "ejercicio": 2026}, auth=DASHBOARD)

    assert r.status_code == 200
    datos = r.json()
    assert datos["empresa"] == "00569" and datos["ejercicio"] == 2026
    assert [m["mes"] for m in datos["meses"]] == list(range(1, 13))
    mayo = datos["meses"][4]
    assert mayo == {"mes": 5, "apuntes": 3, "debe": 100.10, "haber": 100.10,
                    "facturas": 2, "base": 9.36, "iva": 1.97}
    junio = datos["meses"][5]
    assert (junio["apuntes"], junio["facturas"], junio["base"], junio["iva"]) == (0, 1, 50.0, 10.5)
    enero = datos["meses"][0]
    assert (enero["apuntes"], enero["debe"], enero["facturas"], enero["iva"]) == (0, 0.0, 0, 0.0)


def test_resumen_pide_empresa_y_ejercicio(pool):
    assert client.get("/api/contabilidad/resumen", params={"ejercicio": 2026}, auth=DASHBOARD).status_code == 422
    assert client.get("/api/contabilidad/resumen", params={"empresa": "00569"}, auth=DASHBOARD).status_code == 422


# --- Lo que trajo el cuadre con Biloop y el QA de integración ---

def test_guarda_el_tipo_de_operacion_de_cada_factura(pool):
    r = enviar_mes(facturas=[factura(tipoOperacion=6)])

    assert r.status_code == 200
    assert pool.tables["facturas"][0]["tipo_operacion"] == 6


def test_factura_sin_tipo_de_operacion_es_422(pool):
    sin = factura()
    del sin["tipoOperacion"]
    assert enviar_mes(facturas=[sin]).status_code == 422


@pytest.mark.parametrize("donde", ["apunte", "factura", "mes"])
def test_un_campo_desconocido_se_rechaza_en_vez_de_perderse(pool, donde):
    body = mes_body()
    if donde == "apunte":
        body["apuntes"] = [apunte(cuentaX="465000000000")]
    elif donde == "factura":
        body["facturas"] = [factura(baseImponible=9.36)]
    else:
        body["periodo"] = 5
    r = client.post("/ingest/contabilidad/mes", json=body, headers=AGENTE)
    assert r.status_code == 422

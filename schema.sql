-- La API lo aplica en cada arranque: todo debe ser idempotente (IF NOT EXISTS)

CREATE TABLE IF NOT EXISTS empresas (
    id          INTEGER     NOT NULL,
    agent_id    TEXT        NOT NULL,
    codigo      TEXT        NOT NULL,
    nombre      TEXT        NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, agent_id)
);

CREATE TABLE IF NOT EXISTS asientos (
    id             INTEGER     NOT NULL,
    agent_id       TEXT        NOT NULL,
    codigo_empresa TEXT        NOT NULL,
    fecha          DATE        NOT NULL,
    descripcion    TEXT        NOT NULL,
    importe        NUMERIC(15,2) NOT NULL,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, agent_id)
);

CREATE TABLE IF NOT EXISTS nominas (
    id              INTEGER     NOT NULL,
    agent_id        TEXT        NOT NULL,
    codigo_empresa  TEXT        NOT NULL,
    codigo_empleado TEXT        NOT NULL,
    nombre_empleado TEXT        NOT NULL,
    periodo         TEXT        NOT NULL,
    importe_neto    NUMERIC(15,2) NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, agent_id)
);

CREATE TABLE IF NOT EXISTS diagnostic_reports (
    id              SERIAL      PRIMARY KEY,
    agent_id        TEXT,
    machine_name    TEXT,
    os_version      TEXT,
    agent_timestamp TIMESTAMPTZ,
    executables     JSONB,
    scheduled_tasks JSONB,
    com_prog_ids    JSONB,
    odbc_dsns       JSONB,
    install_paths   JSONB,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Informe completo del modo descubrimiento (A3Agent.exe --discover):
-- estructura de las bases de datos de A3, sin filas de datos.
CREATE TABLE IF NOT EXISTS discovery_reports (
    id           SERIAL      PRIMARY KEY,
    agent_id     TEXT,
    machine_name TEXT,
    report       JSONB       NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Contabilidad de a3ASESOR. Cada envío del agente sustituye su ámbito entero
-- (DELETE + INSERT en una transacción), por eso no hay claves únicas por fila.
-- apuntes y facturas: ámbito (empresa, ejercicio, mes).
CREATE TABLE IF NOT EXISTS apuntes (
    empresa    TEXT          NOT NULL,
    ejercicio  INTEGER       NOT NULL,
    mes        SMALLINT      NOT NULL CHECK (mes BETWEEN 1 AND 12),
    fecha      DATE          NOT NULL,
    asiento    INTEGER       NOT NULL,
    num_lin    BIGINT        NOT NULL,
    tipo_linea INTEGER       NOT NULL,
    num_dh     INTEGER       NOT NULL,
    cuenta     TEXT,
    concepto   TEXT,
    documento  TEXT,
    debe_haber CHAR(1)       NOT NULL CHECK (debe_haber IN ('D', 'H')),
    importe    NUMERIC(14,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS facturas (
    empresa        TEXT          NOT NULL,
    ejercicio      INTEGER       NOT NULL,
    mes            SMALLINT      NOT NULL CHECK (mes BETWEEN 1 AND 12),
    tipo           TEXT          NOT NULL,
    fecha          DATE          NOT NULL,
    num_lin        BIGINT        NOT NULL,
    cuenta         TEXT,
    documento      TEXT,
    porcentaje_iva NUMERIC(5,2)  NOT NULL,
    base           NUMERIC(14,2) NOT NULL,
    iva            NUMERIC(14,2) NOT NULL,
    irpf           NUMERIC(14,2) NOT NULL,
    tipo_operacion SMALLINT      NOT NULL DEFAULT 0
);
ALTER TABLE facturas ADD COLUMN IF NOT EXISTS tipo_operacion SMALLINT NOT NULL DEFAULT 0;

-- cuentas: ámbito (empresa, ejercicio); debe y haber son 12 importes (enero..diciembre).
CREATE TABLE IF NOT EXISTS cuentas (
    empresa   TEXT            NOT NULL,
    ejercicio INTEGER         NOT NULL,
    codigo    TEXT            NOT NULL,
    nombre    TEXT            NOT NULL,
    es_grupo  BOOLEAN         NOT NULL,
    debe      NUMERIC(14,2)[] NOT NULL CHECK (cardinality(debe) = 12),
    haber     NUMERIC(14,2)[] NOT NULL CHECK (cardinality(haber) = 12)
);

-- vencimientos: ámbito (empresa).
CREATE TABLE IF NOT EXISTS vencimientos (
    empresa    TEXT          NOT NULL,
    cobro_pago CHAR(1)       NOT NULL CHECK (cobro_pago IN ('C', 'P')),
    fecha      DATE          NOT NULL,
    num_lin    BIGINT        NOT NULL,
    importe    NUMERIC(14,2) NOT NULL
);

-- Un registro por ámbito con la huella y la fecha de recepción del último envío.
-- ambito: 'mes' | 'cuentas' | 'vencimientos'. ejercicio y mes valen 0 cuando no aplican.
CREATE TABLE IF NOT EXISTS contabilidad_envios (
    empresa     TEXT        NOT NULL,
    ambito      TEXT        NOT NULL,
    ejercicio   INTEGER     NOT NULL,
    mes         SMALLINT    NOT NULL,
    agent_id    TEXT,
    huella      TEXT        NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (empresa, ambito, ejercicio, mes)
);

CREATE INDEX IF NOT EXISTS idx_apuntes_empresa_ejercicio_mes  ON apuntes(empresa, ejercicio, mes);
CREATE INDEX IF NOT EXISTS idx_facturas_empresa_ejercicio_mes ON facturas(empresa, ejercicio, mes);
CREATE INDEX IF NOT EXISTS idx_cuentas_empresa_ejercicio      ON cuentas(empresa, ejercicio);
CREATE INDEX IF NOT EXISTS idx_vencimientos_empresa           ON vencimientos(empresa);

-- Índices para las queries del dashboard (ORDER BY received_at DESC)
CREATE INDEX IF NOT EXISTS idx_empresas_received_at        ON empresas(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_asientos_received_at        ON asientos(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_nominas_received_at         ON nominas(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_diagnostic_received_at      ON diagnostic_reports(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_received_at       ON discovery_reports(received_at DESC);

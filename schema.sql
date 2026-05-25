-- Ejecutar una sola vez contra la base de datos a3_conficial

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

-- Índices para las queries del dashboard (ORDER BY received_at DESC)
CREATE INDEX IF NOT EXISTS idx_empresas_received_at        ON empresas(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_asientos_received_at        ON asientos(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_nominas_received_at         ON nominas(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_diagnostic_received_at      ON diagnostic_reports(received_at DESC);

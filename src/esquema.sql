-- Esquema de la plataforma de carteras institucionales MX-US.
-- Principio: los crudos entran sin criterio editorial. Toda decision revisable
-- (denominador, mapeo a clase de activo) vive en una VISTA o tabla de mapeo,
-- nunca horneada en la tabla de hechos.

CREATE TABLE IF NOT EXISTS parametros (
    clave       VARCHAR PRIMARY KEY,
    valor       VARCHAR NOT NULL,
    descripcion VARCHAR NOT NULL,
    decidido_en DATE    NOT NULL,
    evidencia   VARCHAR
);

CREATE TABLE IF NOT EXISTS sources (
    source_id        VARCHAR PRIMARY KEY,
    nombre           VARCHAR NOT NULL,
    cadence          VARCHAR NOT NULL,
    typical_lag_days INTEGER,
    grano            VARCHAR NOT NULL,   -- 'holdings' | 'allocations'
    denominador      VARCHAR NOT NULL,   -- declarado, distinto entre fuentes
    coverage_caveat  VARCHAR
);

-- Idempotencia: si el sha256 ya esta aqui con status 'ok', no se reprocesa.
CREATE SEQUENCE IF NOT EXISTS seq_ingest START 1;
CREATE TABLE IF NOT EXISTS ingest_log (
    ingest_id        BIGINT PRIMARY KEY DEFAULT nextval('seq_ingest'),
    source           VARCHAR NOT NULL,
    archivo          VARCHAR NOT NULL,
    ruta_crudo       VARCHAR NOT NULL,   -- copia inmutable, guardada antes de parsear
    sha256           VARCHAR NOT NULL,
    bytes            BIGINT,
    filas_leidas     BIGINT,
    filas_cargadas   BIGINT,
    publication_date DATE,
    status           VARCHAR NOT NULL,   -- 'ok' | 'error' | 'omitido_idempotencia'
    detalle          VARCHAR,
    ingested_at      TIMESTAMP NOT NULL DEFAULT now()
);

-- Unidad de versionado. Recargar el mismo (source, entity, as_of_date) crea una
-- version nueva y marca la anterior superseded. Nada se borra.
CREATE SEQUENCE IF NOT EXISTS seq_snapshot START 1;
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id      BIGINT PRIMARY KEY DEFAULT nextval('seq_snapshot'),
    source           VARCHAR NOT NULL,
    entity_id        VARCHAR NOT NULL,
    as_of_date       DATE    NOT NULL,
    publication_date DATE,
    ingest_id        BIGINT  NOT NULL,
    version          INTEGER NOT NULL,
    es_provisional   BOOLEAN NOT NULL DEFAULT FALSE,
    loaded_at        TIMESTAMP NOT NULL DEFAULT now(),
    superseded_at    TIMESTAMP,
    superseded_by    BIGINT
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id      VARCHAR PRIMARY KEY,
    source         VARCHAR NOT NULL,
    clave_nativa   VARCHAR NOT NULL,
    nombre         VARCHAR,
    entity_type    VARCHAR NOT NULL,   -- 'vehiculo' | 'gestor'
    subtipo        VARCHAR,            -- 'siefore' | 'fondo_inversion_mx' | 'gestor_13f'
    operadora      VARCHAR,            -- afore / administradora
    vigencia_desde DATE,
    vigencia_hasta DATE
);

CREATE TABLE IF NOT EXISTS entity_taxonomy (
    entity_id                   VARCHAR NOT NULL,
    taxonomy_version            VARCHAR NOT NULL,
    estrategia                  VARCHAR,
    subestrategia               VARCHAR,
    estrategia_declarada_fuente VARCHAR,
    valid_from                  DATE NOT NULL,
    valid_to                    DATE,
    PRIMARY KEY (entity_id, taxonomy_version, valid_from)
);

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id   VARCHAR PRIMARY KEY,
    source          VARCHAR NOT NULL,
    tv              VARCHAR,
    tipo_valor_desc VARCHAR,
    emisora         VARCHAR,
    serie           VARCHAR,
    cusip           VARCHAR,
    isin            VARCHAR,
    primera_vez     DATE,
    ultima_vez      DATE
);

-- El mapeo vive en catalogos/asset_class_map_<source>.csv (versionado en git) y se
-- carga aqui en cada corrida. '*' en emisora_pattern = regla por tipo de valor;
-- una emisora explicita gana sobre el '*' de su TV (necesario para los ETFs, donde
-- el TV mezcla acciones y deuda).
CREATE TABLE IF NOT EXISTS asset_class_map (
    source          VARCHAR NOT NULL,
    tv_pattern      VARCHAR NOT NULL,
    emisora_pattern VARCHAR NOT NULL DEFAULT '*',
    asset_class     VARCHAR NOT NULL,
    subclase        VARCHAR,
    mapped_by       VARCHAR NOT NULL,   -- 'tipo_valor_explicito' | 'catalogo_etf' | 'anexo8_cufi' | 'juicio_pendiente'
    notes           VARCHAR,
    valid_from      DATE NOT NULL DEFAULT DATE '1900-01-01',
    PRIMARY KEY (source, tv_pattern, emisora_pattern, valid_from)
);

-- Grano: (source, entity_id, as_of_date, instrument_id). Para CNBV R7 y SEC 13F.
CREATE SEQUENCE IF NOT EXISTS seq_holding START 1;
CREATE TABLE IF NOT EXISTS holdings (
    holding_id         BIGINT PRIMARY KEY DEFAULT nextval('seq_holding'),
    snapshot_id        BIGINT  NOT NULL,
    source             VARCHAR NOT NULL,
    entity_id          VARCHAR NOT NULL,
    as_of_date         DATE    NOT NULL,
    publication_date   DATE,
    instrument_id      VARCHAR NOT NULL,
    tipo_inversion     VARCHAR NOT NULL,   -- las 4 categorias de R7, sin colapsar
    titulos_raw        DOUBLE,
    valor_unitario_raw DOUBLE,
    valor_total_raw    DOUBLE,
    moneda_raw         VARCHAR,
    unidad_valor_raw   VARCHAR,            -- 'MXN' | 'USD_miles' (13F pre-cambio) ...
    valor_total_mxn    DOUBLE,
    fila_origen        INTEGER
);

-- Grano: (source, entity_id, as_of_date, asset_class). Para CONSAR.
CREATE SEQUENCE IF NOT EXISTS seq_alloc START 1;
CREATE TABLE IF NOT EXISTS allocations (
    alloc_id         BIGINT PRIMARY KEY DEFAULT nextval('seq_alloc'),
    snapshot_id      BIGINT  NOT NULL,
    source           VARCHAR NOT NULL,
    entity_id        VARCHAR NOT NULL,
    siefore          VARCHAR,
    afore            VARCHAR,            -- NULL = total de la siefore (todas las afores)
    as_of_date       DATE    NOT NULL,
    publication_date DATE,
    asset_class      VARCHAR NOT NULL,
    peso             DOUBLE,             -- NULL si la fuente no reporto. Nunca imputado.
    peso_raw         VARCHAR,            -- el texto tal como vino
    origen_peso      VARCHAR             -- 'reportado' | 'calculado'
);

-- Regla 4: campo ausente = NULL + registro aqui. Nunca imputar.
CREATE SEQUENCE IF NOT EXISTS seq_calidad START 1;
CREATE TABLE IF NOT EXISTS calidad_log (
    id         BIGINT PRIMARY KEY DEFAULT nextval('seq_calidad'),
    ingest_id  BIGINT,
    source     VARCHAR,
    regla      VARCHAR NOT NULL,
    severidad  VARCHAR NOT NULL,   -- 'error' | 'alerta' | 'info'
    entity_id  VARCHAR,
    as_of_date DATE,
    detalle    VARCHAR,
    valor      DOUBLE,
    creado_at  TIMESTAMP NOT NULL DEFAULT now()
);

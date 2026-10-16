-- Schéma de collecte Vélib + météo.
-- Types compacts volontairement : ~140 000 lignes/jour dans station_status,
-- il faut tenir dans le stockage gratuit (1 Go sur Neon Free).

CREATE TABLE IF NOT EXISTS stations (
    station_id   BIGINT PRIMARY KEY,
    station_code TEXT,
    name         TEXT NOT NULL,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    capacity     SMALLINT,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Un instantané par station et par passage du collecteur.
-- ts = heure de collecte arrondie à la minute (clé de jointure avec la météo).
CREATE TABLE IF NOT EXISTS station_status (
    ts            TIMESTAMPTZ NOT NULL,
    station_id    BIGINT      NOT NULL,
    bikes_mech    SMALLINT    NOT NULL,
    bikes_ebike   SMALLINT    NOT NULL,
    docks         SMALLINT    NOT NULL,
    is_renting    BOOLEAN     NOT NULL,
    is_returning  BOOLEAN     NOT NULL,
    last_reported TIMESTAMPTZ,
    PRIMARY KEY (station_id, ts)
);

CREATE INDEX IF NOT EXISTS station_status_ts_idx ON station_status USING brin (ts);

CREATE TABLE IF NOT EXISTS weather (
    ts            TIMESTAMPTZ PRIMARY KEY,
    temperature_c REAL,
    precip_mm     REAL,
    wind_kmh      REAL,
    weather_code  SMALLINT
);

-- Journal des passages : permet de repérer les trous de collecte.
CREATE TABLE IF NOT EXISTS collection_runs (
    ts          TIMESTAMPTZ PRIMARY KEY,
    n_stations  INTEGER NOT NULL,
    weather_ok  BOOLEAN NOT NULL,
    duration_ms INTEGER NOT NULL
);

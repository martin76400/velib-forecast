"""Collecteur Vélib + météo.

Lancé toutes les 15 min par GitHub Actions. Récupère :
  - station_status.json (GBFS Vélib) : vélos mécaniques / électriques, bornes libres
  - station_information.json : nom, position, capacité (mis à jour si changement)
  - météo actuelle à Paris (Open-Meteo, sans clé)
et écrit le tout dans Postgres (variable d'environnement DATABASE_URL).

Usage :
    python -m collector.collect            # collecte et insère
    python -m collector.collect --dry-run  # collecte et affiche, sans base
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

GBFS_BASE = "https://velib-metropole-opendata.smovengo.cloud/opendata/Velib_Metropole"
STATUS_URL = f"{GBFS_BASE}/station_status.json"
INFO_URL = f"{GBFS_BASE}/station_information.json"
WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=48.8566&longitude=2.3522"
    "&current=temperature_2m,precipitation,wind_speed_10m,weather_code"
    "&timezone=UTC"
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
HTTP_TIMEOUT = 20
HEADERS = {"User-Agent": "velib-forecast-student-project (github)"}

log = logging.getLogger("collector")


# --------------------------------------------------------------------------- #
# Récupération HTTP
# --------------------------------------------------------------------------- #
def fetch_json(url: str, retries: int = 3) -> dict:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT, headers=HEADERS)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            log.warning("Échec %s (tentative %d/%d) : %s", url, attempt, retries, exc)
            time.sleep(2 * attempt)
    raise RuntimeError(f"Impossible de récupérer {url}") from last_exc


# --------------------------------------------------------------------------- #
# Parsing (fonctions pures, testées dans tests/)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StatusRow:
    station_id: int
    bikes_mech: int
    bikes_ebike: int
    docks: int
    is_renting: bool
    is_returning: bool
    last_reported: datetime | None


@dataclass(frozen=True)
class StationRow:
    station_id: int
    station_code: str | None
    name: str
    lat: float
    lon: float
    capacity: int | None


def _bike_types(raw: dict) -> tuple[int, int]:
    """Vélib publie num_bikes_available_types = [{"mechanical": n}, {"ebike": m}]."""
    mech = ebike = 0
    for item in raw.get("num_bikes_available_types") or []:
        mech += int(item.get("mechanical", 0) or 0)
        ebike += int(item.get("ebike", 0) or 0)
    if mech == 0 and ebike == 0:
        # Repli : pas de détail par type, on met tout en mécanique.
        mech = int(raw.get("num_bikes_available", 0) or 0)
    return mech, ebike


def _ts(value) -> datetime | None:
    if not value:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    # Valeurs aberrantes (0, ou timestamps dans le futur lointain) ignorées.
    if v < 1_400_000_000 or v > 4_000_000_000:
        return None
    return datetime.fromtimestamp(v, tz=timezone.utc)


def parse_status(payload: dict) -> list[StatusRow]:
    rows: list[StatusRow] = []
    seen: set[int] = set()
    for s in payload.get("data", {}).get("stations", []):
        try:
            sid = int(s["station_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if sid in seen:  # doublons occasionnels dans le flux
            continue
        seen.add(sid)
        mech, ebike = _bike_types(s)
        rows.append(
            StatusRow(
                station_id=sid,
                bikes_mech=mech,
                bikes_ebike=ebike,
                docks=int(s.get("num_docks_available", 0) or 0),
                is_renting=bool(int(s.get("is_renting", 0) or 0)),
                is_returning=bool(int(s.get("is_returning", 0) or 0)),
                last_reported=_ts(s.get("last_reported")),
            )
        )
    return rows


def parse_information(payload: dict) -> list[StationRow]:
    rows: list[StationRow] = []
    for s in payload.get("data", {}).get("stations", []):
        try:
            rows.append(
                StationRow(
                    station_id=int(s["station_id"]),
                    station_code=str(s["stationCode"]) if s.get("stationCode") else None,
                    name=str(s["name"]).strip(),
                    lat=float(s["lat"]),
                    lon=float(s["lon"]),
                    capacity=int(s["capacity"]) if s.get("capacity") is not None else None,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def parse_weather(payload: dict) -> dict | None:
    cur = payload.get("current")
    if not cur:
        return None
    return {
        "temperature_c": cur.get("temperature_2m"),
        "precip_mm": cur.get("precipitation"),
        "wind_kmh": cur.get("wind_speed_10m"),
        "weather_code": cur.get("weather_code"),
    }


def run_timestamp(now: datetime | None = None) -> datetime:
    """Heure de collecte arrondie à la minute, en UTC."""
    now = now or datetime.now(timezone.utc)
    return now.replace(second=0, microsecond=0)


# --------------------------------------------------------------------------- #
# Écriture en base
# --------------------------------------------------------------------------- #
def write_to_db(
    dsn: str,
    ts: datetime,
    status: list[StatusRow],
    stations: list[StationRow],
    weather: dict | None,
    duration_ms: int,
) -> int:
    import psycopg  # import local : --dry-run fonctionne sans le driver

    with psycopg.connect(dsn, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_PATH.read_text())

            # Stations : on ne réécrit que si quelque chose a changé
            # (évite de gonfler la table toutes les 15 min).
            cur.executemany(
                """
                INSERT INTO stations (station_id, station_code, name, lat, lon, capacity)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (station_id) DO UPDATE SET
                    station_code = EXCLUDED.station_code,
                    name = EXCLUDED.name,
                    lat = EXCLUDED.lat,
                    lon = EXCLUDED.lon,
                    capacity = EXCLUDED.capacity,
                    updated_at = now()
                WHERE (stations.station_code, stations.name, stations.lat,
                       stations.lon, stations.capacity)
                  IS DISTINCT FROM
                      (EXCLUDED.station_code, EXCLUDED.name, EXCLUDED.lat,
                       EXCLUDED.lon, EXCLUDED.capacity)
                """,
                [(s.station_id, s.station_code, s.name, s.lat, s.lon, s.capacity) for s in stations],
            )

            # Statuts : COPY dans une table temporaire puis INSERT idempotent
            # (relancer deux fois le même passage ne crée pas de doublons).
            cur.execute(
                "CREATE TEMP TABLE tmp_status (LIKE station_status INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            with cur.copy(
                "COPY tmp_status (ts, station_id, bikes_mech, bikes_ebike, docks,"
                " is_renting, is_returning, last_reported) FROM STDIN"
            ) as copy:
                for r in status:
                    copy.write_row(
                        (ts, r.station_id, r.bikes_mech, r.bikes_ebike, r.docks,
                         r.is_renting, r.is_returning, r.last_reported)
                    )
            cur.execute(
                "INSERT INTO station_status SELECT * FROM tmp_status ON CONFLICT DO NOTHING"
            )
            inserted = cur.rowcount

            if weather:
                cur.execute(
                    """
                    INSERT INTO weather (ts, temperature_c, precip_mm, wind_kmh, weather_code)
                    VALUES (%(ts)s, %(temperature_c)s, %(precip_mm)s, %(wind_kmh)s, %(weather_code)s)
                    ON CONFLICT (ts) DO NOTHING
                    """,
                    {"ts": ts, **weather},
                )

            cur.execute(
                """
                INSERT INTO collection_runs (ts, n_stations, weather_ok, duration_ms)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (ts) DO UPDATE SET n_stations = EXCLUDED.n_stations,
                    weather_ok = EXCLUDED.weather_ok, duration_ms = EXCLUDED.duration_ms
                """,
                (ts, len(status), weather is not None, duration_ms),
            )
    return inserted


# --------------------------------------------------------------------------- #
# Point d'entrée
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="n'écrit pas en base")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    t0 = time.monotonic()
    ts = run_timestamp()

    status = parse_status(fetch_json(STATUS_URL))
    stations = parse_information(fetch_json(INFO_URL))
    if len(status) < 100:
        # Flux visiblement cassé : on échoue pour que l'erreur apparaisse dans Actions.
        log.error("Seulement %d stations reçues, abandon.", len(status))
        return 1

    try:
        weather = parse_weather(fetch_json(WEATHER_URL))
    except RuntimeError:
        weather = None  # la météo ne doit jamais bloquer la collecte Vélib
        log.warning("Météo indisponible pour ce passage.")

    duration_ms = int((time.monotonic() - t0) * 1000)
    log.info("%d statuts, %d stations, météo=%s", len(status), len(stations), weather)

    if args.dry_run:
        for r in status[:5]:
            print(r)
        return 0

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        log.error("DATABASE_URL non définie.")
        return 1
    inserted = write_to_db(dsn, ts, status, stations, weather, duration_ms)
    log.info("Passage %s : %d lignes insérées.", ts.isoformat(), inserted)
    return 0


if __name__ == "__main__":
    sys.exit(main())

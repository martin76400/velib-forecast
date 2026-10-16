"""Tests du collecteur.

Les tests de parsing tournent partout. Le test d'intégration ne tourne que si
TEST_DATABASE_URL pointe vers une base Postgres jetable.
"""

import os
from datetime import datetime, timezone

import pytest

from collector.collect import (
    parse_information,
    parse_status,
    parse_weather,
    run_timestamp,
    write_to_db,
)

# Extrait au format du flux GBFS Vélib (station_status.json)
STATUS = {
    "lastUpdatedOther": 1760550000,
    "ttl": 3600,
    "data": {
        "stations": [
            {
                "stationCode": "16107",
                "station_id": 213688169,
                "num_bikes_available": 7,
                "numBikesAvailable": 7,
                "num_bikes_available_types": [{"mechanical": 4}, {"ebike": 3}],
                "num_docks_available": 28,
                "numDocksAvailable": 28,
                "is_installed": 1,
                "is_returning": 1,
                "is_renting": 1,
                "last_reported": 1760549900,
            },
            {
                "stationCode": "6015",
                "station_id": 54000559,
                "num_bikes_available": 2,
                "num_bikes_available_types": [],
                "num_docks_available": 20,
                "is_installed": 1,
                "is_returning": 0,
                "is_renting": 0,
                "last_reported": 0,
            },
            # doublon et ligne invalide : doivent être ignorés
            {"station_id": 213688169, "num_bikes_available": 99},
            {"num_bikes_available": 1},
        ]
    },
}

INFO = {
    "data": {
        "stations": [
            {
                "station_id": 213688169,
                "name": "Benjamin Godard - Victor Hugo ",
                "lat": 48.865983,
                "lon": 2.275725,
                "capacity": 35,
                "stationCode": "16107",
            },
            {"station_id": 1, "name": "sans coordonnées"},
        ]
    }
}

WEATHER = {
    "current": {
        "time": "2026-10-15T18:45",
        "temperature_2m": 12.4,
        "precipitation": 0.2,
        "wind_speed_10m": 14.0,
        "weather_code": 61,
    }
}


def test_parse_status_types_and_dedup():
    rows = parse_status(STATUS)
    assert [r.station_id for r in rows] == [213688169, 54000559]
    first, second = rows
    assert (first.bikes_mech, first.bikes_ebike, first.docks) == (4, 3, 28)
    assert first.is_renting and first.is_returning
    assert first.last_reported == datetime.fromtimestamp(1760549900, tz=timezone.utc)
    # pas de détail par type -> repli sur num_bikes_available
    assert (second.bikes_mech, second.bikes_ebike) == (2, 0)
    assert second.last_reported is None and not second.is_renting


def test_parse_information_skips_invalid():
    rows = parse_information(INFO)
    assert len(rows) == 1
    assert rows[0].name == "Benjamin Godard - Victor Hugo"
    assert rows[0].capacity == 35


def test_parse_weather():
    assert parse_weather(WEATHER)["temperature_c"] == 12.4
    assert parse_weather({}) is None


def test_run_timestamp_rounds_to_minute():
    ts = run_timestamp(datetime(2026, 10, 15, 18, 45, 37, 123, tzinfo=timezone.utc))
    assert ts == datetime(2026, 10, 15, 18, 45, tzinfo=timezone.utc)


@pytest.mark.skipif("TEST_DATABASE_URL" not in os.environ, reason="pas de base de test")
def test_write_is_idempotent():
    import psycopg

    dsn = os.environ["TEST_DATABASE_URL"]
    ts = datetime(2026, 10, 15, 18, 45, tzinfo=timezone.utc)
    status, stations, weather = parse_status(STATUS), parse_information(INFO), parse_weather(WEATHER)

    assert write_to_db(dsn, ts, status, stations, weather, 1234) == 2
    assert write_to_db(dsn, ts, status, stations, weather, 1234) == 0  # relance : aucun doublon

    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM station_status").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM stations").fetchone()[0] == 1
        assert conn.execute("SELECT temperature_c FROM weather").fetchone()[0] == pytest.approx(12.4)
        assert conn.execute("SELECT n_stations FROM collection_runs").fetchone()[0] == 2

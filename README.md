# Vélib Forecast

Prédire la disponibilité des vélos dans les ~1 400 stations Vélib' Métropole, à l'horizon d'une heure.

> 🚧 Étape 1 en cours : collecte automatique des données. Modélisation et API à venir.

## Pipeline

```
GBFS Vélib (temps réel) ─┐
                          ├─► collecteur Python ─► Postgres (Neon) ─► modèle ─► API FastAPI
Open-Meteo (météo) ──────┘   (GitHub Actions, toutes les 15 min)
```

## Données

| Table | Contenu |
|---|---|
| `station_status` | Un instantané par station et par passage : vélos mécaniques, électriques, bornes libres |
| `stations` | Nom, position, capacité (mis à jour seulement si changement) |
| `weather` | Température, précipitations, vent à Paris |
| `collection_runs` | Journal des passages, pour repérer les trous de collecte |

Sources : [Vélib' Métropole – GBFS](https://www.data.gouv.fr/fr/datasets/velib-velos-et-bornes-disponibilite-temps-reel/) (licence ODbL), [Open-Meteo](https://open-meteo.com/).

## Lancer en local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m collector.collect --dry-run          # teste la collecte sans base
export DATABASE_URL="postgresql://..."
python -m collector.collect                     # collecte et écrit en base
pytest -q                                       # tests
```

## Choix techniques

- **Insertion idempotente** (COPY + `ON CONFLICT DO NOTHING`) : un passage relancé ne crée pas de doublons.
- **Types compacts** (`SMALLINT`, index BRIN sur le temps) : ~14 Mo/jour, pour tenir dans l'offre gratuite.
- **La météo ne bloque jamais la collecte** : si Open-Meteo échoue, les données Vélib sont quand même enregistrées.

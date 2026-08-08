# Ask The Verse data server

This repository builds versioned local datasets for Ask The Verse from two
Star Citizen data sources:

- [Erkul](https://erkul.games): ships, ground vehicles, loadouts, and component
  families.
- [SCMDB](https://scmdb.net): missions, crafting, mining, equipment, and
  related shared data pools.

The repository includes the crawler/SQLite builder and a FastAPI service that
warms both sources before exposing the current complete dataset.

## What it does

Each workflow:

1. Checks the source's current game version.
2. Skips the source if that version is already marked complete.
3. Downloads the required source files to a versioned local directory.
4. Parses the source data into type-specific SQLite tables.
5. Records run status, file hashes, source URLs, and record counts.

Equivalent Erkul and SCMDB version strings are normalized to one directory, so
both sources contribute to the same database:

```text
data/<normalized-version>/game_data.sqlite3
```

The generated `data/` directory is intentionally ignored by Git.

## Quick start

Requirements:

- Python 3.9 or newer
- Internet access to `cdn.erkul.games` and `scmdb.net`
- `fastapi` and `uvicorn` for the HTTP service

Install the service and development dependencies from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Run either crawler directly:

```bash
python3 workflows/erkul_workflow.py
python3 workflows/scmdb_workflow.py
```

Force a refresh of one source without replacing the other source's tables:

```bash
python3 workflows/erkul_workflow.py --force
python3 workflows/scmdb_workflow.py --force
```

Other useful options:

```bash
python3 workflows/erkul_workflow.py --branch PTU
python3 workflows/scmdb_workflow.py --channel ptu
python3 workflows/erkul_workflow.py --output-root /tmp/game-data
```

## HTTP server

Start one FastAPI process with one Uvicorn worker:

```bash
.venv/bin/uvicorn data_server.main:app --workers 1
```

`DATA_ROOT` can override the default `data/` directory, and `LOG_LEVEL` can
override the default `INFO` logging level.

The server checks and warms Erkul `LIVE` and SCMDB `live` in parallel before
accepting requests. Available endpoints are:

```text
GET /health
GET /api/v1/versions
GET /api/v1/ships?name=Hammerhead
```

Ship searches use exact, unique substring, then fuzzy suggestion matching.
Details not already present in `erkul_ships` are downloaded and cached with a
per-ship single-flight lock.

## Output layout

```text
data/<version>/
├── game_data.sqlite3
├── erkul/
│   ├── raw/       # Original raw-DEFLATE .bin files
│   └── decoded/   # Readable decoded JSON
└── scmdb/
    └── raw/       # Original JSON files
```

The shared database contains:

- `crawl_runs`: completion state for each source.
- `source_files`: downloaded paths, URLs, hashes, sizes, and timestamps.
- `erkul_*`: the ship catalogue, Hammerhead detail, slots, installed
  components, manifest resources, and component families.
- `scmdb_*`: one table per top-level SCMDB data type.

See the complete schemas:

- [Erkul database schema](docs/erkul/DATABASE_SCHEMA.md)
- [SCMDB database schema](docs/scmdb/DATABASE_SCHEMA.md)

## Repository map

| Path | Responsibility |
|---|---|
| `data_server/` | FastAPI routes, startup lifecycle, SQLite locking, ship matching, lazy loading, and response models |
| `workflows/game_data_common.py` | Shared version normalization, HTTP retries, atomic writes, hashing, SQLite setup, run state, and dependency-aware table replacement |
| `workflows/erkul_workflow.py` | Erkul manifest discovery, raw-DEFLATE decoding, group-index paths, full lightweight vehicle catalogue, Hammerhead detail, slots, and component families |
| `workflows/scmdb_workflow.py` | SCMDB version discovery, required/optional JSON downloads, and dynamic table generation |
| `workflows/README.md` | Short workflow command reference |
| `docs/erkul/README.md` | Reverse-engineering notes for Erkul's CDN format and data-loading chain |
| `docs/erkul/DATABASE_SCHEMA.md` | Every Erkul table and column |
| `docs/scmdb/README.md` | SCMDB source model, search behavior, crafting model, and mining calculations |
| `docs/scmdb/DATABASE_SCHEMA.md` | SCMDB generated-table contract and current table inventory |
| `docs/erkul/erkul_fetch.py` | Original Erkul analysis/reproduction script |
| `docs/scmdb/scmdb_analysis_scmdb_client.py` | Original SCMDB analysis/reference client |

## Where to make changes

### Add or change Erkul data

Start in `workflows/erkul_workflow.py`.

- Change downloads or manifest traversal in `ErkulWorkflow.run`.
- Add catalogue columns in `create_schema` and `insert_ship_catalog`.
- Change full ship selection in `select_hammerhead` and the group-index lookup.
- Add Hammerhead detail fields in `insert_ship`.
- Change recursive slot/component extraction in `insert_slots`.
- Change component-family storage in `insert_families`.

Update `docs/erkul/DATABASE_SCHEMA.md` whenever a table or column changes.

### Add or change SCMDB data

Start in `workflows/scmdb_workflow.py`.

- Change required or optional source files in `ScmdbWorkflow.run`.
- Change common extracted lookup fields in `extract_guid`, `extract_type`, or
  the shared `display_name` helper.
- Change table naming or record storage in `insert_dataset` and
  `insert_data_type`.

New SCMDB top-level JSON keys already receive tables automatically. Update
`docs/scmdb/DATABASE_SCHEMA.md` when the generated contract or known inventory
changes.

### Change shared behavior

Use `workflows/game_data_common.py` for behavior that applies to both sources:

- Version-directory and database naming
- HTTP request headers, retries, and timeouts
- Atomic file writes and SHA-256 tracking
- `crawl_runs` and `source_files`
- Source completion checks
- Safe replacement of source-prefixed tables

Keep source-specific parsing out of this module.

### Add another data source

1. Add `workflows/<source>_workflow.py`.
2. Reuse the common version, download, file-recording, and run-state helpers.
3. Prefix source-owned tables with `<source>_`.
4. Replace only that prefix during `--force` refreshes.
5. Store complete source records in `payload_json` when flattening selected
   fields.
6. Add source analysis and schema documentation under `docs/<source>/`.
7. Add the new source to this README and `workflows/README.md`.

## Design rules

- Discover versions and hashed paths from source manifests; do not hardcode
  current hashes.
- Keep one database file per normalized game version.
- Keep each source independently refreshable and independently marked
  complete.
- Validate Erkul manifest checksums before parsing.
- Preserve raw downloads for reproducibility.
- Preserve complete source objects as JSON when selected columns expose only a
  subset.
- Do not commit generated `data/`, caches, credentials, or local environments.
- Prefer the Python standard library unless a dependency solves a demonstrated
  problem.

## Verification

Compile all workflow modules:

```bash
python3 -m py_compile \
  workflows/game_data_common.py \
  workflows/erkul_workflow.py \
  workflows/scmdb_workflow.py
```

Check a generated database:

```bash
sqlite3 data/<version>/game_data.sqlite3 "PRAGMA integrity_check;"
sqlite3 data/<version>/game_data.sqlite3 \
  "SELECT source, status, file_count, record_count FROM crawl_runs;"
```

After changing a workflow, run it once with `--force`, inspect representative
rows, then run it normally and confirm that the completed version is skipped.

## Current scope

- The complete Erkul lightweight catalogue is stored, including detail blob
  paths for every ship and ground vehicle.
- Only Hammerhead's full Erkul ship blob is downloaded and expanded.
- SCMDB required files and available optional overlays are stored.
- Workflows can run manually and are also executed during FastAPI startup.
- The HTTP API exposes health, complete versions, and current-version ship
  search/detail responses.

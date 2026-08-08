# Ask The Verse Data Server

[![Data Server CI](https://github.com/Ask-The-Verse/data-server/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Ask-The-Verse/data-server/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Ask-The-Verse/data-server/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/Ask-The-Verse/data-server/actions/workflows/codeql.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)

FastAPI and SQLite game data service for Ask The Verse.

The service builds versioned local datasets from two Star Citizen data
sources:

- [Erkul](https://erkul.games): ships, ground vehicles, loadouts, and component
  families.
- [SCMDB](https://scmdb.net): missions, crafting, mining, equipment, and
  related shared data pools.

At startup, the service checks both upstream sources in parallel, reuses
complete local data when possible, and exposes only a version completed by
both workflows.

## Features

- Parallel Erkul `LIVE` and SCMDB `live` startup workflows
- One SQLite database per normalized game version
- Exact, substring, and fuzzy ship-name matching
- Lazy ship-detail downloads with SHA-256 validation
- Per-ship single-flight locking to prevent duplicate downloads
- Writer-priority database locking and per-thread SQLite connections
- Manufacturer reference expansion
- Strictly trimmed API responses for agent/tool consumption
- Offline tests with all network access mocked

## Quick start

Requirements:

- Python 3.9 or newer
- Internet access to `cdn.erkul.games` and `scmdb.net`

Install the service and development dependencies from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Start one FastAPI process with one Uvicorn worker:

```bash
.venv/bin/uvicorn data_server.main:app --host 127.0.0.1 --port 8000 --workers 1
```

The server completes startup synchronization before accepting requests.
Open the generated API documentation at:

```text
http://127.0.0.1:8000/docs
```

Verify the service:

```bash
curl http://127.0.0.1:8000/health
```

```json
{
  "status": "ok",
  "current_version": "4.9.0-live.12344265"
}
```

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `DATA_ROOT` | `<repository>/data` | Versioned downloads and SQLite databases |
| `LOG_LEVEL` | `INFO` | Python logging level |

This demo service intentionally has no authentication or authorization. It
supports only the Star Citizen live channel and must run as one FastAPI process
with one Uvicorn worker.

## API

### Health

```text
GET /health
```

### Versions

```text
GET /api/v1/versions
```

Returns versions for which both Erkul and SCMDB have a completed crawl.

### Ship details

```text
GET /api/v1/ships?name=Hammerhead
```

Ship searches use exact, unique substring, then fuzzy suggestion matching.
Details not already present in `erkul_ships` are downloaded and cached with a
per-ship single-flight lock.

Successful, ambiguous, and not-found searches all return HTTP `200` with a
machine-readable `status`:

```json
{
  "status": "found",
  "message": "Ship found with the name: Aegis Hammerhead.",
  "possible_matches": [],
  "ship": {
    "i18n": {},
    "manufacturer": {},
    "precomputed": {},
    "subType": "Vehicle_Spaceship",
    "tags": [],
    "vehicle": {}
  }
}
```

Invalid query parameters return HTTP `400`, upstream detail failures return
HTTP `502`, and SQLite failures return HTTP `500`. Error responses use the
same outer structure with `status: "error"`.

## Manual workflows

Run either source workflow independently:

```bash
.venv/bin/python workflows/erkul_workflow.py
.venv/bin/python workflows/scmdb_workflow.py
```

Force a source refresh without replacing the other source's tables:

```bash
.venv/bin/python workflows/erkul_workflow.py --force
.venv/bin/python workflows/scmdb_workflow.py --force
```

Useful workflow options:

```bash
.venv/bin/python workflows/erkul_workflow.py --output-root /tmp/game-data
.venv/bin/python workflows/scmdb_workflow.py --output-root /tmp/game-data
```

The HTTP service always uses Erkul `LIVE` and SCMDB `live`; alternate workflow
channels are intended only for manual data investigation.

## Runtime model

- Startup runs both source workflows concurrently and fails if either workflow
  fails.
- A version becomes available only when both `erkul` and `scmdb` are marked
  `complete` in the same database.
- If the latest source versions differ, startup falls back to the newest
  complete common version.
- Every request and workflow thread owns its SQLite connection.
- A writer-priority read/write lock prevents writer starvation.
- Per-reference locks serialize lazy loading for the same ship while allowing
  different ships to download concurrently.

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

## Quality checks

Run the same checks used by CI:

```bash
.venv/bin/ruff check data_server workflows tests
.venv/bin/ruff format --check data_server workflows tests
.venv/bin/python -m compileall -q data_server workflows tests
.venv/bin/python -m pytest \
  --cov=data_server \
  --cov=workflows \
  --cov-report=term-missing
.github/scripts/check-repository-hygiene.sh
```

All tests are offline and mock external network requests. GitHub Actions also
runs dependency auditing and CodeQL analysis. The protected `main` branch
requires every CI job and CodeQL to pass.

To inspect a generated database:

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
- The startup workflow seeds Hammerhead's full Erkul blob; other matched ships
  are downloaded and cached lazily through the API.
- SCMDB required files and available optional overlays are stored.
- Workflows can run manually and are also executed during FastAPI startup.
- The HTTP API exposes health, complete versions, and current-version ship
  search/detail responses.

## Related repositories

- [Backend](https://github.com/Ask-The-Verse/backend): Go backend and agent
  runtime
- [Frontend](https://github.com/Ask-The-Verse/frontend): Next.js application
- [Website](https://github.com/Ask-The-Verse/github.AskTheVerse.com): public
  GitHub Pages site

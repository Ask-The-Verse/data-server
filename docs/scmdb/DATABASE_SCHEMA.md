# SCMDB workflow and database schema

## Run

The workflow uses only the Python standard library:

```bash
python3 crawler/workflows/scmdb_workflow.py
```

Useful options:

```bash
python3 crawler/workflows/scmdb_workflow.py --channel ptu
python3 crawler/workflows/scmdb_workflow.py --force
python3 crawler/workflows/scmdb_workflow.py --output-root /path/to/data
```

The script always checks `game-versions.json`. If the newest build for the
selected channel has a completed `scmdb` row in `crawl_runs`, it exits without
downloading or changing tables. `--force` replaces only SCMDB tables.

The default output layout is:

```text
crawler/data/<normalized-version>/
├── game_data.sqlite3
└── scmdb/
    └── raw/       # original JSON files
```

The required versioned files are `merged`, `crafting_blueprints`,
`crafting_items`, `mining_data`, and `mining_equipment`. The workflow also
stores valid `site-settings`, `changelog`, `mema-cache`, `deltas`, `overrides`,
`cig_data_issues`, and `mission-history` files when SCMDB publishes them.
Optional URLs that return the HTML application are ignored.

SCMDB and Erkul versions are normalized to lowercase, so equivalent builds
write to the same `game_data.sqlite3`.

## Shared tables

### `crawl_runs`

| Column | Type | Meaning |
|---|---|---|
| `source` | TEXT PK | `erkul` or `scmdb` |
| `source_version` | TEXT | Version exactly as published by the source |
| `normalized_version` | TEXT | Shared lowercase version key |
| `status` | TEXT | `running`, `complete`, or `failed` |
| `started_at`, `completed_at` | TEXT | UTC ISO-8601 timestamps |
| `raw_directory` | TEXT | Absolute raw download directory |
| `file_count`, `record_count` | INTEGER | Completed run totals |
| `error_message` | TEXT | Last failure, otherwise NULL |

### `source_files`

| Column | Type | Meaning |
|---|---|---|
| `source`, `relative_path` | TEXT composite PK | Source and version-relative file path |
| `url` | TEXT | Download URL |
| `sha256` | TEXT | SHA-256 of the stored bytes |
| `byte_count` | INTEGER | Stored byte count |
| `downloaded_at` | TEXT | UTC ISO-8601 timestamp |

## SCMDB catalog

### `scmdb_table_catalog`

This table makes the generated schema discoverable without inspecting
`sqlite_master`.

| Column | Type | Meaning |
|---|---|---|
| `table_name` | TEXT PK | Generated SQLite table |
| `dataset_name` | TEXT | Source file's logical name |
| `data_type` | TEXT | Top-level JSON key or `records` |
| `record_count` | INTEGER | Rows stored in that table |

## Generated data-table schema

Each top-level JSON type receives its own table:

```text
scmdb_<dataset_name>_<data_type>
```

For example, contracts are in `scmdb_merged_contracts`, crafting recipes are
in `scmdb_crafting_blueprints_blueprints`, and mineable elements are in
`scmdb_mining_data_mineableelements`.

Every generated data table has this schema:

| Column | Type | Meaning |
|---|---|---|
| `record_id` | INTEGER PK | SQLite row id |
| `record_key` | TEXT | Source dictionary key, GUID/id, or ordinal |
| `ordinal` | INTEGER | Original array/object order |
| `name` | TEXT | Extracted display/product/title/name, when present |
| `guid` | TEXT | Extracted guid/id/uuid/className/tag, when present |
| `entity_type` | TEXT | Extracted type/category/missionType/gear/kind |
| `payload_json` | TEXT | Complete source record as canonical JSON |

Every generated table has indexes on `name` and `guid`. Full source objects
remain queryable with SQLite JSON functions, for example:

```sql
SELECT
    name,
    json_extract(payload_json, '$.rewardUEC') AS reward_uec
FROM scmdb_merged_contracts
WHERE name LIKE '%delivery%';
```

## Table inventory

The following tables are generated from each current required dataset. The
table name is `scmdb_<dataset>_<type>` for every listed type; identifiers are
normalized to lowercase SQL names, so `blueprintPools` becomes
`blueprintpools`.

| Dataset | Data types, one table each |
|---|---|
| `game_versions` | `records` |
| `merged` | `availabilityPools`, `blueprintPools`, `cargoManifestPools`, `contracts`, `eventScopes`, `factionRewardsPools`, `factions`, `legacyContracts`, `locationPools`, `partialRewardPayoutPools`, `pyroRegions`, `regions`, `resourcePools`, `scopes`, `shipPools`, `ships`, `version` |
| `crafting_blueprints` | `blueprints`, `dismantle`, `items`, `meta`, `properties`, `resources`, `version` |
| `crafting_items` | `ammoPools`, `damageResistancePools`, `fireModesPools`, `items`, `magazinePools`, `manufacturers`, `meta`, `signaturesPools`, `version` |
| `mining_data` | `clusteringPresets`, `compositions`, `locations`, `meta`, `mineableElements`, `qualityBandBoundaries`, `qualityDistribution`, `refineries`, `refineryProfiles`, `version` |
| `mining_equipment` | `fpsTools`, `gadgets`, `globalParams`, `lasers`, `meta`, `mineableElements`, `modules`, `version` |

Current optional datasets create these tables when their JSON exists:

| Dataset | Data types, one table each |
|---|---|
| `site_settings` | `blueprints_enabled`, `id`, `mema_cache_interval_hours`, `mema_cache_last_refreshed`, `mema_enabled`, `ptu_enabled`, `require_rsi_for_entries`, `scenario_overrides`, `updated_at`, `updated_by` |
| `changelog` | `records` |
| `mema_cache` | `records` |
| `cig_data_issues` | `_comment`, `blueprintPools`, `blueprintRecords`, `entities` |
| `mission_history` | `channel`, `current_version`, `generated_at`, `missions`, `schema`, `versions_seen` |

If SCMDB later adds a new top-level type, the workflow creates its table
automatically and registers it in `scmdb_table_catalog`. If an optional file is
absent, no tables for that dataset are created.

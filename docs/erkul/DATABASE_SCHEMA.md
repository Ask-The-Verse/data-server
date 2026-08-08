# Erkul workflow and database schema

## Run

The workflow uses only the Python standard library:

```bash
python3 crawler/workflows/erkul_workflow.py
```

Useful options:

```bash
python3 crawler/workflows/erkul_workflow.py --branch PTU
python3 crawler/workflows/erkul_workflow.py --force
python3 crawler/workflows/erkul_workflow.py --output-root /path/to/data
```

The script checks `catalog.bin` on every invocation. If the current Erkul
`dataVersion` has a completed `erkul` row in `crawl_runs`, it exits without
downloading or changing tables. `--force` replaces only the Erkul tables.

The default output layout is:

```text
crawler/data/<normalized-version>/
├── game_data.sqlite3
└── erkul/
    ├── raw/       # original raw-DEFLATE .bin files
    └── decoded/   # readable decoded JSON
```

Erkul and SCMDB versions are normalized to lowercase. This lets
`4.9.0-LIVE.12344265` and `4.9.0-live.12344265` share one database.

## Shared tables

### `crawl_runs`

One row per crawler source in this version database.

| Column | Type | Meaning |
|---|---|---|
| `source` | TEXT PK | `erkul` or `scmdb` |
| `source_version` | TEXT | Version exactly as published by the source |
| `normalized_version` | TEXT | Shared lowercase version key |
| `status` | TEXT | `running`, `complete`, or `failed` |
| `started_at` | TEXT | UTC ISO-8601 timestamp |
| `completed_at` | TEXT | UTC completion/failure timestamp |
| `raw_directory` | TEXT | Absolute raw download directory |
| `file_count` | INTEGER | Number of downloaded source files |
| `record_count` | INTEGER | Number of inserted source records |
| `error_message` | TEXT | Last failure, otherwise NULL |

### `source_files`

One row per downloaded source file.

| Column | Type | Meaning |
|---|---|---|
| `source` | TEXT PK/FK | Owner source |
| `relative_path` | TEXT PK | Path relative to the version directory |
| `url` | TEXT | Download URL |
| `sha256` | TEXT | SHA-256 of the stored bytes |
| `byte_count` | INTEGER | Stored byte count |
| `downloaded_at` | TEXT | UTC ISO-8601 timestamp |

## Erkul tables

### `erkul_manifest_resources`

One row for every manifest group, family, single, or patch.

| Column | Type | Meaning |
|---|---|---|
| `resource_type`, `kind` | TEXT composite PK | Manifest section and resource kind |
| `path` | TEXT | CDN path or group index path |
| `sha256` | TEXT | Expected source checksum |
| `item_count` | INTEGER | Manifest item count, when present |
| `byte_count` | INTEGER | Compressed byte count, when present |
| `payload_json` | TEXT | Complete manifest resource object |

### `erkul_ship_catalog`

Contains every lightweight ship and ground-vehicle entry published in Erkul's
`index.<hash>.bin`. Full detail blobs are not required for this table.

| Column | Type | Meaning |
|---|---|---|
| `class_name` | TEXT PK | Stable Erkul vehicle class name |
| `ref` | TEXT | Source UUID/reference |
| `category` | TEXT | `AssembledShip` or `AssembledGroundVehicle` |
| `name`, `short_name`, `display_name` | TEXT | Vehicle names |
| `manufacturer_name` | TEXT | Manufacturer |
| `role`, `career`, `focus` | TEXT | Gameplay classifications |
| `size`, `crew_size` | INTEGER | Vehicle size and crew |
| `mass_kg`, `hp` | REAL | Fixed mass and total HP |
| `cargo_scu`, `storage_scu` | REAL | Cargo and personal storage |
| `shield_type`, `shield_hp` | TEXT/REAL | Shield geometry and total HP |
| `burst_dps`, `pilot_burst_dps` | REAL | Precomputed weapon DPS |
| `scm_speed`, `max_speed` | REAL | Precomputed flight speeds |
| `quantum_range_gm` | REAL | Precomputed quantum range |
| `detail_path` | TEXT | Full-detail blob path from the matching group index |
| `detail_sha256` | TEXT | Expected SHA-256 for the full-detail blob |
| `detail_bytes` | INTEGER | Compressed full-detail blob size |
| `payload_json` | TEXT | Complete lightweight index entry |

Indexes are provided for `category`, `display_name`, and `manufacturer_name`.
The workflow reads both `ships.group.<hash>.bin` and
`groundvehicles.group.<hash>.bin` to populate the detail metadata, but still
downloads only Hammerhead's full-detail blob.

### `erkul_ships`

Contains one row: `aegs_hammerhead_gs`.

| Column | Type | Meaning |
|---|---|---|
| `class_name` | TEXT PK | Erkul ship class name |
| `ref` | TEXT | Source UUID/reference |
| `display_name` | TEXT | Vehicle display name |
| `manufacturer_name` | TEXT | Manufacturer |
| `role`, `career` | TEXT | Lightweight index classifications |
| `size`, `crew_size` | INTEGER | Ship size and crew |
| `mass_kg` | REAL | Total mass |
| `hp` | REAL | Precomputed total HP |
| `cargo_scu` | REAL | Precomputed cargo capacity |
| `payload_json` | TEXT | Complete Hammerhead blob |

### `erkul_ship_slots`

Every node in the Hammerhead recursive slot tree.

| Column | Type | Meaning |
|---|---|---|
| `slot_id` | TEXT PK | Deterministic recursive slot identifier |
| `parent_slot_id` | TEXT FK | Parent slot, NULL for root slots |
| `depth` | INTEGER | Tree depth |
| `slot_kind`, `port_name` | TEXT | Erkul slot metadata |
| `port_path_json` | TEXT | Source port path array |
| `item_category`, `item_class_name` | TEXT | Installed item summary |
| `minimum_size`, `maximum_size` | INTEGER | Hardpoint size constraints |
| `payload_json` | TEXT | Complete slot object |

### `erkul_default_components`

One row for each installed item found while walking `erkul_ship_slots`.

| Column | Type | Meaning |
|---|---|---|
| `component_id` | TEXT PK | Same deterministic id as its slot |
| `slot_id` | TEXT FK | Owning slot |
| `category`, `family` | TEXT | Source category and normalized family |
| `class_name`, `name` | TEXT | Component identifiers |
| `manufacturer_name` | TEXT | Manufacturer |
| `size` | INTEGER | Component size |
| `grade` | TEXT | Component grade |
| `payload_json` | TEXT | Complete installed item object |

### `erkul_family_<kind>`

The workflow creates a separate table for every family in the live manifest:

```text
erkul_family_blades
erkul_family_bombs
erkul_family_coolers
erkul_family_emps
erkul_family_jumpdrives
erkul_family_manufacturers
erkul_family_mininglasers
erkul_family_missileracks
erkul_family_missiles
erkul_family_modules
erkul_family_mounts
erkul_family_paints
erkul_family_powerplants
erkul_family_qeds
erkul_family_quantumdrives
erkul_family_radars
erkul_family_rocketpods
erkul_family_salvageheads
erkul_family_shields
erkul_family_tractorbeams
erkul_family_turrets
erkul_family_utilities
erkul_family_weapons
```

All family tables have the same schema:

| Column | Type | Meaning |
|---|---|---|
| `record_id` | INTEGER PK | SQLite row id |
| `record_key` | TEXT | Source class/ref key; duplicates are preserved |
| `class_name`, `ref` | TEXT | Source identifiers |
| `category`, `name` | TEXT | Component classification/display name |
| `manufacturer_name` | TEXT | Manufacturer |
| `size` | INTEGER | Component size |
| `grade` | TEXT | Component grade |
| `payload_json` | TEXT | Complete family item object |

`payload_json` is intentionally retained in every data table so no source
field is lost when Erkul adds attributes that do not yet have scalar columns.

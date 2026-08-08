# Crawler workflows

Executable crawler workflows and shared runtime code live in this directory.

```bash
python3 crawler/workflows/erkul_workflow.py
python3 crawler/workflows/scmdb_workflow.py
```

Both workflows write to the shared versioned output root:

```text
crawler/data/<normalized-version>/game_data.sqlite3
```

- `erkul_workflow.py`: Erkul version discovery, downloads, the complete
  lightweight ship catalogue, Hammerhead detail parsing, and component-family
  tables.
- `scmdb_workflow.py`: SCMDB version discovery, downloads, and one SQLite table
  per source data type.
- `game_data_common.py`: shared download, hashing, version, and SQLite helpers.

Detailed schemas remain in:

- [`../docs/erkul/DATABASE_SCHEMA.md`](../docs/erkul/DATABASE_SCHEMA.md)
- [`../docs/scmdb/DATABASE_SCHEMA.md`](../docs/scmdb/DATABASE_SCHEMA.md)

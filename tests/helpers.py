from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple


def create_version_database(
    data_root: Path,
    version: str,
    statuses: Iterable[Tuple[str, str]] = (
        ("erkul", "complete"),
        ("scmdb", "complete"),
    ),
) -> Path:
    version_directory = data_root / version
    version_directory.mkdir(parents=True, exist_ok=True)
    path = version_directory / "game_data.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE crawl_runs (
            source TEXT PRIMARY KEY,
            source_version TEXT NOT NULL,
            normalized_version TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            raw_directory TEXT NOT NULL,
            file_count INTEGER NOT NULL DEFAULT 0,
            record_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT
        );

        CREATE TABLE source_files (
            source TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            url TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            byte_count INTEGER NOT NULL,
            downloaded_at TEXT NOT NULL,
            PRIMARY KEY (source, relative_path),
            FOREIGN KEY (source) REFERENCES crawl_runs(source)
        );

        CREATE TABLE erkul_ship_catalog (
            class_name TEXT PRIMARY KEY,
            ref TEXT,
            name TEXT NOT NULL,
            short_name TEXT,
            display_name TEXT,
            detail_path TEXT NOT NULL,
            detail_sha256 TEXT NOT NULL
        );

        CREATE TABLE erkul_ships (
            class_name TEXT PRIMARY KEY,
            ref TEXT,
            display_name TEXT NOT NULL,
            manufacturer_name TEXT,
            role TEXT,
            career TEXT,
            size INTEGER,
            crew_size INTEGER,
            mass_kg REAL,
            hp REAL,
            cargo_scu REAL,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE erkul_family_manufacturers (
            record_id INTEGER PRIMARY KEY,
            record_key TEXT NOT NULL,
            class_name TEXT,
            ref TEXT,
            payload_json TEXT NOT NULL
        );
        """
    )
    for source, status in statuses:
        connection.execute(
            """
            INSERT INTO crawl_runs (
                source, source_version, normalized_version, status, started_at,
                completed_at, raw_directory, file_count, record_count
            )
            VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z',
                    '2026-01-01T00:00:01Z', ?, 7, 11)
            """,
            (source, version, version, status, str(version_directory / source)),
        )
    connection.commit()
    connection.close()
    return path


def insert_catalog_ship(
    path: Path,
    *,
    class_name: str,
    ref: str,
    name: str,
    detail_path: str = "ships/detail.bin",
    detail_sha256: str = "unused",
    short_name: str = "",
    display_name: str = "",
) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO erkul_ship_catalog (
            class_name, ref, name, short_name, display_name,
            detail_path, detail_sha256
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            class_name,
            ref,
            name,
            short_name or None,
            display_name or None,
            detail_path,
            detail_sha256,
        ),
    )
    connection.commit()
    connection.close()


def insert_cached_ship(
    path: Path,
    *,
    class_name: str,
    ref: str,
    display_name: str,
    payload: Dict[str, Any],
) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO erkul_ships (
            class_name, ref, display_name, payload_json
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            class_name,
            ref,
            display_name,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
        ),
    )
    connection.commit()
    connection.close()


def insert_manufacturer(
    path: Path,
    *,
    record_key: str,
    ref: str,
    class_name: str,
    name: str,
) -> Dict[str, Any]:
    payload = {
        "ref": ref,
        "className": class_name,
        "i18n": {"name": name},
        "category": "Manufacturer",
    }
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO erkul_family_manufacturers (
            record_key, class_name, ref, payload_json
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            record_key,
            class_name,
            ref,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
        ),
    )
    connection.commit()
    connection.close()
    return payload


def raw_deflate_json(payload: Dict[str, Any]) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    content = json.dumps(payload).encode("utf-8")
    return compressor.compress(content) + compressor.flush()

#!/usr/bin/env python3
"""Shared helpers for versioned Star Citizen crawler databases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


USER_AGENT = "AskTheVerse-game-data-crawler/1.0"
IDENTIFIER_RE = re.compile(r"[^a-z0-9]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_version(version: str) -> str:
    """Normalize equivalent source versions to one directory/database key."""
    normalized = version.strip().lower()
    normalized = re.sub(r"[^a-z0-9._-]+", "-", normalized)
    return normalized.strip("-")


def safe_identifier(value: str) -> str:
    identifier = IDENTIFIER_RE.sub("_", value.lower()).strip("_")
    if not identifier:
        raise ValueError(f"Cannot create a SQL identifier from {value!r}")
    if identifier[0].isdigit():
        identifier = f"t_{identifier}"
    return identifier


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def display_name(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    for key in (
        "displayName",
        "productName",
        "title",
        "name",
        "label",
        "className",
        "tag",
    ):
        candidate = value.get(key)
        if candidate is not None:
            return str(candidate)
    return None


def version_paths(output_root: Path, version: str) -> tuple[Path, Path]:
    version_dir = output_root / normalize_version(version)
    return version_dir, version_dir / "game_data.sqlite3"


def connect_database(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS crawl_runs (
            source TEXT PRIMARY KEY,
            source_version TEXT NOT NULL,
            normalized_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            raw_directory TEXT NOT NULL,
            file_count INTEGER NOT NULL DEFAULT 0,
            record_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS source_files (
            source TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            url TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            byte_count INTEGER NOT NULL,
            downloaded_at TEXT NOT NULL,
            PRIMARY KEY (source, relative_path),
            FOREIGN KEY (source) REFERENCES crawl_runs(source) ON DELETE CASCADE
        );
        """
    )
    return connection


def source_is_complete(
    connection: sqlite3.Connection, source: str, source_version: str
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM crawl_runs
        WHERE source = ? AND source_version = ? AND status = 'complete'
        """,
        (source, source_version),
    ).fetchone()
    return row is not None


def begin_source_run(
    connection: sqlite3.Connection,
    source: str,
    source_version: str,
    raw_directory: Path,
) -> None:
    connection.execute(
        """
        INSERT INTO crawl_runs (
            source, source_version, normalized_version, status, started_at,
            completed_at, raw_directory, file_count, record_count, error_message
        )
        VALUES (?, ?, ?, 'running', ?, NULL, ?, 0, 0, NULL)
        ON CONFLICT(source) DO UPDATE SET
            source_version = excluded.source_version,
            normalized_version = excluded.normalized_version,
            status = 'running',
            started_at = excluded.started_at,
            completed_at = NULL,
            raw_directory = excluded.raw_directory,
            file_count = 0,
            record_count = 0,
            error_message = NULL
        """,
        (
            source,
            source_version,
            normalize_version(source_version),
            utc_now(),
            str(raw_directory),
        ),
    )
    connection.execute("DELETE FROM source_files WHERE source = ?", (source,))


def complete_source_run(
    connection: sqlite3.Connection, source: str, file_count: int, record_count: int
) -> None:
    connection.execute(
        """
        UPDATE crawl_runs
        SET status = 'complete', completed_at = ?, file_count = ?, record_count = ?,
            error_message = NULL
        WHERE source = ?
        """,
        (utc_now(), file_count, record_count, source),
    )


def fail_source_run(
    connection: sqlite3.Connection, source: str, error_message: str
) -> None:
    connection.execute(
        """
        UPDATE crawl_runs
        SET status = 'failed', completed_at = ?, error_message = ?
        WHERE source = ?
        """,
        (utc_now(), error_message[:4000], source),
    )


def drop_tables(connection: sqlite3.Connection, prefix: str) -> None:
    prefix = safe_identifier(prefix)
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name LIKE ? ESCAPE '\\'
        """,
        (prefix.replace("_", r"\_") + r"\_%",),
    ).fetchall()
    pending = {name for (name,) in rows}
    while pending:
        referenced = set()
        for child in pending:
            foreign_keys = connection.execute(
                f'PRAGMA foreign_key_list("{child}")'
            ).fetchall()
            referenced.update(
                row[2]
                for row in foreign_keys
                if row[2] in pending and row[2] != child
            )

        droppable = pending - referenced
        if not droppable:
            raise RuntimeError(
                f"Cannot drop tables with cyclic foreign keys: {sorted(pending)}"
            )
        for name in sorted(droppable):
            connection.execute(f'DROP TABLE "{name}"')
            pending.remove(name)


def http_get(url: str, timeout: int = 120, attempts: int = 4) -> bytes:
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json,application/octet-stream,*/*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if 400 <= error.code < 500:
                raise
            last_error = error
            if attempt == attempts - 1:
                break
            time.sleep(2**attempt)
        except (OSError, TimeoutError) as error:
            last_error = error
            if attempt == attempts - 1:
                break
            time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def record_source_file(
    connection: sqlite3.Connection,
    source: str,
    version_directory: Path,
    path: Path,
    url: str,
    content: bytes,
) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO source_files (
            source, relative_path, url, sha256, byte_count, downloaded_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            str(path.relative_to(version_directory)),
            url,
            sha256_bytes(content),
            len(content),
            utc_now(),
        ),
    )


def collection_records(value: Any) -> Iterable[tuple[Optional[str], int, Any]]:
    """Yield stable records while keeping object-shaped metadata intact."""
    if isinstance(value, list):
        for ordinal, item in enumerate(value):
            yield None, ordinal, item
        return

    if isinstance(value, dict) and value:
        complex_values = sum(isinstance(item, (dict, list)) for item in value.values())
        if complex_values >= max(1, len(value) // 2):
            for ordinal, (key, item) in enumerate(value.items()):
                yield str(key), ordinal, item
            return

    yield None, 0, value

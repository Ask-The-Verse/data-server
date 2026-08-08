#!/usr/bin/env python3
"""Download the current SCMDB dataset and load each data type into SQLite."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
from pathlib import Path
from typing import Any, Optional


CRAWLER_ROOT = Path(__file__).resolve().parents[1]
if str(CRAWLER_ROOT) not in sys.path:
    sys.path.insert(0, str(CRAWLER_ROOT))

from game_data_common import (  # noqa: E402
    atomic_write,
    begin_source_run,
    canonical_json,
    collection_records,
    complete_source_run,
    connect_database,
    display_name,
    drop_tables,
    fail_source_run,
    http_get,
    record_source_file,
    safe_identifier,
    source_is_complete,
    version_paths,
)


BASE_URL = "https://scmdb.net/data"
SOURCE = "scmdb"


def channel_of(version: str) -> str:
    lowered = version.lower()
    return "ptu" if "-ptu." in lowered or "-ptu-" in lowered else "live"


def parse_json(content: bytes, filename: str) -> Any:
    text = content.decode("utf-8-sig")
    if text.lstrip().lower().startswith("<!doctype html"):
        raise ValueError(f"{filename} returned the SCMDB HTML application, not JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{filename} is not valid JSON: {error}") from error


def extract_guid(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    for key in ("guid", "id", "uuid", "className", "tag"):
        candidate = value.get(key)
        if candidate is not None:
            return str(candidate)
    return None


def extract_type(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    for key in ("type", "category", "missionType", "gear", "kind"):
        candidate = value.get(key)
        if candidate is not None:
            return str(candidate)
    return None


class ScmdbWorkflow:
    def __init__(self, channel: str, output_root: Path, force: bool, timeout: int):
        self.channel = channel.lower()
        self.output_root = output_root
        self.force = force
        self.timeout = timeout
        self.file_count = 0
        self.record_count = 0
        self.version_directory: Optional[Path] = None
        self.raw_directory: Optional[Path] = None
        self.connection: Optional[sqlite3.Connection] = None

    @staticmethod
    def source_url(filename: str, cache_bust: bool = False) -> str:
        url = f"{BASE_URL}/{filename}"
        if cache_bust:
            # A fixed unique query is enough to avoid a stale intermediary cache.
            from time import time_ns

            url = f"{url}?t={time_ns() // 1_000_000}"
        return url

    def fetch_json(
        self, filename: str, *, cache_bust: bool = False
    ) -> tuple[str, bytes, Any]:
        url = self.source_url(filename, cache_bust=cache_bust)
        content = http_get(url, timeout=self.timeout)
        return url, content, parse_json(content, filename)

    def save_download(
        self, filename: str, url: str, content: bytes
    ) -> None:
        assert self.raw_directory is not None
        assert self.version_directory is not None
        assert self.connection is not None

        destination = self.raw_directory / filename
        atomic_write(destination, content)
        record_source_file(
            self.connection,
            SOURCE,
            self.version_directory,
            destination,
            url,
            content,
        )
        self.file_count += 1
        print(f"downloaded {url} -> {destination}")

    def run(self) -> int:
        versions_url, versions_content, versions = self.fetch_json(
            "game-versions.json", cache_bust=True
        )
        if not isinstance(versions, list):
            raise TypeError("SCMDB game-versions.json did not decode to a list")
        version_entry = next(
            (
                entry
                for entry in versions
                if isinstance(entry, dict)
                and channel_of(str(entry.get("version", ""))) == self.channel
            ),
            None,
        )
        if not version_entry:
            raise RuntimeError(f"No SCMDB {self.channel} version is available")

        source_version = str(version_entry["version"])
        version_directory, db_path = version_paths(self.output_root, source_version)
        connection = connect_database(db_path)
        self.connection = connection
        self.version_directory = version_directory
        self.raw_directory = version_directory / SOURCE / "raw"

        if not self.force and source_is_complete(connection, SOURCE, source_version):
            print(
                f"SCMDB {source_version} has already been crawled; "
                f"leaving {db_path} unchanged."
            )
            connection.close()
            return 0

        begin_source_run(connection, SOURCE, source_version, self.raw_directory)
        connection.commit()

        datasets: list[tuple[str, Any]] = []
        try:
            self.save_download("game-versions.json", versions_url, versions_content)
            datasets.append(("game_versions", versions))

            merged_filename = str(
                version_entry.get("file") or f"merged-{source_version}.json"
            )
            required = [
                ("merged", merged_filename),
                (
                    "crafting_blueprints",
                    f"crafting_blueprints-{source_version}.json",
                ),
                ("crafting_items", f"crafting_items-{source_version}.json"),
                ("mining_data", f"mining_data-{source_version}.json"),
                ("mining_equipment", f"mining_equipment-{source_version}.json"),
            ]
            optional = [
                ("site_settings", "site-settings.json"),
                ("changelog", "changelog.json"),
                ("mema_cache", "mema-cache.json"),
                ("deltas", f"deltas-{source_version}.json"),
                ("overrides", f"overrides-{source_version}.json"),
                ("cig_data_issues", f"cig_data_issues-{source_version}.json"),
                ("mission_history", f"mission-history-{source_version}.json"),
            ]

            for dataset_name, filename in required:
                url, content, data = self.fetch_json(filename)
                self.save_download(filename, url, content)
                datasets.append((dataset_name, data))

            for dataset_name, filename in optional:
                try:
                    url, content, data = self.fetch_json(filename)
                except (urllib.error.HTTPError, ValueError) as error:
                    print(f"optional SCMDB file skipped: {filename} ({error})")
                    continue
                self.save_download(filename, url, content)
                datasets.append((dataset_name, data))

            with connection:
                drop_tables(connection, "scmdb")
                self.create_catalog(connection)
                for dataset_name, data in datasets:
                    self.insert_dataset(connection, dataset_name, data)
                complete_source_run(
                    connection, SOURCE, self.file_count, self.record_count
                )
        except BaseException as error:
            connection.rollback()
            fail_source_run(connection, SOURCE, str(error))
            connection.commit()
            raise
        finally:
            connection.close()

        print(
            f"SCMDB {source_version}: {self.file_count} files and "
            f"{self.record_count} database records written to {db_path}"
        )
        return 0

    @staticmethod
    def create_catalog(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE scmdb_table_catalog (
                table_name TEXT PRIMARY KEY,
                dataset_name TEXT NOT NULL,
                data_type TEXT NOT NULL,
                record_count INTEGER NOT NULL
            )
            """
        )

    def insert_dataset(
        self, connection: sqlite3.Connection, dataset_name: str, data: Any
    ) -> None:
        if isinstance(data, dict):
            for data_type, value in data.items():
                self.insert_data_type(
                    connection, dataset_name, str(data_type), value
                )
        else:
            self.insert_data_type(connection, dataset_name, "records", data)

    def insert_data_type(
        self,
        connection: sqlite3.Connection,
        dataset_name: str,
        data_type: str,
        value: Any,
    ) -> None:
        table = (
            f"scmdb_{safe_identifier(dataset_name)}_{safe_identifier(data_type)}"
        )
        connection.execute(
            f"""
            CREATE TABLE "{table}" (
                record_id INTEGER PRIMARY KEY,
                record_key TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                name TEXT,
                guid TEXT,
                entity_type TEXT,
                payload_json TEXT NOT NULL
            )
            """
        )

        count = 0
        for source_key, ordinal, item in collection_records(value):
            guid = extract_guid(item)
            key = source_key or guid or str(ordinal)
            connection.execute(
                f"""
                INSERT INTO "{table}" (
                    record_key, ordinal, name, guid, entity_type, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    ordinal,
                    display_name(item),
                    guid,
                    extract_type(item),
                    canonical_json(item),
                ),
            )
            count += 1

        connection.execute(
            f'CREATE INDEX "{table}_name_idx" ON "{table}" (name)'
        )
        connection.execute(
            f'CREATE INDEX "{table}_guid_idx" ON "{table}" (guid)'
        )
        connection.execute(
            """
            INSERT INTO scmdb_table_catalog (
                table_name, dataset_name, data_type, record_count
            )
            VALUES (?, ?, ?, ?)
            """,
            (table, dataset_name, data_type, count),
        )
        self.record_count += count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel",
        choices=("live", "ptu"),
        default="live",
        help="SCMDB channel (default: live)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=CRAWLER_ROOT / "data",
        help="Root directory for versioned downloads and databases",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download and replace SCMDB tables for the current version",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="HTTP timeout in seconds (default: 120)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workflow = ScmdbWorkflow(
        channel=args.channel,
        output_root=args.output_root.resolve(),
        force=args.force,
        timeout=args.timeout,
    )
    return workflow.run()


if __name__ == "__main__":
    raise SystemExit(main())

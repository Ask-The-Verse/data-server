#!/usr/bin/env python3
"""Download the current Erkul dataset and load Hammerhead data into SQLite."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import zlib
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Optional

CRAWLER_ROOT = Path(__file__).resolve().parents[1]
if str(CRAWLER_ROOT) not in sys.path:
    sys.path.insert(0, str(CRAWLER_ROOT))

try:
    from .game_data_common import (
        atomic_write,
        begin_source_run,
        canonical_json,
        complete_source_run,
        connect_database,
        display_name,
        drop_tables,
        fail_source_run,
        http_get,
        record_source_file,
        safe_identifier,
        sha256_bytes,
        source_is_complete,
        version_paths,
    )
except ImportError:
    from game_data_common import (  # type: ignore[no-redef]
        atomic_write,
        begin_source_run,
        canonical_json,
        complete_source_run,
        connect_database,
        display_name,
        drop_tables,
        fail_source_run,
        http_get,
        record_source_file,
        safe_identifier,
        sha256_bytes,
        source_is_complete,
        version_paths,
    )


BASE_URL = "https://cdn.erkul.games"
SOURCE = "erkul"
TARGET_SHIP = "hammerhead"
TARGET_CLASS_NAME = "aegs_hammerhead_gs"


def decode_bin(content: bytes) -> Any:
    text = zlib.decompress(content, wbits=-zlib.MAX_WBITS).decode("utf-8")
    return json.loads(text)


def nested_name(value: Any) -> Optional[str]:
    direct = display_name(value)
    if direct:
        return direct
    if isinstance(value, dict):
        i18n = value.get("i18n")
        if isinstance(i18n, dict):
            return display_name(i18n)
    return None


def manufacturer_name(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    manufacturer = value.get("manufacturer")
    if isinstance(manufacturer, dict):
        return nested_name(manufacturer)
    if manufacturer is not None:
        return str(manufacturer)
    candidate = value.get("manufacturerName")
    return str(candidate) if candidate is not None else None


def numeric_value(value: Any, *preferred_keys: str) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, dict):
        for key in preferred_keys:
            candidate = value.get(key)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                return candidate
    return None


class ErkulWorkflow:
    def __init__(
        self,
        branch: str,
        output_root: Path,
        force: bool,
        timeout: int,
        database_lock: Optional[Any] = None,
    ):
        self.branch = branch.upper()
        self.output_root = output_root
        self.force = force
        self.timeout = timeout
        self.database_lock = database_lock
        self.file_count = 0
        self.record_count = 0
        self.source_version: Optional[str] = None
        self.version_directory: Optional[Path] = None
        self.raw_directory: Optional[Path] = None
        self.decoded_directory: Optional[Path] = None
        self.connection: Optional[sqlite3.Connection] = None

    def read_guard(self) -> Any:
        if self.database_lock is None:
            return nullcontext()
        return self.database_lock.read_lock()

    def write_guard(self) -> Any:
        if self.database_lock is None:
            return nullcontext()
        return self.database_lock.write_lock()

    def source_url(self, path: str, branch_scoped: bool = True) -> str:
        if branch_scoped:
            return f"{BASE_URL}/{self.branch}/{path}"
        return f"{BASE_URL}/{path}"

    def save_download(
        self,
        path: str,
        content: bytes,
        *,
        url: str,
        decoded: Any,
        expected_sha256: Optional[str] = None,
        action: str = "downloaded",
    ) -> None:
        assert self.raw_directory is not None
        assert self.decoded_directory is not None
        assert self.version_directory is not None
        assert self.connection is not None

        actual_sha256 = sha256_bytes(content)
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise ValueError(
                f"SHA-256 mismatch for {path}: expected {expected_sha256}, "
                f"received {actual_sha256}"
            )

        raw_path = self.raw_directory / path
        decoded_path = self.decoded_directory / f"{path}.json"
        atomic_write(raw_path, content)
        atomic_write(
            decoded_path,
            json.dumps(decoded, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        with self.write_guard():
            record_source_file(
                self.connection,
                SOURCE,
                self.version_directory,
                raw_path,
                url,
                content,
            )
            self.connection.commit()
        self.file_count += 1
        print(f"{action} {url} -> {raw_path}")

    def fetch_decoded(
        self,
        path: str,
        *,
        expected_sha256: Optional[str] = None,
        branch_scoped: bool = True,
    ) -> Any:
        url = self.source_url(path, branch_scoped=branch_scoped)
        if self.raw_directory is not None and expected_sha256:
            local_path = self.raw_directory / path
            if local_path.is_file():
                local_content = local_path.read_bytes()
                if sha256_bytes(local_content) == expected_sha256:
                    decoded = decode_bin(local_content)
                    self.save_download(
                        path,
                        local_content,
                        url=url,
                        decoded=decoded,
                        expected_sha256=expected_sha256,
                        action="reused",
                    )
                    return decoded
        content = http_get(url, timeout=self.timeout)
        decoded = decode_bin(content)
        self.save_download(
            path,
            content,
            url=url,
            decoded=decoded,
            expected_sha256=expected_sha256,
        )
        return decoded

    def run(self) -> int:
        status_content = http_get(self.source_url("status.bin", False), self.timeout)
        status = decode_bin(status_content)
        branch_status = status.get(self.branch, {}).get("status")

        catalog_content = http_get(self.source_url("catalog.bin"), self.timeout)
        manifest = decode_bin(catalog_content)
        source_version = str(manifest["dataVersion"])
        self.source_version = source_version
        version_directory, db_path = version_paths(self.output_root, source_version)

        with self.write_guard():
            connection = connect_database(db_path)
        self.connection = connection
        self.version_directory = version_directory
        self.raw_directory = version_directory / SOURCE / "raw"
        self.decoded_directory = version_directory / SOURCE / "decoded"

        with self.read_guard():
            already_complete = source_is_complete(connection, SOURCE, source_version)
        if not self.force and already_complete:
            print(
                f"Erkul {source_version} has already been crawled; "
                f"leaving {db_path} unchanged."
            )
            connection.close()
            return 0

        if branch_status != "open":
            connection.close()
            raise RuntimeError(
                f"Erkul branch {self.branch} is not open: {branch_status!r}"
            )

        with self.write_guard():
            begin_source_run(connection, SOURCE, source_version, self.raw_directory)
            connection.commit()

        try:
            self.save_download(
                "status.bin",
                status_content,
                url=self.source_url("status.bin", False),
                decoded=status,
            )
            self.save_download(
                "catalog.bin",
                catalog_content,
                url=self.source_url("catalog.bin"),
                decoded=manifest,
            )

            singles: dict[str, Any] = {}
            for resource in manifest.get("singles", []):
                singles[resource["kind"]] = self.fetch_decoded(
                    resource["path"], expected_sha256=resource.get("sha256")
                )

            ship_index = singles.get("index")
            if not isinstance(ship_index, dict):
                raise RuntimeError("Erkul catalog does not contain a usable ship index")
            target = self.select_hammerhead(ship_index.get("ships", []))

            vehicle_blobs: dict[str, dict[str, Any]] = {}
            for group in manifest.get("groups", []):
                if group.get("kind") not in ("ships", "groundvehicles"):
                    continue
                group_index = self.fetch_decoded(
                    group["indexPath"],
                    expected_sha256=group.get("indexSha256"),
                )
                for item in group_index.get("blobs", []):
                    if isinstance(item, dict) and item.get("id"):
                        vehicle_blobs[str(item["id"])] = item

            blob = vehicle_blobs.get(str(target["className"]))
            if not blob:
                raise RuntimeError(
                    f"No detail path found for Hammerhead {target['className']}"
                )
            ship = self.fetch_decoded(blob["path"], expected_sha256=blob.get("sha256"))

            family_data: dict[str, list[Any]] = {}
            for family in manifest.get("families", []):
                values = self.fetch_decoded(
                    family["path"], expected_sha256=family.get("sha256")
                )
                if not isinstance(values, list):
                    raise TypeError(f"Family {family['kind']} did not decode to a list")
                family_data[family["kind"]] = values

            with self.write_guard():
                with connection:
                    drop_tables(connection, "erkul")
                    self.create_schema(connection, family_data)
                    self.insert_manifest(connection, manifest)
                    self.insert_ship_catalog(
                        connection, ship_index.get("ships", []), vehicle_blobs
                    )
                    self.insert_ship(connection, target, ship)
                    self.insert_slots(connection, ship.get("slots", []))
                    self.insert_families(connection, family_data)
                    complete_source_run(
                        connection, SOURCE, self.file_count, self.record_count
                    )
        except BaseException as error:
            with self.write_guard():
                connection.rollback()
                fail_source_run(connection, SOURCE, str(error))
                connection.commit()
            raise
        finally:
            connection.close()

        print(
            f"Erkul {source_version}: {self.file_count} files and "
            f"{self.record_count} database records written to {db_path}"
        )
        return 0

    @staticmethod
    def select_hammerhead(ships: list[Any]) -> dict[str, Any]:
        candidates = [
            ship
            for ship in ships
            if isinstance(ship, dict)
            and any(
                TARGET_SHIP in str(ship.get(key, "")).lower()
                for key in (
                    "displayName",
                    "name",
                    "className",
                    "manufacturerName",
                    "role",
                    "career",
                )
            )
        ]
        if not candidates:
            raise RuntimeError("Hammerhead was not found in the Erkul ship index")
        candidates.sort(
            key=lambda ship: (
                ship.get("className") != TARGET_CLASS_NAME,
                str(ship.get("displayName") or ship.get("name") or ""),
            )
        )
        return candidates[0]

    @staticmethod
    def create_schema(
        connection: sqlite3.Connection, family_data: dict[str, list[Any]]
    ) -> None:
        connection.executescript(
            """
            CREATE TABLE erkul_manifest_resources (
                resource_type TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT,
                sha256 TEXT,
                item_count INTEGER,
                byte_count INTEGER,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (resource_type, kind)
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

            CREATE TABLE erkul_ship_catalog (
                class_name TEXT PRIMARY KEY,
                ref TEXT,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                short_name TEXT,
                display_name TEXT,
                manufacturer_name TEXT,
                role TEXT,
                career TEXT,
                focus TEXT,
                size INTEGER,
                crew_size INTEGER,
                mass_kg REAL,
                hp REAL,
                cargo_scu REAL,
                storage_scu REAL,
                shield_type TEXT,
                shield_hp REAL,
                burst_dps REAL,
                pilot_burst_dps REAL,
                scm_speed REAL,
                max_speed REAL,
                quantum_range_gm REAL,
                detail_path TEXT NOT NULL,
                detail_sha256 TEXT NOT NULL,
                detail_bytes INTEGER,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX erkul_ship_catalog_category_idx
                ON erkul_ship_catalog (category);
            CREATE INDEX erkul_ship_catalog_display_name_idx
                ON erkul_ship_catalog (display_name);
            CREATE INDEX erkul_ship_catalog_manufacturer_idx
                ON erkul_ship_catalog (manufacturer_name);

            CREATE TABLE erkul_ship_slots (
                slot_id TEXT PRIMARY KEY,
                parent_slot_id TEXT,
                depth INTEGER NOT NULL,
                slot_kind TEXT,
                port_name TEXT,
                port_path_json TEXT,
                item_category TEXT,
                item_class_name TEXT,
                minimum_size INTEGER,
                maximum_size INTEGER,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (parent_slot_id) REFERENCES erkul_ship_slots(slot_id)
            );

            CREATE TABLE erkul_default_components (
                component_id TEXT PRIMARY KEY,
                slot_id TEXT NOT NULL,
                category TEXT,
                family TEXT,
                class_name TEXT,
                name TEXT,
                manufacturer_name TEXT,
                size INTEGER,
                grade TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (slot_id) REFERENCES erkul_ship_slots(slot_id)
            );
            """
        )
        for kind in family_data:
            table = f"erkul_family_{safe_identifier(kind)}"
            connection.execute(
                f"""
                CREATE TABLE "{table}" (
                    record_id INTEGER PRIMARY KEY,
                    record_key TEXT NOT NULL,
                    class_name TEXT,
                    ref TEXT,
                    category TEXT,
                    name TEXT,
                    manufacturer_name TEXT,
                    size INTEGER,
                    grade TEXT,
                    payload_json TEXT NOT NULL
                )
                """
            )

    def insert_manifest(
        self, connection: sqlite3.Connection, manifest: dict[str, Any]
    ) -> None:
        for resource_type in ("groups", "families", "singles", "patches"):
            for ordinal, resource in enumerate(manifest.get(resource_type, [])):
                kind = str(resource.get("kind") or resource.get("id") or ordinal)
                path = resource.get("path") or resource.get("indexPath")
                sha256 = resource.get("sha256") or resource.get("indexSha256")
                connection.execute(
                    """
                    INSERT INTO erkul_manifest_resources (
                        resource_type, kind, path, sha256, item_count, byte_count,
                        payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resource_type,
                        kind,
                        path,
                        sha256,
                        resource.get("count"),
                        resource.get("bytes") or resource.get("totalBytes"),
                        canonical_json(resource),
                    ),
                )
                self.record_count += 1

    def insert_ship_catalog(
        self,
        connection: sqlite3.Connection,
        ships: list[Any],
        vehicle_blobs: dict[str, dict[str, Any]],
    ) -> None:
        for ship in ships:
            if not isinstance(ship, dict):
                continue
            class_name = ship.get("className")
            if not class_name:
                raise ValueError("Erkul ship catalogue entry is missing className")
            blob = vehicle_blobs.get(str(class_name))
            if not blob:
                raise ValueError(
                    f"Erkul ship catalogue entry has no detail path: {class_name}"
                )

            dps = ship.get("dps") if isinstance(ship.get("dps"), dict) else {}
            flight = ship.get("flight") if isinstance(ship.get("flight"), dict) else {}
            quantum = (
                ship.get("quantum") if isinstance(ship.get("quantum"), dict) else {}
            )
            connection.execute(
                """
                INSERT INTO erkul_ship_catalog (
                    class_name, ref, category, name, short_name, display_name,
                    manufacturer_name, role, career, focus, size, crew_size,
                    mass_kg, hp, cargo_scu, storage_scu, shield_type, shield_hp,
                    burst_dps, pilot_burst_dps, scm_speed, max_speed,
                    quantum_range_gm, detail_path, detail_sha256, detail_bytes,
                    payload_json
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    class_name,
                    ship.get("ref"),
                    ship.get("category") or "Unknown",
                    ship.get("name") or class_name,
                    ship.get("shortName"),
                    ship.get("displayName"),
                    ship.get("manufacturerName"),
                    ship.get("role"),
                    ship.get("career"),
                    ship.get("focus"),
                    ship.get("size"),
                    ship.get("crewSize"),
                    numeric_value(ship.get("massFixedKg"), "kg", "value"),
                    numeric_value(ship.get("hp"), "total", "value"),
                    numeric_value(ship.get("cargo"), "scu", "value"),
                    numeric_value(ship.get("storage"), "scu", "value"),
                    ship.get("shield"),
                    numeric_value(ship.get("shieldHp"), "total", "value"),
                    numeric_value(dps.get("burst"), "total", "value"),
                    numeric_value(dps.get("pilotBurst"), "total", "value"),
                    numeric_value(flight.get("scmSpeed"), "value"),
                    numeric_value(flight.get("maxSpeed"), "value"),
                    numeric_value(quantum.get("rangeGm"), "value"),
                    blob["path"],
                    blob["sha256"],
                    blob.get("bytes"),
                    canonical_json(ship),
                ),
            )
            self.record_count += 1

    def insert_ship(
        self,
        connection: sqlite3.Connection,
        light_ship: dict[str, Any],
        ship: dict[str, Any],
    ) -> None:
        vehicle = ship.get("vehicle") or {}
        precomputed = ship.get("precomputed") or {}
        display = (
            vehicle.get("vehicleDisplayName")
            or light_ship.get("displayName")
            or light_ship.get("name")
            or ship["className"]
        )
        hp = numeric_value(precomputed.get("hp"), "total", "value")
        cargo = numeric_value(precomputed.get("cargo"), "scu", "total", "value")
        mass = numeric_value(vehicle.get("totalMass"), "kg", "total", "value")
        connection.execute(
            """
            INSERT INTO erkul_ships (
                class_name, ref, display_name, manufacturer_name, role, career,
                size, crew_size, mass_kg, hp, cargo_scu, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ship["className"],
                ship.get("ref"),
                display,
                manufacturer_name(ship) or light_ship.get("manufacturerName"),
                light_ship.get("role"),
                light_ship.get("career"),
                ship.get("size") or light_ship.get("size"),
                vehicle.get("crewSize") or light_ship.get("crewSize"),
                mass if mass is not None else light_ship.get("massFixedKg"),
                hp if hp is not None else light_ship.get("hp"),
                cargo if cargo is not None else light_ship.get("cargo"),
                canonical_json(ship),
            ),
        )
        self.record_count += 1

    def insert_slots(self, connection: sqlite3.Connection, slots: list[Any]) -> None:
        category_families = {
            "Weapon": "weapon",
            "AssembledWeapon": "weapon",
            "Shield": "shield",
            "PowerPlant": "powerplant",
            "Cooler": "cooler",
            "Radar": "radar",
            "QuantumDrive": "qdrive",
            "QED": "qed",
            "EMP": "emp",
            "MiningLaser": "mininglaser",
            "SalvageHead": "salvagehead",
            "TractorBeam": "tractorbeam",
        }

        def walk(
            nodes: list[Any], parent_slot_id: Optional[str] = None, depth: int = 0
        ) -> None:
            for ordinal, slot in enumerate(nodes or []):
                if not isinstance(slot, dict):
                    continue
                port_name = str(slot.get("portName") or f"slot-{ordinal}")
                segment = port_name.replace("/", "_")
                slot_id = (
                    f"{parent_slot_id}/{ordinal}:{segment}"
                    if parent_slot_id
                    else f"{ordinal}:{segment}"
                )
                item = slot.get("item") if isinstance(slot.get("item"), dict) else {}
                hardpoint = (
                    slot.get("hardpoint")
                    if isinstance(slot.get("hardpoint"), dict)
                    else {}
                )
                connection.execute(
                    """
                    INSERT INTO erkul_ship_slots (
                        slot_id, parent_slot_id, depth, slot_kind, port_name,
                        port_path_json, item_category, item_class_name, minimum_size,
                        maximum_size, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slot_id,
                        parent_slot_id,
                        depth,
                        slot.get("kind"),
                        slot.get("portName"),
                        canonical_json(slot.get("portPath")),
                        item.get("category"),
                        item.get("className"),
                        hardpoint.get("minSize"),
                        hardpoint.get("maxSize"),
                        canonical_json(slot),
                    ),
                )
                self.record_count += 1

                if item:
                    category = item.get("category")
                    connection.execute(
                        """
                        INSERT INTO erkul_default_components (
                            component_id, slot_id, category, family, class_name,
                            name, manufacturer_name, size, grade, payload_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            slot_id,
                            slot_id,
                            category,
                            category_families.get(str(category)),
                            item.get("className"),
                            nested_name(item),
                            manufacturer_name(item),
                            item.get("size"),
                            item.get("grade"),
                            canonical_json(item),
                        ),
                    )
                    self.record_count += 1
                walk(slot.get("children") or [], slot_id, depth + 1)

        walk(slots)

    def insert_families(
        self,
        connection: sqlite3.Connection,
        family_data: dict[str, list[Any]],
    ) -> None:
        for kind, values in family_data.items():
            table = f"erkul_family_{safe_identifier(kind)}"
            for ordinal, item in enumerate(values):
                if not isinstance(item, dict):
                    item = {"value": item}
                record_key = str(
                    item.get("className") or item.get("ref") or f"{kind}-{ordinal}"
                )
                connection.execute(
                    f"""
                    INSERT INTO "{table}" (
                        record_key, class_name, ref, category, name,
                        manufacturer_name, size, grade, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_key,
                        item.get("className"),
                        item.get("ref"),
                        item.get("category"),
                        nested_name(item),
                        manufacturer_name(item),
                        item.get("size"),
                        item.get("grade"),
                        canonical_json(item),
                    ),
                )
                self.record_count += 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--branch",
        choices=("LIVE", "PTU", "live", "ptu"),
        default="LIVE",
        help="Erkul data branch (default: LIVE)",
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
        help="Re-download and replace Erkul tables for the current version",
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
    workflow = ErkulWorkflow(
        branch=args.branch,
        output_root=args.output_root.resolve(),
        force=args.force,
        timeout=args.timeout,
    )
    return workflow.run()


if __name__ == "__main__":
    raise SystemExit(main())

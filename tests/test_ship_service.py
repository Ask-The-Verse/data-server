from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from data_server.database import Database, ReadWriteLock
from data_server.ship_service import (
    CatalogShip,
    ShipService,
    UpstreamDetailError,
    match_catalog,
    normalize_ship_name,
    trim_ship_payload,
)
from tests.helpers import (
    create_version_database,
    insert_catalog_ship,
    insert_manufacturer,
    raw_deflate_json,
)
from workflows.game_data_common import sha256_bytes


def catalog_ship(class_name, name, short_name=None, display_name=None):
    return CatalogShip(
        class_name=class_name,
        ref=f"ref-{class_name}",
        name=name,
        short_name=short_name,
        display_name=display_name,
        detail_path=f"ships/{class_name}.bin",
        detail_sha256="sha",
    )


@pytest.mark.parametrize(
    "value, expected",
    [
        ("  F7C-M__Mk   II ", "f7c m mk ii"),
        ("Ｆ７Ｃ－Ｍ", "f7c m"),
        ("Hornet/Ghost", "hornet ghost"),
    ],
)
def test_normalize_ship_name_nfkc_case_punctuation_and_whitespace(value, expected):
    assert normalize_ship_name(value) == expected


def test_match_catalog_exact_match_across_all_name_fields():
    ships = [
        catalog_ship(
            "f7c",
            "Anvil F7C Hornet",
            short_name="F7C-M",
            display_name="Hornet",
        )
    ]

    result = match_catalog("  f7c_m ", ships)

    assert result.kind == "exact"
    assert result.matches[0].class_name == "f7c"


def test_match_catalog_unique_and_multiple_substring_are_distinct():
    ships = [
        catalog_ship("hammerhead", "Aegis Hammerhead", display_name="Hammerhead"),
        catalog_ship("hornet", "Anvil Hornet"),
        catalog_ship("hornet_ghost", "Anvil Hornet Ghost"),
    ]

    unique = match_catalog("hammer", ships)
    multiple = match_catalog("hornet", ships)

    assert unique.kind == "substring"
    assert unique.matches[0].class_name == "hammerhead"
    assert multiple.kind == "multiple"
    assert [ship.name for ship in multiple.matches] == [
        "Anvil Hornet",
        "Anvil Hornet Ghost",
    ]


def test_match_catalog_fuzzy_threshold_is_inclusive_and_has_no_result_limit():
    candidates = [
        catalog_ship(f"candidate_{index}", f"abc{index:02d}") for index in range(12)
    ]
    candidates.append(catalog_ship("below", "abxyz"))

    result = match_catalog("abcde", candidates)

    assert result.kind == "not_found"
    assert len(result.suggestions) == 12
    assert "abxyz" not in result.suggestions


def test_match_catalog_fuzzy_match_never_auto_selects_single_candidate():
    result = match_catalog(
        "hamerhed",
        [catalog_ship("hammerhead", "Hammerhead")],
    )

    assert result.kind == "not_found"
    assert result.matches == ()
    assert result.suggestions == ("Hammerhead",)


def test_trim_ship_payload_uses_strict_whitelist_and_shallow_vehicle_removal():
    payload = {
        "i18n": {"name": "Hammerhead"},
        "category": "AssembledShip",
        "className": "aegs_hammerhead",
        "vehicle": {
            "hardpoints": [1],
            "parts": [2],
            "implementationPath": "secret",
            "crewSize": 9,
            "nested": {"parts": "keep"},
        },
    }

    result = trim_ship_payload(payload)

    assert set(result) == {
        "i18n",
        "manufacturer",
        "precomputed",
        "subType",
        "tags",
        "vehicle",
    }
    assert result["manufacturer"] is None
    assert result["precomputed"] is None
    assert result["subType"] is None
    assert result["tags"] is None
    assert result["vehicle"] == {
        "crewSize": 9,
        "nested": {"parts": "keep"},
    }


def test_expand_manufacturer_prefers_uuid_then_falls_back_to_class_name(tmp_path):
    version = "4.9.0-live.1"
    path = create_version_database(tmp_path, version)
    uuid_payload = insert_manufacturer(
        path,
        record_key="uuid-row",
        ref="uuid-target",
        class_name="wrong-class",
        name="UUID Manufacturer",
    )
    class_payload = insert_manufacturer(
        path,
        record_key="class-row",
        ref="other-uuid",
        class_name="target-class",
        name="Class Manufacturer",
    )
    service = ShipService(Database(tmp_path, ReadWriteLock()), version)

    uuid_result = service._expand_manufacturer(
        {"manufacturer": {"uuid": "uuid-target", "className": "target-class"}}
    )
    class_result = service._expand_manufacturer(
        {"manufacturer": {"className": "target-class"}}
    )
    missing_result = service._expand_manufacturer(
        {"manufacturer": {"uuid": "missing", "className": "missing"}}
    )

    assert uuid_result["manufacturer"] == uuid_payload
    assert class_result["manufacturer"] == class_payload
    assert missing_result["manufacturer"] == {
        "uuid": "missing",
        "className": "missing",
    }


def test_same_ref_concurrent_lazy_load_downloads_registers_and_inserts_once(tmp_path):
    version = "4.9.0-live.1"
    path = create_version_database(tmp_path, version)
    manufacturer = insert_manufacturer(
        path,
        record_key="manufacturer",
        ref="manufacturer-uuid",
        class_name="aeg",
        name="Aegis",
    )
    payload = {
        "className": "aegs_hammerhead",
        "i18n": {"name": "Hammerhead"},
        "manufacturer": {"uuid": "manufacturer-uuid", "className": "aeg"},
        "precomputed": {"hp": {"total": 100}, "cargo": {"scu": 40}},
        "subType": "Gunship",
        "tags": ["ship"],
        "vehicle": {
            "vehicleDisplayName": "Hammerhead",
            "crewSize": 9,
            "hardpoints": [1],
            "parts": [2],
            "implementationPath": "path",
        },
    }
    content = raw_deflate_json(payload)
    insert_catalog_ship(
        path,
        class_name="aegs_hammerhead",
        ref="ship-ref",
        name="Aegis Hammerhead",
        display_name="Hammerhead",
        detail_path="ships/hammerhead.bin",
        detail_sha256=sha256_bytes(content),
    )
    calls = 0
    calls_lock = threading.Lock()

    def downloader(url, timeout):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return content

    service = ShipService(
        Database(tmp_path, ReadWriteLock()),
        version,
        downloader=downloader,
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(lambda _: service.search("Hammerhead"), range(8)))

    assert calls == 1
    assert all(response.status == "found" for response in responses)
    assert all(response.ship.manufacturer == manufacturer for response in responses)
    assert all(
        response.ship.vehicle
        == {
            "vehicleDisplayName": "Hammerhead",
            "crewSize": 9,
        }
        for response in responses
    )

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM erkul_ships").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 1
        )
        assert connection.execute(
            "SELECT file_count, record_count FROM crawl_runs WHERE source = 'erkul'"
        ).fetchone() == (7, 11)
    finally:
        connection.close()
    assert (tmp_path / version / "erkul" / "raw" / "ships" / "hammerhead.bin").is_file()
    assert (
        tmp_path / version / "erkul" / "decoded" / "ships" / "hammerhead.bin.json"
    ).is_file()


def test_lazy_load_rejects_sha256_mismatch_as_upstream_error(tmp_path):
    version = "4.9.0-live.1"
    create_version_database(tmp_path, version)
    service = ShipService(
        Database(tmp_path, ReadWriteLock()),
        version,
        downloader=lambda url, timeout: raw_deflate_json({"className": "ship"}),
    )
    ship = catalog_ship("ship", "Ship")

    with pytest.raises(UpstreamDetailError, match="SHA-256 mismatch"):
        service._download_detail(ship)

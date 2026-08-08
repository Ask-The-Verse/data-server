"""Ship name matching, lazy detail loading, and response shaping."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import unicodedata
import zlib
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from data_server.database import Database
from data_server.models import ShipPayload, ShipResponse
from workflows.game_data_common import atomic_write, http_get, sha256_bytes, utc_now

LOGGER = logging.getLogger(__name__)
ERKUL_BASE_URL = "https://cdn.erkul.games/LIVE"
SHIP_FIELDS = ("i18n", "manufacturer", "precomputed", "subType", "tags", "vehicle")
VEHICLE_EXCLUDED_FIELDS = ("hardpoints", "parts", "implementationPath")


class UpstreamDetailError(RuntimeError):
    """A ship detail could not be downloaded or decoded safely."""


@dataclass(frozen=True)
class CatalogShip:
    class_name: str
    ref: str
    name: str
    short_name: Optional[str]
    display_name: Optional[str]
    detail_path: str
    detail_sha256: str

    @property
    def preferred_name(self) -> str:
        return self.name or self.display_name or self.short_name or self.class_name

    def normalized_names(self) -> Tuple[str, ...]:
        values = {
            normalize_ship_name(value)
            for value in (self.name, self.short_name, self.display_name)
            if value
        }
        values.discard("")
        return tuple(sorted(values))


@dataclass(frozen=True)
class MatchResult:
    kind: str
    matches: Tuple[CatalogShip, ...]
    suggestions: Tuple[str, ...] = ()


def normalize_ship_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters = [
        " " if unicodedata.category(character).startswith("P") else character
        for character in normalized
    ]
    return " ".join("".join(characters).split())


def _stable_ships(ships: Iterable[CatalogShip]) -> Tuple[CatalogShip, ...]:
    return tuple(
        sorted(
            ships,
            key=lambda ship: (
                ship.preferred_name.casefold(),
                ship.class_name.casefold(),
            ),
        )
    )


def match_catalog(name: str, ships: Iterable[CatalogShip]) -> MatchResult:
    query = normalize_ship_name(name)
    unique = {ship.class_name: ship for ship in ships}
    catalog = tuple(unique.values())

    exact = [ship for ship in catalog if query in set(ship.normalized_names())]
    if exact:
        ordered = _stable_ships(exact)
        return MatchResult("exact" if len(ordered) == 1 else "multiple", ordered)

    substring = [
        ship
        for ship in catalog
        if query and any(query in candidate for candidate in ship.normalized_names())
    ]
    if substring:
        ordered = _stable_ships(substring)
        return MatchResult("substring" if len(ordered) == 1 else "multiple", ordered)

    scored: List[Tuple[float, CatalogShip]] = []
    for ship in catalog:
        score = max(
            (
                SequenceMatcher(None, query, candidate).ratio()
                for candidate in ship.normalized_names()
            ),
            default=0.0,
        )
        if score >= 0.6:
            scored.append((score, ship))
    scored.sort(
        key=lambda item: (
            -item[0],
            item[1].preferred_name.casefold(),
            item[1].class_name.casefold(),
        )
    )
    return MatchResult(
        "not_found",
        (),
        tuple(ship.preferred_name for _, ship in scored),
    )


def trim_ship_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = {field: payload.get(field) for field in SHIP_FIELDS}
    vehicle = result["vehicle"]
    if isinstance(vehicle, dict):
        vehicle = dict(vehicle)
        for field in VEHICLE_EXCLUDED_FIELDS:
            vehicle.pop(field, None)
        result["vehicle"] = vehicle
    return result


class ShipService:
    def __init__(
        self,
        database: Database,
        current_version: str,
        downloader: Callable[..., bytes] = http_get,
    ) -> None:
        self.database = database
        self.current_version = current_version
        self._downloader = downloader
        self._ref_locks: Dict[str, threading.Lock] = {}
        self._ref_locks_guard = threading.Lock()

    def _catalog(self) -> List[CatalogShip]:
        with self.database.lock.read_lock():
            connection = self.database.connect(self.current_version)
            try:
                rows = connection.execute(
                    """
                    SELECT class_name, ref, name, short_name, display_name,
                           detail_path, detail_sha256
                    FROM erkul_ship_catalog
                    """
                ).fetchall()
            finally:
                connection.close()
        return [
            CatalogShip(
                class_name=str(row["class_name"]),
                ref=str(row["ref"]),
                name=str(row["name"]),
                short_name=row["short_name"],
                display_name=row["display_name"],
                detail_path=str(row["detail_path"]),
                detail_sha256=str(row["detail_sha256"]),
            )
            for row in rows
        ]

    def search(self, name: str) -> ShipResponse:
        result = match_catalog(name, self._catalog())
        if result.kind == "multiple":
            possible = [ship.preferred_name for ship in result.matches]
            return ShipResponse(
                status="multiple_matches",
                message=(
                    f"Multiple ships found with the name: {name}. "
                    f"Here are the possible matches: {possible}"
                ),
                possible_matches=possible,
                ship=None,
            )
        if result.kind == "not_found":
            possible = list(result.suggestions)
            return ShipResponse(
                status="not_found",
                message=(
                    f"No ship found with the name: {name}. "
                    f"Here are the possible matches: {possible}"
                ),
                possible_matches=possible,
                ship=None,
            )

        ship = result.matches[0]
        payload = self._get_or_load_detail(ship)
        payload = self._expand_manufacturer(payload)
        if result.kind == "substring":
            message = (
                f'Resolved "{name}" to the unique ship match: {ship.preferred_name}.'
            )
        else:
            message = f"Ship found with the name: {ship.preferred_name}."
        return ShipResponse(
            status="found",
            message=message,
            possible_matches=[],
            ship=ShipPayload(**trim_ship_payload(payload)),
        )

    def _ref_lock(self, ref: str) -> threading.Lock:
        with self._ref_locks_guard:
            return self._ref_locks.setdefault(ref, threading.Lock())

    def _read_cached_detail(self, ref: str) -> Optional[Dict[str, Any]]:
        with self.database.lock.read_lock():
            connection = self.database.connect(self.current_version)
            try:
                row = connection.execute(
                    "SELECT payload_json FROM erkul_ships WHERE ref = ?",
                    (ref,),
                ).fetchone()
            finally:
                connection.close()
        if row is None:
            return None
        return json.loads(str(row["payload_json"]))

    def _get_or_load_detail(self, ship: CatalogShip) -> Dict[str, Any]:
        cached = self._read_cached_detail(ship.ref)
        if cached is not None:
            LOGGER.info("Ship detail cache hit ref=%s", ship.ref)
            return cached

        LOGGER.info("Ship detail cache miss ref=%s", ship.ref)
        with self._ref_lock(ship.ref):
            cached = self._read_cached_detail(ship.ref)
            if cached is not None:
                LOGGER.info("Ship detail cache filled during wait ref=%s", ship.ref)
                return cached

            content, payload, url = self._download_detail(ship)
            raw_path, decoded_path = self._detail_paths(ship.detail_path)
            atomic_write(raw_path, content)
            atomic_write(
                decoded_path,
                json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            )

            with self.database.lock.write_lock():
                connection = self.database.connect(self.current_version)
                try:
                    with connection:
                        row = connection.execute(
                            "SELECT payload_json FROM erkul_ships WHERE ref = ?",
                            (ship.ref,),
                        ).fetchone()
                        if row is None:
                            self._record_detail(
                                connection, ship, payload, content, raw_path, url
                            )
                            LOGGER.info("Ship detail cached ref=%s", ship.ref)
                        else:
                            return json.loads(str(row["payload_json"]))
                finally:
                    connection.close()
            return payload

    def _download_detail(self, ship: CatalogShip) -> Tuple[bytes, Dict[str, Any], str]:
        url = f"{ERKUL_BASE_URL}/{ship.detail_path}"
        LOGGER.info("Downloading ship detail ref=%s url=%s", ship.ref, url)
        try:
            content = self._downloader(url, timeout=120)
            actual_sha256 = sha256_bytes(content)
            if actual_sha256 != ship.detail_sha256:
                raise ValueError(
                    f"SHA-256 mismatch: expected {ship.detail_sha256}, "
                    f"received {actual_sha256}"
                )
            text = zlib.decompress(content, wbits=-zlib.MAX_WBITS).decode("utf-8")
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise TypeError("Decoded ship detail is not a JSON object")
        except (OSError, TimeoutError, ValueError, TypeError, zlib.error) as error:
            raise UpstreamDetailError(
                f"Unable to load Erkul ship detail for {ship.preferred_name}: {error}"
            ) from error
        return content, payload, url

    def _detail_paths(self, detail_path: str) -> Tuple[Path, Path]:
        version_directory = self.database.data_root / self.current_version
        return (
            version_directory / "erkul" / "raw" / detail_path,
            version_directory / "erkul" / "decoded" / f"{detail_path}.json",
        )

    def _record_detail(
        self,
        connection: sqlite3.Connection,
        ship: CatalogShip,
        payload: Dict[str, Any],
        content: bytes,
        raw_path: Path,
        url: str,
    ) -> None:
        version_directory = self.database.data_root / self.current_version
        connection.execute(
            """
            INSERT INTO source_files (
                source, relative_path, url, sha256, byte_count, downloaded_at
            )
            VALUES ('erkul', ?, ?, ?, ?, ?)
            ON CONFLICT(source, relative_path) DO UPDATE SET
                url = excluded.url,
                sha256 = excluded.sha256,
                byte_count = excluded.byte_count,
                downloaded_at = excluded.downloaded_at
            """,
            (
                str(raw_path.relative_to(version_directory)),
                url,
                sha256_bytes(content),
                len(content),
                utc_now(),
            ),
        )
        vehicle = payload.get("vehicle")
        vehicle = vehicle if isinstance(vehicle, dict) else {}
        precomputed = payload.get("precomputed")
        precomputed = precomputed if isinstance(precomputed, dict) else {}
        manufacturer = payload.get("manufacturer")
        manufacturer_name = None
        if isinstance(manufacturer, dict):
            i18n = manufacturer.get("i18n")
            if isinstance(i18n, dict):
                manufacturer_name = i18n.get("name")

        connection.execute(
            """
            INSERT INTO erkul_ships (
                class_name, ref, display_name, manufacturer_name, role, career,
                size, crew_size, mass_kg, hp, cargo_scu, payload_json
            )
            VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(payload.get("className") or ship.class_name),
                ship.ref,
                str(
                    vehicle.get("vehicleDisplayName")
                    or payload.get("name")
                    or ship.preferred_name
                ),
                manufacturer_name,
                payload.get("size"),
                vehicle.get("crewSize"),
                _numeric(vehicle.get("totalMass")),
                _numeric(precomputed.get("hp")),
                _numeric(precomputed.get("cargo")),
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )

    def _expand_manufacturer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        manufacturer = payload.get("manufacturer")
        if not isinstance(manufacturer, dict):
            LOGGER.warning("Manufacturer reference is missing or invalid")
            return payload

        row = None
        with self.database.lock.read_lock():
            connection = self.database.connect(self.current_version)
            try:
                manufacturer_uuid = manufacturer.get("uuid")
                if manufacturer_uuid:
                    row = connection.execute(
                        """
                        SELECT payload_json
                        FROM erkul_family_manufacturers
                        WHERE ref = ?
                        ORDER BY record_id
                        LIMIT 1
                        """,
                        (str(manufacturer_uuid),),
                    ).fetchone()
                if row is None and manufacturer.get("className"):
                    row = connection.execute(
                        """
                        SELECT payload_json
                        FROM erkul_family_manufacturers
                        WHERE class_name = ?
                        ORDER BY record_id
                        LIMIT 1
                        """,
                        (str(manufacturer["className"]),),
                    ).fetchone()
            finally:
                connection.close()

        if row is None:
            LOGGER.warning(
                "Manufacturer association failed uuid=%s className=%s",
                manufacturer.get("uuid"),
                manufacturer.get("className"),
            )
            return payload
        expanded = dict(payload)
        expanded["manufacturer"] = json.loads(str(row["payload_json"]))
        return expanded


def _numeric(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, dict):
        for key in ("total", "value", "kg", "scu"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                return float(candidate)
    return None

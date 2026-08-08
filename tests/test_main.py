from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from data_server.config import Settings
from data_server.main import create_app
from data_server.ship_service import UpstreamDetailError
from tests.helpers import (
    create_version_database,
    insert_cached_ship,
    insert_catalog_ship,
    insert_manufacturer,
)


@pytest.fixture
def app_client(tmp_path):
    current = "4.9.0-live.123"
    path = create_version_database(tmp_path, current)
    create_version_database(tmp_path, "4.8.1-live.987")
    insert_manufacturer(
        path,
        record_key="aeg",
        ref="manufacturer-uuid",
        class_name="aeg",
        name="Aegis Dynamics",
    )
    insert_catalog_ship(
        path,
        class_name="hammerhead",
        ref="hammerhead-ref",
        name="Aegis Hammerhead",
        display_name="Hammerhead",
    )
    insert_cached_ship(
        path,
        class_name="hammerhead",
        ref="hammerhead-ref",
        display_name="Hammerhead",
        payload={
            "i18n": {"name": "Hammerhead"},
            "manufacturer": {
                "uuid": "manufacturer-uuid",
                "className": "aeg",
            },
            "precomputed": {"hp": {"total": 100}},
            "subType": "Gunship",
            "tags": ["ship"],
            "vehicle": {
                "crewSize": 9,
                "hardpoints": [1],
                "parts": [2],
                "implementationPath": "hidden",
            },
            "category": "AssembledShip",
            "className": "hammerhead",
            "ref": "hammerhead-ref",
            "slots": [1],
        },
    )
    insert_catalog_ship(
        path,
        class_name="hornet",
        ref="hornet-ref",
        name="Anvil Hornet",
    )
    insert_catalog_ship(
        path,
        class_name="hornet-ghost",
        ref="hornet-ghost-ref",
        name="Anvil Hornet Ghost",
    )
    app = create_app(
        Settings(tmp_path, "CRITICAL"),
        lambda settings, database: current,
    )
    with TestClient(app) as client:
        yield client, app


def assert_ship_envelope(body, status):
    assert set(body) == {"status", "message", "possible_matches", "ship"}
    assert body["status"] == status
    assert isinstance(body["message"], str)
    assert isinstance(body["possible_matches"], list)


def test_health_and_versions_endpoints(app_client):
    client, _ = app_client

    health = client.get("/health")
    versions = client.get("/api/v1/versions")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "current_version": "4.9.0-live.123",
    }
    assert versions.status_code == 200
    assert versions.json() == {
        "current_version": "4.9.0-live.123",
        "historical_versions": ["4.8.1-live.987"],
    }


def test_ship_found_response_has_strict_payload_shape(app_client):
    client, _ = app_client

    response = client.get("/api/v1/ships", params={"name": "Hammerhead"})
    body = response.json()

    assert response.status_code == 200
    assert_ship_envelope(body, "found")
    assert body["possible_matches"] == []
    assert set(body["ship"]) == {
        "i18n",
        "manufacturer",
        "precomputed",
        "subType",
        "tags",
        "vehicle",
    }
    assert body["ship"]["manufacturer"]["i18n"]["name"] == "Aegis Dynamics"
    assert body["ship"]["vehicle"] == {"crewSize": 9}


def test_ship_not_found_and_multiple_matches_are_http_200(app_client):
    client, _ = app_client

    not_found = client.get("/api/v1/ships", params={"name": "hamerhed"})
    multiple = client.get("/api/v1/ships", params={"name": "horn"})

    assert not_found.status_code == 200
    assert_ship_envelope(not_found.json(), "not_found")
    assert not_found.json()["ship"] is None
    assert multiple.status_code == 200
    assert_ship_envelope(multiple.json(), "multiple_matches")
    assert multiple.json()["possible_matches"] == [
        "Anvil Hornet",
        "Anvil Hornet Ghost",
    ]
    assert multiple.json()["ship"] is None


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/ships",
        "/api/v1/ships?name=%20%20",
        f"/api/v1/ships?name={'x' * 201}",
        "/api/v1/ships?name=Hammerhead&other=value",
        "/api/v1/ships?name=Hammerhead&name=Hornet",
    ],
)
def test_ship_parameter_errors_use_uniform_http_400_response(app_client, url):
    client, _ = app_client

    response = client.get(url)
    body = response.json()

    assert response.status_code == 400
    assert_ship_envelope(body, "error")
    assert body["possible_matches"] == []
    assert body["ship"] is None


def test_ship_sqlite_failure_uses_uniform_http_500_response(app_client):
    client, app = app_client

    class FailingService:
        def search(self, name):
            raise sqlite3.OperationalError("database is locked")

    app.state.ship_service = FailingService()
    response = client.get("/api/v1/ships", params={"name": "Hammerhead"})

    assert response.status_code == 500
    assert_ship_envelope(response.json(), "error")
    assert "database error" in response.json()["message"]
    assert response.json()["ship"] is None


def test_ship_upstream_failure_uses_uniform_http_502_response(app_client):
    client, app = app_client

    class FailingService:
        def search(self, name):
            raise UpstreamDetailError("Erkul detail checksum failed.")

    app.state.ship_service = FailingService()
    response = client.get("/api/v1/ships", params={"name": "Hammerhead"})

    assert response.status_code == 502
    assert_ship_envelope(response.json(), "error")
    assert response.json()["message"] == "Erkul detail checksum failed."
    assert response.json()["possible_matches"] == []
    assert response.json()["ship"] is None

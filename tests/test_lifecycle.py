from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from data_server.config import Settings
from data_server.lifecycle import (
    WarmupError,
    parse_version,
    select_current_version,
    sort_versions,
    warm_up,
)


class FakeDatabase:
    def __init__(self, versions):
        self._versions = versions

    def complete_versions(self):
        return list(self._versions)


class SuccessfulWorkflow:
    def __init__(self, version, barrier=None):
        self.source_version = version
        self._barrier = barrier

    def run(self):
        if self._barrier is not None:
            self._barrier.wait(timeout=2)
        return 0


def test_parse_and_sort_versions_use_integer_segments():
    assert parse_version("4.10.0-LIVE.100") == (4, 10, 0, 100)
    assert parse_version("4.9-live.1") is None
    assert sort_versions(
        ["4.9.1-live.999999", "4.10.0-live.100", "3.99.0-live.999"]
    ) == [
        "4.10.0-live.100",
        "4.9.1-live.999999",
        "3.99.0-live.999",
    ]


def test_select_current_version_uses_matching_latest():
    assert (
        select_current_version(
            "4.9.0-LIVE.123",
            "4.9.0-live.123",
            ["4.8.0-live.1", "4.9.0-live.123"],
        )
        == "4.9.0-live.123"
    )


def test_select_current_version_falls_back_to_latest_complete_common_version():
    assert (
        select_current_version(
            "4.10.0-LIVE.100",
            "4.9.1-live.999999",
            ["4.8.0-live.9", "4.9.0-live.10"],
        )
        == "4.9.0-live.10"
    )


def test_select_current_version_fails_without_complete_common_version():
    with pytest.raises(WarmupError, match="No complete common"):
        select_current_version(
            "4.10.0-LIVE.100",
            "4.9.1-live.999999",
            ["not-a-version"],
        )


def test_warm_up_runs_both_workflows_in_parallel(tmp_path):
    barrier = threading.Barrier(2)
    version = "4.9.0-live.123"
    settings = Settings(Path(tmp_path), "INFO")
    database = FakeDatabase([version])

    current = warm_up(
        settings,
        database,
        {
            "erkul": lambda: SuccessfulWorkflow(version.upper(), barrier),
            "scmdb": lambda: SuccessfulWorkflow(version, barrier),
        },
    )

    assert current == version


def test_warm_up_waits_for_other_workflow_when_one_fails(tmp_path):
    finished = threading.Event()

    class FailingWorkflow:
        source_version = None

        def run(self):
            raise RuntimeError("erkul unavailable")

    class SlowWorkflow:
        source_version = "4.9.0-live.123"

        def run(self):
            time.sleep(0.05)
            finished.set()

    with pytest.raises(WarmupError, match="erkul unavailable"):
        warm_up(
            Settings(Path(tmp_path), "INFO"),
            FakeDatabase(["4.9.0-live.123"]),
            {
                "erkul": FailingWorkflow,
                "scmdb": SlowWorkflow,
            },
        )

    assert finished.is_set()

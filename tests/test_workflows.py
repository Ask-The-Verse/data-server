from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from workflows.erkul_workflow import ErkulWorkflow
from workflows.game_data_common import begin_source_run, connect_database
from workflows.scmdb_workflow import ScmdbWorkflow


class TrackingLock:
    def __init__(self):
        self.read_entries = 0
        self.write_entries = 0

    @contextmanager
    def read_lock(self):
        self.read_entries += 1
        yield

    @contextmanager
    def write_lock(self):
        self.write_entries += 1
        yield


def test_erkul_save_download_commits_source_file_in_short_locked_transaction(
    tmp_path,
):
    version_directory = tmp_path / "4.9.0-live.1"
    connection = connect_database(version_directory / "game_data.sqlite3")
    raw_directory = version_directory / "erkul" / "raw"
    begin_source_run(connection, "erkul", "4.9.0-LIVE.1", raw_directory)
    connection.commit()
    lock = TrackingLock()
    workflow = ErkulWorkflow("LIVE", tmp_path, False, 10, database_lock=lock)
    workflow.connection = connection
    workflow.version_directory = version_directory
    workflow.raw_directory = raw_directory
    workflow.decoded_directory = version_directory / "erkul" / "decoded"

    workflow.save_download(
        "ships/test.bin",
        b"compressed",
        url="https://example.test/ships/test.bin",
        decoded={"className": "test"},
    )

    observer = sqlite3.connect(version_directory / "game_data.sqlite3")
    try:
        row = observer.execute(
            "SELECT relative_path FROM source_files WHERE source = 'erkul'"
        ).fetchone()
    finally:
        observer.close()
        connection.close()
    assert row == ("erkul/raw/ships/test.bin",)
    assert lock.write_entries == 1
    assert workflow.file_count == 1


def test_scmdb_save_download_commits_source_file_in_short_locked_transaction(
    tmp_path,
):
    version_directory = tmp_path / "4.9.0-live.1"
    connection = connect_database(version_directory / "game_data.sqlite3")
    raw_directory = version_directory / "scmdb" / "raw"
    begin_source_run(connection, "scmdb", "4.9.0-live.1", raw_directory)
    connection.commit()
    lock = TrackingLock()
    workflow = ScmdbWorkflow("live", tmp_path, False, 10, database_lock=lock)
    workflow.connection = connection
    workflow.version_directory = version_directory
    workflow.raw_directory = raw_directory

    workflow.save_download(
        "merged.json",
        "https://example.test/merged.json",
        b'{"version":"4.9.0"}',
    )

    observer = sqlite3.connect(version_directory / "game_data.sqlite3")
    try:
        row = observer.execute(
            "SELECT relative_path FROM source_files WHERE source = 'scmdb'"
        ).fetchone()
    finally:
        observer.close()
        connection.close()
    assert row == ("scmdb/raw/merged.json",)
    assert lock.write_entries == 1
    assert workflow.file_count == 1

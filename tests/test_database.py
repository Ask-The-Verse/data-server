from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from data_server.database import Database, ReadWriteLock
from tests.helpers import create_version_database


def test_complete_versions_requires_both_sources(tmp_path):
    create_version_database(tmp_path, "4.8.0-live.1")
    create_version_database(
        tmp_path,
        "4.9.0-live.2",
        statuses=(("erkul", "complete"), ("scmdb", "failed")),
    )
    database = Database(tmp_path, ReadWriteLock())

    assert database.complete_versions() == ["4.8.0-live.1"]


def test_connect_creates_independent_connections_per_thread(tmp_path):
    version = "4.9.0-live.1"
    create_version_database(tmp_path, version)
    database = Database(tmp_path, ReadWriteLock())
    barrier = threading.Barrier(2)

    def connect_and_hold():
        connection = database.connect(version)
        try:
            barrier.wait(timeout=2)
            return id(connection), connection.execute("SELECT 1").fetchone()[0]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: connect_and_hold(), range(2)))

    assert results[0][0] != results[1][0]
    assert [result[1] for result in results] == [1, 1]


def test_read_write_lock_gives_waiting_writer_priority():
    lock = ReadWriteLock()
    order = []
    writer_started = threading.Event()

    def writer():
        writer_started.set()
        with lock.write_lock():
            order.append("writer")

    def reader():
        with lock.read_lock():
            order.append("reader")

    with lock.read_lock():
        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        assert writer_started.wait(timeout=1)
        deadline = time.monotonic() + 1
        while lock._waiting_writers == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert lock._waiting_writers == 1
        reader_thread = threading.Thread(target=reader)
        reader_thread.start()

    writer_thread.join(timeout=2)
    reader_thread.join(timeout=2)

    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert order == ["writer", "reader"]

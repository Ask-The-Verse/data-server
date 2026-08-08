"""SQLite connections and the process-local writer-priority read/write lock."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, List

DATABASE_NAME = "game_data.sqlite3"
REQUIRED_SOURCES = frozenset(("erkul", "scmdb"))


class ReadWriteLock:
    """A writer-priority lock for one process with multiple threads."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer_active = False
        self._waiting_writers = 0

    @contextmanager
    def read_lock(self) -> Generator[None, None, None]:
        with self._condition:
            while self._writer_active or self._waiting_writers:
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextmanager
    def write_lock(self) -> Generator[None, None, None]:
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer_active or self._readers:
                    self._condition.wait()
                self._writer_active = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            with self._condition:
                self._writer_active = False
                self._condition.notify_all()


class Database:
    """Creates an independent SQLite connection for every operation."""

    def __init__(self, data_root: Path, lock: ReadWriteLock) -> None:
        self.data_root = data_root
        self.lock = lock

    def path_for_version(self, version: str) -> Path:
        return self.data_root / version / DATABASE_NAME

    def connect(self, version: str) -> sqlite3.Connection:
        path = self.path_for_version(version)
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def complete_versions(self) -> List[str]:
        versions: List[str] = []
        if not self.data_root.is_dir():
            return versions

        with self.lock.read_lock():
            for directory in self.data_root.iterdir():
                db_path = directory / DATABASE_NAME
                if not directory.is_dir() or not db_path.is_file():
                    continue
                connection = sqlite3.connect(db_path, timeout=30)
                try:
                    rows = connection.execute(
                        "SELECT source, status FROM crawl_runs"
                    ).fetchall()
                except sqlite3.Error:
                    continue
                finally:
                    connection.close()
                statuses = {str(source): str(status) for source, status in rows}
                if all(
                    statuses.get(source) == "complete" for source in REQUIRED_SOURCES
                ):
                    versions.append(directory.name)
        return versions

    def is_complete(self, version: str) -> bool:
        return version in self.complete_versions()

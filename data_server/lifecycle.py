"""Parallel startup warm-up and semantic game-version selection."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from data_server.config import Settings
from data_server.database import Database
from workflows.erkul_workflow import ErkulWorkflow
from workflows.game_data_common import normalize_version
from workflows.scmdb_workflow import ScmdbWorkflow

LOGGER = logging.getLogger(__name__)
VERSION_PATTERN = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)-live\.(?P<build>\d+)$",
    re.IGNORECASE,
)
VersionKey = Tuple[int, int, int, int]


class WarmupError(RuntimeError):
    """Raised when startup data preparation cannot produce a current version."""


@dataclass(frozen=True)
class WorkflowResult:
    source: str
    version: str
    duration_seconds: float


def parse_version(version: str) -> Optional[VersionKey]:
    match = VERSION_PATTERN.fullmatch(version.strip())
    if not match:
        return None
    return tuple(
        int(match.group(part)) for part in ("major", "minor", "patch", "build")
    )  # type: ignore[return-value]


def sort_versions(versions: Iterable[str]) -> List[str]:
    parsed: List[Tuple[VersionKey, str]] = []
    invalid: List[str] = []
    for version in set(versions):
        key = parse_version(version)
        if key is None:
            invalid.append(version)
            LOGGER.warning("Ignoring unparseable version for ordering: %s", version)
        else:
            parsed.append((key, version))
    parsed.sort(key=lambda item: item[0], reverse=True)
    return [version for _, version in parsed] + sorted(invalid, reverse=True)


def select_current_version(
    erkul_latest: str,
    scmdb_latest: str,
    complete_versions: Iterable[str],
) -> str:
    erkul_version = normalize_version(erkul_latest)
    scmdb_version = normalize_version(scmdb_latest)
    complete = set(complete_versions)

    if (
        erkul_version == scmdb_version
        and parse_version(erkul_version) is not None
        and erkul_version in complete
    ):
        return erkul_version

    if erkul_version != scmdb_version:
        LOGGER.warning(
            "Latest source versions differ (erkul=%s, scmdb=%s); "
            "falling back to the latest common complete version",
            erkul_version,
            scmdb_version,
        )
    else:
        LOGGER.warning(
            "Latest shared version %s is not a valid complete current candidate",
            erkul_version,
        )

    ordered = [
        version
        for version in sort_versions(complete)
        if parse_version(version) is not None
    ]
    if not ordered:
        raise WarmupError("No complete common Erkul/SCMDB version is available.")
    return ordered[0]


def _run_workflow(source: str, workflow: object) -> WorkflowResult:
    started = time.monotonic()
    LOGGER.info("Starting %s workflow", source)
    try:
        run = getattr(workflow, "run")
        run()
        version = getattr(workflow, "source_version", None)
        if not version:
            raise RuntimeError(f"{source} workflow did not report a version")
    except BaseException:
        duration = time.monotonic() - started
        LOGGER.exception(
            "Finished %s workflow in %.3fs with version=%s result=failed",
            source,
            duration,
            getattr(workflow, "source_version", None),
        )
        raise
    duration = time.monotonic() - started
    LOGGER.info(
        "Finished %s workflow in %.3fs with version=%s result=success",
        source,
        duration,
        version,
    )
    return WorkflowResult(source, str(version), duration)


def warm_up(
    settings: Settings,
    database: Database,
    workflow_factories: Optional[Dict[str, Callable[[], object]]] = None,
) -> str:
    factories = workflow_factories or {
        "erkul": lambda: ErkulWorkflow(
            branch="LIVE",
            output_root=settings.data_root,
            force=False,
            timeout=settings.request_timeout,
            database_lock=database.lock,
        ),
        "scmdb": lambda: ScmdbWorkflow(
            channel="live",
            output_root=settings.data_root,
            force=False,
            timeout=settings.request_timeout,
            database_lock=database.lock,
        ),
    }

    results: Dict[str, WorkflowResult] = {}
    failures: Dict[str, BaseException] = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="warmup") as executor:
        futures = {
            source: executor.submit(_run_workflow, source, factory())
            for source, factory in factories.items()
        }
        for source, future in futures.items():
            try:
                results[source] = future.result()
            except BaseException as error:
                failures[source] = error

    if failures:
        summary = "; ".join(
            f"{source}: {type(error).__name__}: {error}"
            for source, error in sorted(failures.items())
        )
        raise WarmupError(f"Startup workflows failed: {summary}")

    return select_current_version(
        results["erkul"].version,
        results["scmdb"].version,
        database.complete_versions(),
    )

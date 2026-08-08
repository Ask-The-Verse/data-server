"""FastAPI application and HTTP routes."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from data_server.config import Settings
from data_server.database import Database, ReadWriteLock
from data_server.lifecycle import sort_versions, warm_up
from data_server.models import HealthResponse, ShipResponse, VersionsResponse
from data_server.ship_service import ShipService, UpstreamDetailError

LOGGER = logging.getLogger(__name__)


class UtcFormatter(logging.Formatter):
    converter = time.gmtime


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        UtcFormatter(
            "%(asctime)sZ %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))


def _model_dict(model: Any) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def error_response(message: str, status_code: int) -> JSONResponse:
    response = ShipResponse(
        status="error",
        message=message,
        possible_matches=[],
        ship=None,
    )
    return JSONResponse(status_code=status_code, content=_model_dict(response))


def create_app(
    settings: Optional[Settings] = None,
    warmup_function: Callable[[Settings, Database], str] = warm_up,
) -> FastAPI:
    service_settings = settings or Settings.from_env()
    configure_logging(service_settings.log_level)
    lock = ReadWriteLock()
    database = Database(service_settings.data_root, lock)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        current_version = await asyncio.to_thread(
            warmup_function, service_settings, database
        )
        application.state.current_version = current_version
        application.state.database = database
        application.state.ship_service = ShipService(database, current_version)
        yield

    application = FastAPI(title="Ask The Verse Data Server", lifespan=lifespan)

    @application.middleware("http")
    async def request_logging(request: Request, call_next):
        started = time.monotonic()
        name = request.query_params.get("name")
        try:
            response = await call_next(request)
            return response
        except BaseException:
            LOGGER.exception(
                "Unhandled request exception path=%s name=%r",
                request.url.path,
                name,
            )
            raise
        finally:
            LOGGER.info(
                "API request path=%s name=%r elapsed=%.3fs",
                request.url.path,
                name,
                time.monotonic() - started,
            )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        LOGGER.warning(
            "Request validation failed path=%s error=%s", request.url.path, error
        )
        return error_response("Invalid request parameters.", 400)

    @application.get("/health", response_model=HealthResponse)
    def health(request: Request) -> HealthResponse:
        return HealthResponse(
            status="ok",
            current_version=request.app.state.current_version,
        )

    @application.get("/api/v1/versions", response_model=VersionsResponse)
    def versions(request: Request) -> VersionsResponse:
        current = request.app.state.current_version
        all_versions = request.app.state.database.complete_versions()
        historical = [
            version for version in sort_versions(all_versions) if version != current
        ]
        return VersionsResponse(
            current_version=current,
            historical_versions=historical,
        )

    @application.get("/api/v1/ships", response_model=ShipResponse)
    def ships(
        request: Request,
        name: Optional[str] = Query(default=None),
    ):
        started = time.monotonic()
        result_status = "error"
        try:
            keys = [key for key, _ in request.query_params.multi_items()]
            if any(key != "name" for key in keys) or keys.count("name") != 1:
                return error_response(
                    'The query must contain exactly one "name" parameter.',
                    400,
                )
            assert name is not None
            stripped_name = name.strip()
            if not stripped_name:
                return error_response('The "name" parameter must not be blank.', 400)
            if len(stripped_name) > 200:
                return error_response(
                    'The "name" parameter must be 200 characters or fewer.',
                    400,
                )

            response = request.app.state.ship_service.search(stripped_name)
            result_status = response.status
            return response
        except UpstreamDetailError as error:
            LOGGER.exception("Upstream ship detail failure name=%r", name)
            return error_response(str(error), 502)
        except sqlite3.Error:
            LOGGER.exception("SQLite failure while serving ship name=%r", name)
            return error_response(
                "A database error occurred while retrieving the ship.",
                500,
            )
        except Exception:
            LOGGER.exception("Unexpected ship request failure name=%r", name)
            return error_response(
                "An internal error occurred while retrieving the ship.",
                500,
            )
        finally:
            LOGGER.info(
                "Ship API name=%r result=%s elapsed=%.3fs",
                name,
                result_status,
                time.monotonic() - started,
            )

    return application


app = create_app()

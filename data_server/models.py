"""Pydantic response models."""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]
    current_version: str


class VersionsResponse(BaseModel):
    current_version: str
    historical_versions: List[str] = Field(default_factory=list)


class ShipPayload(BaseModel):
    i18n: Optional[Any] = None
    manufacturer: Optional[Any] = None
    precomputed: Optional[Any] = None
    subType: Optional[Any] = None
    tags: Optional[Any] = None
    vehicle: Optional[Any] = None


class ShipResponse(BaseModel):
    status: Literal["found", "not_found", "multiple_matches", "error"]
    message: str
    possible_matches: List[str] = Field(default_factory=list)
    ship: Optional[ShipPayload] = None

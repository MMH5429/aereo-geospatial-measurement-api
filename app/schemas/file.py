from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer


class UploadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    status: str


class FileInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    feature_count: int
    crs: str | None
    status: str
    uploaded_at: datetime
    warnings: list[str]
    error_message: str | None = None

    @field_serializer("uploaded_at")
    def _utc_z(self, value: datetime) -> str:
        # SQLite drops tzinfo; stored values are always UTC.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FeatureMeasurement(BaseModel):
    index: int
    geometry_type: str | None
    crs: str | None
    measurable: bool
    geometry_valid: bool | None
    area_sqm: float | None
    perimeter_m: float | None
    length_m: float | None
    projected_crs: str | None
    method: str
    reason: str | None
    properties: dict[str, Any]
    geometry: dict[str, Any] | None = None


class MeasurementsResponse(BaseModel):
    file_id: str
    method: str
    total_area_sqm: float
    total_length_m: float
    count: int
    limit: int
    offset: int
    features: list[FeatureMeasurement]


class ErrorResponse(BaseModel):
    detail: Any
    error_type: str

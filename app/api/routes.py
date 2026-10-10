import uuid
from pathlib import Path
from typing import Literal

import shapely
from fastapi import APIRouter, BackgroundTasks, Depends, Query, UploadFile
from shapely.geometry import mapping
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import APIError
from app.db.models import Feature, FileStatus, Measurement, UploadedFile
from app.db.session import get_db
from app.schemas.file import (
    ErrorResponse,
    FeatureMeasurement,
    FileInfo,
    MeasurementsResponse,
    UploadResponse,
)
from app.services.archive import (
    ArchiveTooLargeError,
    InvalidArchiveError,
    UnsafeArchiveError,
    inspect_archive,
)
from app.services.ingest import sniff_kind
from app.services.processing import process_file, upload_path

router = APIRouter(prefix="/api/files", tags=["files"])

_CHUNK = 1024 * 1024
_ERRORS = {code: {"model": ErrorResponse} for code in (400, 404, 409, 413, 415)}


def _get_file(file_id: str, db: Session) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise APIError(404, f"No file with id {file_id!r}", "not_found")
    return record


async def _save_upload(upload: UploadFile, file_id: str) -> tuple[Path, str]:
    """Sniff the type from the content, then stream to disk under the size cap."""
    settings = get_settings()
    head = await upload.read(_CHUNK)
    kind = sniff_kind(head)
    if kind is None:
        raise APIError(
            415,
            "Unsupported file type: expected a .zip containing a Shapefile, or a .kml",
            "unsupported_media_type",
        )

    dest = upload_path(file_id, kind)
    size = 0
    try:
        with open(dest, "wb") as out:
            chunk = head
            while chunk:
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise APIError(
                        413,
                        f"File exceeds the {settings.max_upload_bytes} byte upload limit",
                        "file_too_large",
                    )
                out.write(chunk)
                chunk = await upload.read(_CHUNK)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest, kind


@router.post(
    "/",
    status_code=202,
    response_model=UploadResponse,
    responses=_ERRORS,
    summary="Upload a zipped Shapefile or a KML",
)
async def upload_file(
    file: UploadFile, background: BackgroundTasks, db: Session = Depends(get_db)
) -> UploadedFile:
    """Validates the upload synchronously, then parses and measures it in the background.

    Poll `GET /api/files/{id}/` until `status` is `COMPLETED` or `FAILED`.
    """
    file_id = uuid.uuid4().hex
    dest, kind = await _save_upload(file, file_id)

    if kind == "zip":
        try:
            inspect_archive(dest)
        except ArchiveTooLargeError as exc:
            dest.unlink(missing_ok=True)
            raise APIError(413, str(exc), "archive_too_large") from exc
        except UnsafeArchiveError as exc:
            dest.unlink(missing_ok=True)
            raise APIError(400, str(exc), "unsafe_archive") from exc
        except InvalidArchiveError as exc:
            dest.unlink(missing_ok=True)
            raise APIError(400, str(exc), "invalid_archive") from exc

    record = UploadedFile(
        id=file_id,
        filename=Path(file.filename or f"upload.{kind}").name[:255],
        kind=kind,
        status=FileStatus.PENDING.value,
    )
    db.add(record)
    db.commit()
    background.add_task(process_file, file_id)
    return record


@router.get(
    "/{file_id}/", response_model=FileInfo, responses=_ERRORS, summary="File information"
)
def get_file(file_id: str, db: Session = Depends(get_db)) -> UploadedFile:
    return _get_file(file_id, db)


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    responses=_ERRORS,
    summary="Per-feature measurements",
)
def get_measurements(
    file_id: str,
    method: Literal["utm", "geodesic"] = Query(
        "utm", description="utm: per-feature UTM projection. geodesic: WGS84 ellipsoid."
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    include_geometry: bool = Query(
        False, description="Include each feature's geometry as GeoJSON, in the source CRS"
    ),
    db: Session = Depends(get_db),
) -> MeasurementsResponse:
    record = _get_file(file_id, db)
    if record.status in (FileStatus.PENDING.value, FileStatus.PROCESSING.value):
        raise APIError(
            409,
            f"File is still {record.status}; retry once its status is COMPLETED",
            "not_ready",
            headers={"Retry-After": "2"},
        )
    if record.status == FileStatus.FAILED.value:
        raise APIError(
            409, f"File processing failed: {record.error_message}", "processing_failed"
        )

    total_area, total_length = db.execute(
        select(
            func.coalesce(func.sum(Measurement.area_sqm), 0.0),
            func.coalesce(func.sum(Measurement.length_m), 0.0),
        )
        .join(Feature)
        .where(Feature.file_id == file_id, Measurement.requested_method == method)
    ).one()
    rows = db.execute(
        select(Feature, Measurement)
        .join(Measurement)
        .where(Feature.file_id == file_id, Measurement.requested_method == method)
        .order_by(Feature.index)
        .limit(limit)
        .offset(offset)
    ).all()

    features = []
    for feature, m in rows:
        geometry = None
        if include_geometry and feature.geometry_wkb is not None:
            geometry = mapping(shapely.from_wkb(feature.geometry_wkb))
        features.append(
            FeatureMeasurement(
                index=feature.index,
                geometry_type=feature.geometry_type,
                crs=record.crs,
                measurable=m.measurable,
                geometry_valid=m.geometry_valid,
                area_sqm=m.area_sqm,
                perimeter_m=m.perimeter_m,
                length_m=m.length_m,
                projected_crs=m.projected_crs,
                method=m.method,
                reason=m.reason,
                properties=feature.properties,
                geometry=geometry,
            )
        )

    return MeasurementsResponse(
        file_id=file_id,
        method=method,
        total_area_sqm=round(total_area, 4),
        total_length_m=round(total_length, 4),
        count=record.feature_count,
        limit=limit,
        offset=offset,
        features=features,
    )

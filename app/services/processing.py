"""The background job: extract -> read -> measure -> persist."""

import logging
import shutil
import tempfile
from pathlib import Path

from app.core.config import get_settings
from app.db.models import Feature, FileStatus, Measurement, UploadedFile
from app.db.session import SessionLocal
from app.services import measure as measure_service
from app.services.archive import ArchiveError, inspect_archive, safe_extract
from app.services.ingest import IngestError, read_features

logger = logging.getLogger(__name__)


def upload_path(file_id: str, kind: str) -> Path:
    return get_settings().upload_dir / f"{file_id}.{kind}"


def _measure(geometry, source_crs, method: str) -> dict:
    try:
        return measure_service.measure(geometry, source_crs, method).as_dict()
    except Exception as exc:  # one bad feature must not fail the whole file
        logger.warning("Measurement failed (%s): %s", method, exc)
        return measure_service.Measurement(
            method=method, reason=f"Measurement failed: {exc}"
        ).as_dict()


def process_file(file_id: str) -> None:
    settings = get_settings()
    with SessionLocal() as db:
        record = db.get(UploadedFile, file_id)
        if record is None:
            return
        record.status = FileStatus.PROCESSING.value
        db.commit()

        source = upload_path(file_id, record.kind)
        workdir = Path(tempfile.mkdtemp(prefix=f"{file_id}-", dir=settings.tmp_dir))
        try:
            if record.kind == "zip":
                shp_member, _ = inspect_archive(source)
                safe_extract(source, workdir)
                target = workdir / shp_member
            else:
                target = source

            result = read_features(target)
            for item in result.features:
                feature = Feature(
                    file_id=file_id,
                    index=item.index,
                    geometry_type=item.geom_type,
                    geometry_wkb=None if item.geometry is None else item.geometry.wkb,
                    properties=item.properties,
                )
                for method in measure_service.METHODS:
                    feature.measurements.append(
                        Measurement(
                            requested_method=method,
                            **_measure(item.geometry, result.source_crs, method),
                        )
                    )
                db.add(feature)

            record.crs = result.crs
            record.feature_count = len(result.features)
            record.warnings = result.warnings
            record.status = FileStatus.COMPLETED.value
            db.commit()
        except Exception as exc:
            db.rollback()
            if not isinstance(exc, (ArchiveError, IngestError)):
                logger.exception("Unexpected failure processing file %s", file_id)
            record = db.get(UploadedFile, file_id)
            record.status = FileStatus.FAILED.value
            record.error_message = str(exc) or type(exc).__name__
            db.commit()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            source.unlink(missing_ok=True)

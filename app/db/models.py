from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import JSON, ForeignKey, Index, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class FileStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UploadedFile(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(8))  # "zip" (shapefile) or "kml"
    status: Mapped[str] = mapped_column(String(16), default=FileStatus.PENDING.value)
    crs: Mapped[str | None] = mapped_column(String(255), default=None)
    feature_count: Mapped[int] = mapped_column(default=0)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    uploaded_at: Mapped[datetime] = mapped_column(default=_utcnow)

    features: Mapped[list["Feature"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (Index("ix_features_file_index", "file_id", "index"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    index: Mapped[int]
    geometry_type: Mapped[str | None] = mapped_column(String(32), default=None)
    # WKB in the file's source CRS; null for null/empty geometries.
    geometry_wkb: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    properties: Mapped[dict] = mapped_column(JSON, default=dict)

    file: Mapped[UploadedFile] = relationship(back_populates="features")
    measurements: Mapped[list["Measurement"]] = relationship(
        back_populates="feature", cascade="all, delete-orphan"
    )


class Measurement(Base):
    """One row per feature per requested method, computed once at ingest."""

    __tablename__ = "measurements"
    __table_args__ = (UniqueConstraint("feature_id", "requested_method"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id", ondelete="CASCADE"))
    requested_method: Mapped[str] = mapped_column(String(16), index=True)
    # What was actually used: a UTM request can fall back to geodesic.
    method: Mapped[str] = mapped_column(String(16))
    measurable: Mapped[bool] = mapped_column(default=False)
    geometry_valid: Mapped[bool | None] = mapped_column(default=None)
    area_sqm: Mapped[float | None] = mapped_column(default=None)
    perimeter_m: Mapped[float | None] = mapped_column(default=None)
    length_m: Mapped[float | None] = mapped_column(default=None)
    projected_crs: Mapped[str | None] = mapped_column(String(255), default=None)
    reason: Mapped[str | None] = mapped_column(Text, default=None)

    feature: Mapped[Feature] = relationship(back_populates="measurements")

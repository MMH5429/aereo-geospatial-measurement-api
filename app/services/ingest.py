"""Reading uploaded Shapefiles and KML into plain feature records."""

import math
import warnings as py_warnings
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from app.services.crs import WGS84, crs_label

ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
_SNIFF_BYTES = 2048
_KML_RENDERING_FIELDS = {"tessellate", "extrude", "visibility", "drawOrder", "icon"}


class IngestError(Exception):
    """The file was accepted but its contents cannot be read as features."""


@dataclass
class FeatureRecord:
    index: int
    geom_type: str | None
    geometry: BaseGeometry | None
    properties: dict


@dataclass
class IngestResult:
    features: list[FeatureRecord]
    source_crs: CRS
    crs: str
    warnings: list[str] = field(default_factory=list)


def sniff_kind(head: bytes) -> str | None:
    """Identify an upload from its first bytes, ignoring the filename.

    Returns "zip", "kml" or None.
    """
    if head.startswith(ZIP_MAGIC):
        return "zip"
    text = head[:_SNIFF_BYTES].decode("utf-8", errors="ignore").lower()
    if "<kml" in text:
        return "kml"
    return None


def _json_safe(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return None if math.isnan(value) or math.isinf(value) else float(value)
    if value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, (datetime, date, time, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def _read_shapefile(path: Path) -> tuple[gpd.GeoDataFrame, list[str]]:
    warnings: list[str] = []
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        if len(gdf) and not gdf.geometry.is_empty.all():
            west, south, east, north = gdf.total_bounds
            if not (-180 <= west and east <= 180 and -90 <= south and north <= 90):
                raise IngestError(
                    "Shapefile has no .prj and its coordinates are not valid "
                    "longitude/latitude, so its CRS cannot be determined"
                )
        gdf = gdf.set_crs(WGS84)
        warnings.append(
            "Shapefile has no .prj file; CRS was assumed to be EPSG:4326. "
            "Measurements are wrong if the data is in another CRS."
        )
    return gdf, warnings


def _read_kml(path: Path) -> tuple[gpd.GeoDataFrame, list[str]]:
    # A KML document exposes each folder as its own layer; read them all so
    # features in folders are not silently dropped.
    layers = [str(name) for name, _ in pyogrio.list_layers(path)]
    frames = [gpd.read_file(path, layer=name) for name in layers]
    frames = [f for f in frames if len(f)]
    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs=WGS84), []
    gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry")
    # KML is EPSG:4326 by specification, whatever the reader reports.
    gdf = gdf.set_crs(WGS84, allow_override=True)
    return gdf, []


def read_features(path: Path) -> IngestResult:
    """Read a .shp (inside an extracted directory) or a standalone .kml."""
    path = Path(path)
    is_kml = path.suffix.lower() == ".kml"
    try:
        with py_warnings.catch_warnings():
            py_warnings.simplefilter("ignore")
            gdf, warnings = _read_kml(path) if is_kml else _read_shapefile(path)
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(f"Could not read {'KML' if is_kml else 'Shapefile'}: {exc}") from exc

    source_crs = CRS.from_user_input(gdf.crs)
    attributes = gdf.drop(columns=gdf.geometry.name).to_dict("records")

    features = []
    for index, (geometry, attrs) in enumerate(zip(gdf.geometry.values, attributes)):
        properties = {str(k): _json_safe(v) for k, v in attrs.items()}
        if is_kml:
            # The KML driver adds a fixed set of schema fields to every feature:
            # mostly empty, plus rendering flags that are not attributes.
            properties = {
                k: v
                for k, v in properties.items()
                if v not in (None, "") and k not in _KML_RENDERING_FIELDS
            }
        features.append(
            FeatureRecord(
                index=index,
                geom_type=None if geometry is None else geometry.geom_type,
                geometry=None if geometry is None or geometry.is_empty else geometry,
                properties=properties,
            )
        )

    return IngestResult(
        features=features, source_crs=source_crs, crs=crs_label(source_crs), warnings=warnings
    )

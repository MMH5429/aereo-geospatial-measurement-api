"""Area and length for a single geometry, never computed in degrees."""

from dataclasses import asdict, dataclass

import numpy as np
import shapely
from pyproj import CRS, Geod
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

from app.services.crs import (
    MAX_UTM_LON_SPAN_DEG,
    WGS84,
    crs_label,
    get_transformer,
    needs_reprojection,
    pick_utm_crs,
)

METHODS = ("utm", "geodesic")
_GEOD = Geod(ellps="WGS84")
_ROUND = 4


@dataclass
class Measurement:
    method: str
    measurable: bool = False
    geometry_valid: bool | None = None
    area_sqm: float | None = None
    perimeter_m: float | None = None
    length_m: float | None = None
    projected_crs: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _split(geom: BaseGeometry) -> tuple[list, list]:
    """Flatten any geometry into its polygon parts and its line parts."""
    polygons, lines = [], []
    stack = [geom]
    while stack:
        g = stack.pop()
        if g.is_empty:
            continue
        kind = g.geom_type
        if kind == "Polygon":
            polygons.append(g)
        elif kind in ("LineString", "LinearRing"):
            lines.append(g)
        elif kind in ("MultiPolygon", "MultiLineString", "GeometryCollection"):
            stack.extend(g.geoms)
    return polygons, lines


def _transform(geom: BaseGeometry, src: CRS, dst: CRS) -> BaseGeometry:
    transformer = get_transformer(src, dst)

    def _apply(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    out = shapely.transform(geom, _apply)
    if not np.isfinite(shapely.get_coordinates(out)).all():
        raise ValueError(
            f"coordinates cannot be transformed from {crs_label(src)} to {crs_label(dst)}"
        )
    return out


def _planar(polygons: list, lines: list) -> tuple[float | None, float | None, float | None]:
    area = sum(p.area for p in polygons) if polygons else None
    perimeter = sum(p.length for p in polygons) if polygons else None
    length = sum(line.length for line in lines) if lines else None
    return area, perimeter, length


def _geodesic(polygons: list, lines: list) -> tuple[float | None, float | None, float | None]:
    area = perimeter = length = None
    if polygons:
        area = perimeter = 0.0
        for polygon in polygons:
            # pyproj adds each ring's *signed* area, so holes only subtract when
            # they wind opposite to the shell. orient() guarantees that; abs()
            # then removes the overall sign.
            ring_area, ring_perimeter = _GEOD.geometry_area_perimeter(orient(polygon))
            area += abs(ring_area)
            perimeter += ring_perimeter
    if lines:
        length = sum(_GEOD.geometry_length(line) for line in lines)
    return area, perimeter, length


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), _ROUND)


def measure(geometry: BaseGeometry | None, source_crs: CRS, method: str = "utm") -> Measurement:
    """Measure one geometry.

    `method` is what the caller asked for. The returned `method` is what was
    actually used: "utm", "geodesic", or "native" when the source CRS is
    already a metre-based projection and no reprojection was needed.
    """
    if method not in METHODS:
        raise ValueError(f"Unknown method {method!r}; expected one of {METHODS}")

    if geometry is None or geometry.is_empty:
        return Measurement(method=method, reason="Feature has no geometry")

    # KML carries altitude; area and length here are planar 2D quantities.
    geom = shapely.force_2d(geometry)
    valid = bool(geom.is_valid)
    if not valid:
        geom = shapely.make_valid(geom)

    polygons, lines = _split(geom)
    if not polygons and not lines:
        return Measurement(
            method=method,
            geometry_valid=valid,
            reason=f"{geometry.geom_type} geometries have no area or length",
        )

    result = Measurement(method=method, measurable=True, geometry_valid=valid)
    if not valid:
        result.reason = "Geometry was invalid and was repaired with make_valid before measuring"

    if method == "utm" and not needs_reprojection(source_crs):
        result.method = "native"
        result.projected_crs = crs_label(source_crs)
        area, perimeter, length = _planar(polygons, lines)
    else:
        lonlat = geom if source_crs == WGS84 else _transform(geom, source_crs, WGS84)
        polygons, lines = _split(lonlat)
        utm_crs = None
        if method == "utm":
            west, south, east, north = lonlat.bounds
            if east - west > MAX_UTM_LON_SPAN_DEG:
                fallback = f"Feature spans {east - west:.1f} degrees of longitude, wider than one UTM zone"
            else:
                utm_crs = pick_utm_crs((west + east) / 2, (south + north) / 2)
                fallback = "Feature lies outside UTM coverage"
            if utm_crs is None:
                result.method = "geodesic"
                note = f"{fallback}; measured on the WGS84 ellipsoid instead"
                result.reason = f"{result.reason}. {note}" if result.reason else note

        if utm_crs is not None:
            projected = _transform(lonlat, WGS84, utm_crs)
            result.projected_crs = crs_label(utm_crs)
            area, perimeter, length = _planar(*_split(projected))
        else:
            area, perimeter, length = _geodesic(polygons, lines)

    result.area_sqm = _round(area)
    result.perimeter_m = _round(perimeter)
    result.length_m = _round(length)
    return result

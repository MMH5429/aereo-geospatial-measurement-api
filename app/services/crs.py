"""CRS selection.

Strategy: each feature is measured in the UTM zone that contains it. Features
too wide for a single zone, or outside UTM's latitude range, are measured on
the WGS84 ellipsoid instead (see measure.py).
"""

import math
from functools import lru_cache

from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info

WGS84 = CRS.from_epsg(4326)

# A UTM zone is 6 degrees wide. A feature wider than that cannot sit inside one.
MAX_UTM_LON_SPAN_DEG = 6.0
_WEB_MERCATOR_EPSG = {3857, 3785, 900913, 102100, 102113}
_METRE_UNITS = {"metre", "meter"}


def crs_label(crs: CRS) -> str:
    """'EPSG:xxxx' when the CRS can be identified, otherwise its name."""
    epsg = crs.to_epsg(min_confidence=20)
    return f"EPSG:{epsg}" if epsg else crs.name


@lru_cache(maxsize=256)
def get_transformer(src: CRS, dst: CRS) -> Transformer:
    # Building a Transformer is expensive; always_xy pins the axis order to
    # (lon, lat) / (easting, northing) regardless of what the CRS declares.
    return Transformer.from_crs(src, dst, always_xy=True)


def needs_reprojection(crs: CRS) -> bool:
    """True unless the CRS is already a metre-based projection safe to measure in."""
    if not crs.is_projected:
        return True
    if crs.to_epsg(min_confidence=20) in _WEB_MERCATOR_EPSG:
        # Metre-based, but area is inflated by 1/cos^2(latitude).
        return True
    if "pseudo" in crs.name.lower() and "mercator" in crs.name.lower():
        return True
    return any(axis.unit_name not in _METRE_UNITS for axis in crs.axis_info[:2])


@lru_cache(maxsize=1024)
def _utm_crs_for_cell(lon_cell: int, lat_cell: int) -> CRS | None:
    # The EPSG database uses the regular grid: zones change every 6 degrees of
    # longitude and at the equator (it does not model the Norway and Svalbard
    # exceptions). Every point in a 3x4 degree cell therefore shares a zone, so
    # one database lookup per cell is exact.
    lon = lon_cell * 3 + 1.5
    lat = lat_cell * 4 + 2.0
    matches = query_utm_crs_info(
        datum_name="WGS 84",
        area_of_interest=AreaOfInterest(
            west_lon_degree=lon, south_lat_degree=lat, east_lon_degree=lon, north_lat_degree=lat
        ),
    )
    return CRS.from_epsg(matches[0].code) if matches else None


def pick_utm_crs(lon: float, lat: float) -> CRS | None:
    """The WGS84 UTM zone containing a lon/lat point, or None outside UTM coverage."""
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    if not (-180.0 <= lon <= 180.0 and -80.0 <= lat <= 84.0):
        return None
    lon_cell = min(math.floor(lon / 3), 59)
    lat_cell = min(math.floor(lat / 4), 20)
    return _utm_crs_for_cell(lon_cell, lat_cell)

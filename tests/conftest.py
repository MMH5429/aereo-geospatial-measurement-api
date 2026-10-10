"""Fixtures. Every geospatial test file is generated here at test time."""

import io
import os
import stat
import tempfile
import zipfile
from pathlib import Path

# Must be set before the app (and its settings) are imported.
_DATA_DIR = tempfile.mkdtemp(prefix="geoapi-tests-")
os.environ["GEO_DATA_DIR"] = _DATA_DIR
os.environ["GEO_MAX_UPLOAD_BYTES"] = str(5 * 1024 * 1024)

import geopandas as gpd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pyproj import Geod  # noqa: E402
from shapely.geometry import LineString, Polygon  # noqa: E402

from app.main import app  # noqa: E402

GEOD = Geod(ellps="WGS84")
# Bengaluru: about 2.6 degrees east of UTM zone 43N's central meridian.
ORIGIN_LON, ORIGIN_LAT = 77.59, 12.97


def geodesic_square(lon: float = ORIGIN_LON, lat: float = ORIGIN_LAT, side_m: float = 1000.0):
    """A square whose sides are `side_m` long on the ellipsoid, in lon/lat."""
    east_lon, east_lat, _ = GEOD.fwd(lon, lat, 90, side_m)
    ne_lon, ne_lat, _ = GEOD.fwd(east_lon, east_lat, 0, side_m)
    north_lon, north_lat, _ = GEOD.fwd(lon, lat, 0, side_m)
    return Polygon(
        [(lon, lat), (east_lon, east_lat), (ne_lon, ne_lat), (north_lon, north_lat)]
    )


def geodesic_line(lon: float = ORIGIN_LON, lat: float = ORIGIN_LAT, length_m: float = 1000.0):
    end_lon, end_lat, _ = GEOD.fwd(lon, lat, 45, length_m)
    return LineString([(lon, lat), (end_lon, end_lat)])


def shapefile_zip(gdf: gpd.GeoDataFrame, tmp_path: Path, include_prj: bool = True) -> bytes:
    """Write a GeoDataFrame as a Shapefile and return it zipped."""
    folder = tmp_path / "shp"
    folder.mkdir(exist_ok=True)
    gdf.to_file(folder / "survey.shp")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for part in sorted(folder.iterdir()):
            if part.suffix == ".prj" and not include_prj:
                continue
            zf.write(part, part.name)
    return buffer.getvalue()


def zip_of(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def _coords(geometry, z: float = 920.0) -> str:
    return " ".join(f"{x:.8f},{y:.8f},{z}" for x, y in geometry.coords)


@pytest.fixture
def square():
    return geodesic_square()


@pytest.fixture
def line():
    return geodesic_line()


@pytest.fixture
def polygon_zip(tmp_path, square) -> bytes:
    """A zipped Shapefile with two 1 km squares."""
    gdf = gpd.GeoDataFrame(
        {"name": ["Plot A", "Plot B"], "plot_id": [1, 2]},
        geometry=[square, geodesic_square(lon=ORIGIN_LON + 0.05)],
        crs="EPSG:4326",
    )
    return shapefile_zip(gdf, tmp_path)


@pytest.fixture
def polygon_zip_no_prj(tmp_path, square) -> bytes:
    gdf = gpd.GeoDataFrame({"name": ["Plot A"]}, geometry=[square], crs="EPSG:4326")
    return shapefile_zip(gdf, tmp_path, include_prj=False)


@pytest.fixture
def kml_bytes(square, line) -> bytes:
    """A KML with a polygon, a line (inside a Folder) and a point, all with altitude."""
    ring = _coords(square.exterior)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>survey</name>
    <Placemark>
      <name>Plot A</name>
      <Polygon><outerBoundaryIs><LinearRing>
        <coordinates>{ring}</coordinates>
      </LinearRing></outerBoundaryIs></Polygon>
    </Placemark>
    <Folder>
      <name>Roads</name>
      <Placemark>
        <name>Haul road</name>
        <LineString><coordinates>{_coords(line)}</coordinates></LineString>
      </Placemark>
    </Folder>
    <Placemark>
      <name>Benchmark</name>
      <Point><coordinates>{ORIGIN_LON},{ORIGIN_LAT},920.0</coordinates></Point>
    </Placemark>
  </Document>
</kml>
""".encode()


@pytest.fixture
def traversal_zip() -> bytes:
    return zip_of({"../../evil.txt": b"owned", "survey.shp": b"x"})


@pytest.fixture
def symlink_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        info = zipfile.ZipInfo("link.shp")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    return buffer.getvalue()


@pytest.fixture
def bomb_zip() -> bytes:
    """4 MB of zeros that deflates to a few KB: roughly a 1000:1 ratio."""
    return zip_of({"survey.shp": b"\0" * (4 * 1024 * 1024)})


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client

"""Generate the files in samples/.

    python scripts/make_samples.py

Writes a zipped Shapefile of three survey plots and a KML holding a polygon, a
line and a point, all near Bengaluru in EPSG:4326.
"""

import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
from pyproj import Geod
from shapely.geometry import LineString, Polygon

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
GEOD = Geod(ellps="WGS84")
LON, LAT = 77.59, 12.97


def rectangle(lon: float, lat: float, width_m: float, height_m: float) -> Polygon:
    east = GEOD.fwd(lon, lat, 90, width_m)[:2]
    north_east = GEOD.fwd(*east, 0, height_m)[:2]
    north = GEOD.fwd(lon, lat, 0, height_m)[:2]
    return Polygon([(lon, lat), east, north_east, north])


def write_shapefile_zip() -> Path:
    gdf = gpd.GeoDataFrame(
        {"name": ["Plot A", "Plot B", "Plot C"], "plot_id": [1, 2, 3]},
        geometry=[
            rectangle(LON, LAT, 1000, 1000),
            rectangle(LON + 0.02, LAT, 500, 200),
            rectangle(LON, LAT + 0.02, 250, 400),
        ],
        crs="EPSG:4326",
    )
    target = SAMPLES / "plots.zip"
    with tempfile.TemporaryDirectory() as tmp:
        gdf.to_file(Path(tmp) / "plots.shp")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for part in sorted(Path(tmp).iterdir()):
                zf.write(part, part.name)
    return target


def _coords(points, z: float = 920.0) -> str:
    return " ".join(f"{x:.8f},{y:.8f},{z}" for x, y in points)


def write_kml() -> Path:
    plot = rectangle(LON, LAT, 1000, 1000)
    road = LineString([(LON, LAT), GEOD.fwd(LON, LAT, 45, 1500)[:2]])
    target = SAMPLES / "survey.kml"
    target.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>survey</name>
    <Placemark>
      <name>Plot A</name>
      <Polygon><outerBoundaryIs><LinearRing>
        <coordinates>{_coords(plot.exterior.coords)}</coordinates>
      </LinearRing></outerBoundaryIs></Polygon>
    </Placemark>
    <Folder>
      <name>Roads</name>
      <Placemark>
        <name>Haul road</name>
        <LineString><coordinates>{_coords(road.coords)}</coordinates></LineString>
      </Placemark>
    </Folder>
    <Placemark>
      <name>Benchmark</name>
      <Point><coordinates>{LON},{LAT},920.0</coordinates></Point>
    </Placemark>
  </Document>
</kml>
""",
        encoding="utf-8",
    )
    return target


if __name__ == "__main__":
    SAMPLES.mkdir(exist_ok=True)
    for path in (write_shapefile_zip(), write_kml()):
        print(f"wrote {path}")

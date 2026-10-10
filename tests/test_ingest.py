import zipfile

import geopandas as gpd
import pytest
from shapely.geometry import box

from app.services.ingest import IngestError, read_features, sniff_kind
from tests.conftest import shapefile_zip


def _extract(data: bytes, tmp_path):
    archive = tmp_path / "upload.zip"
    archive.write_bytes(data)
    out = tmp_path / "extracted"
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(out)
    return out / "survey.shp"


def test_sniff_kind_uses_content_not_name(polygon_zip, kml_bytes):
    assert sniff_kind(polygon_zip[:2048]) == "zip"
    assert sniff_kind(kml_bytes[:2048]) == "kml"
    assert sniff_kind(b"\xef\xbb\xbf<?xml version='1.0'?><KML>") == "kml"
    assert sniff_kind(b"%PDF-1.7 ...") is None
    assert sniff_kind(b"") is None


def test_read_shapefile(tmp_path, polygon_zip):
    result = read_features(_extract(polygon_zip, tmp_path))
    assert result.crs == "EPSG:4326"
    assert result.warnings == []
    assert [f.index for f in result.features] == [0, 1]
    assert all(f.geom_type == "Polygon" for f in result.features)
    assert result.features[0].properties == {"name": "Plot A", "plot_id": 1}


def test_shapefile_without_prj_assumes_4326_and_warns(tmp_path, polygon_zip_no_prj):
    result = read_features(_extract(polygon_zip_no_prj, tmp_path))
    assert result.crs == "EPSG:4326"
    assert len(result.warnings) == 1
    assert "no .prj" in result.warnings[0]


def test_shapefile_without_prj_and_non_lonlat_coordinates_is_refused(tmp_path):
    gdf = gpd.GeoDataFrame(
        {"name": ["a"]}, geometry=[box(780_000, 1_430_000, 781_000, 1_431_000)], crs="EPSG:32643"
    )
    shp = _extract(shapefile_zip(gdf, tmp_path, include_prj=False), tmp_path)
    with pytest.raises(IngestError, match="cannot be determined"):
        read_features(shp)


def test_projected_shapefile_keeps_its_declared_crs(tmp_path):
    gdf = gpd.GeoDataFrame(
        {"name": ["a"]}, geometry=[box(780_000, 1_430_000, 781_000, 1_431_000)], crs="EPSG:32643"
    )
    result = read_features(_extract(shapefile_zip(gdf, tmp_path), tmp_path))
    assert result.crs == "EPSG:32643"


def test_null_geometry_is_recorded_not_fatal(tmp_path, square):
    gdf = gpd.GeoDataFrame({"name": ["a", "b"]}, geometry=[square, None], crs="EPSG:4326")
    result = read_features(_extract(shapefile_zip(gdf, tmp_path), tmp_path))
    assert len(result.features) == 2
    assert result.features[1].geometry is None


def test_read_kml_includes_features_inside_folders(tmp_path, kml_bytes):
    path = tmp_path / "survey.kml"
    path.write_bytes(kml_bytes)
    result = read_features(path)
    assert result.crs == "EPSG:4326"
    assert sorted(f.geom_type for f in result.features) == ["LineString", "Point", "Polygon"]
    assert [f.index for f in result.features] == [0, 1, 2]
    names = {f.properties.get("Name") or f.properties.get("name") for f in result.features}
    assert names == {"Plot A", "Haul road", "Benchmark"}


def test_unreadable_kml_raises_ingest_error(tmp_path):
    path = tmp_path / "broken.kml"
    path.write_bytes(b"<kml><Document><Placemark><Polygon>")
    with pytest.raises(IngestError):
        read_features(path)

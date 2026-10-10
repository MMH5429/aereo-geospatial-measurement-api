import pytest
from pyproj import CRS
from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from app.services.crs import WGS84, needs_reprojection, pick_utm_crs
from app.services.measure import measure
from tests.conftest import ORIGIN_LAT, ORIGIN_LON, geodesic_square

SQ_KM = 1_000_000.0


def rel_error(actual: float, expected: float) -> float:
    return abs(actual - expected) / expected


# --- accuracy: these prove the CRS handling is correct, not just that it runs ---


def test_geodesic_area_of_known_square(square):
    result = measure(square, WGS84, "geodesic")
    assert result.method == "geodesic"
    assert rel_error(result.area_sqm, SQ_KM) < 1e-4
    assert rel_error(result.perimeter_m, 4000) < 1e-4


def test_utm_area_of_known_square(square):
    result = measure(square, WGS84, "utm")
    assert result.method == "utm"
    assert result.projected_crs == "EPSG:32643"
    # Bengaluru is ~2.6 degrees off the zone's central meridian, where UTM
    # overstates area by roughly 0.1%. 0.25% bounds that with margin.
    assert rel_error(result.area_sqm, SQ_KM) < 2.5e-3


def test_utm_and_geodesic_agree(square):
    utm = measure(square, WGS84, "utm").area_sqm
    geodesic = measure(square, WGS84, "geodesic").area_sqm
    assert rel_error(utm, geodesic) < 2.5e-3


def test_area_is_not_computed_in_degrees(square):
    # In raw degrees this square's "area" is about 8e-5.
    assert measure(square, WGS84).area_sqm > 900_000


@pytest.mark.parametrize("method", ["utm", "geodesic"])
def test_line_length(line, method):
    result = measure(line, WGS84, method)
    assert result.area_sqm is None and result.perimeter_m is None
    assert rel_error(result.length_m, 1000) < 2e-3


def test_southern_hemisphere_uses_a_south_zone():
    result = measure(geodesic_square(lon=151.2, lat=-33.87), WGS84)
    assert result.projected_crs == "EPSG:32756"
    assert rel_error(result.area_sqm, SQ_KM) < 2.5e-3


# --- the geometry matrix ---


def test_multipolygon_sums_its_parts(square):
    other = geodesic_square(lon=ORIGIN_LON + 0.05)
    result = measure(MultiPolygon([square, other]), WGS84, "geodesic")
    assert rel_error(result.area_sqm, 2 * SQ_KM) < 1e-4


def test_multilinestring_sums_its_parts(line):
    result = measure(MultiLineString([line, line]), WGS84, "geodesic")
    assert rel_error(result.length_m, 2000) < 1e-4


@pytest.mark.parametrize("hole_reversed", [False, True])
@pytest.mark.parametrize("method", ["utm", "geodesic"])
def test_hole_is_subtracted_whatever_its_winding(method, hole_reversed):
    outer = geodesic_square(side_m=1000)
    hole = list(geodesic_square(ORIGIN_LON + 0.002, ORIGIN_LAT + 0.002, 100).exterior.coords)
    if hole_reversed:
        hole = hole[::-1]
    result = measure(Polygon(outer.exterior.coords, [hole]), WGS84, method)
    assert rel_error(result.area_sqm, SQ_KM - 10_000) < 2.5e-3


@pytest.mark.parametrize("geometry", [Point(ORIGIN_LON, ORIGIN_LAT), MultiPoint([(77, 13), (78, 13)])])
def test_points_are_not_measurable_and_not_an_error(geometry):
    result = measure(geometry, WGS84)
    assert result.measurable is False
    assert result.area_sqm is None and result.length_m is None
    assert "no area or length" in result.reason


@pytest.mark.parametrize("geometry", [None, Polygon()])
def test_missing_geometry_is_reported(geometry):
    result = measure(geometry, WGS84)
    assert result.measurable is False
    assert result.reason == "Feature has no geometry"


def test_linear_ring_is_measured_as_a_closed_line(square):
    result = measure(LinearRing(square.exterior.coords), WGS84, "geodesic")
    assert result.area_sqm is None
    assert rel_error(result.length_m, 4000) < 1e-4


def test_geometry_collection_recurses(square, line):
    collection = GeometryCollection([square, line, Point(ORIGIN_LON, ORIGIN_LAT)])
    result = measure(collection, WGS84, "geodesic")
    assert rel_error(result.area_sqm, SQ_KM) < 1e-4
    assert rel_error(result.length_m, 1000) < 1e-4


def test_altitude_is_ignored(square):
    with_z = Polygon([(x, y, 5000.0) for x, y in square.exterior.coords])
    assert measure(with_z, WGS84).area_sqm == measure(square, WGS84).area_sqm


def test_invalid_polygon_is_repaired_and_flagged():
    d = 0.01
    bowtie = Polygon(
        [
            (ORIGIN_LON, ORIGIN_LAT),
            (ORIGIN_LON + d, ORIGIN_LAT + d),
            (ORIGIN_LON + d, ORIGIN_LAT),
            (ORIGIN_LON, ORIGIN_LAT + d),
        ]
    )
    assert not bowtie.is_valid
    result = measure(bowtie, WGS84)
    assert result.measurable is True
    assert result.geometry_valid is False
    assert result.area_sqm > 0
    assert "repaired" in result.reason


# --- CRS strategy ---


def test_feature_wider_than_a_utm_zone_falls_back_to_geodesic():
    wide = box(70.0, 10.0, 85.0, 11.0)
    result = measure(wide, WGS84, "utm")
    assert result.method == "geodesic"
    assert result.projected_crs is None
    assert "wider than one UTM zone" in result.reason
    assert result.area_sqm == measure(wide, WGS84, "geodesic").area_sqm


def test_polar_feature_falls_back_to_geodesic():
    result = measure(box(10.0, 86.0, 11.0, 86.5), WGS84, "utm")
    assert result.method == "geodesic"
    assert "outside UTM coverage" in result.reason
    assert result.area_sqm > 0


def test_metre_based_projected_source_is_measured_natively():
    utm43 = CRS.from_epsg(32643)
    result = measure(box(780_000, 1_430_000, 781_000, 1_431_000), utm43, "utm")
    assert result.method == "native"
    assert result.projected_crs == "EPSG:32643"
    assert result.area_sqm == SQ_KM


def test_web_mercator_source_is_reprojected_not_trusted():
    from app.services.measure import _transform

    mercator = CRS.from_epsg(3857)
    # At 60N Web Mercator inflates area fourfold.
    square = geodesic_square(lon=10.5, lat=60.0)
    in_mercator = _transform(square, WGS84, mercator)
    assert in_mercator.area > 3.5 * SQ_KM
    result = measure(in_mercator, mercator, "utm")
    assert result.method == "utm"
    assert rel_error(result.area_sqm, SQ_KM) < 2.5e-3


def test_needs_reprojection():
    assert needs_reprojection(WGS84)
    assert needs_reprojection(CRS.from_epsg(3857))
    assert needs_reprojection(CRS.from_epsg(2263))  # US survey feet
    assert not needs_reprojection(CRS.from_epsg(32643))


@pytest.mark.parametrize(
    "lon, lat, epsg",
    [
        (77.59, 12.97, 32643),
        (-0.1, 51.5, 32630),
        (151.2, -33.87, 32756),
        (5.3, 60.4, 32631),  # Bergen: the regular grid, not Norway's widened 32V
        (179.9, 0.1, 32660),
        (-179.9, -0.1, 32701),
    ],
)
def test_pick_utm_crs(lon, lat, epsg):
    assert pick_utm_crs(lon, lat).to_epsg() == epsg


def test_unknown_method_is_rejected(square):
    with pytest.raises(ValueError):
        measure(square, WGS84, "mercator")

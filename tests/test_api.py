import uuid

import pytest

from app.core.config import get_settings
from app.db.models import FileStatus, UploadedFile
from app.db.session import SessionLocal
from tests.conftest import zip_of

SQ_KM = 1_000_000.0


def upload(client, data: bytes, filename: str):
    return client.post("/api/files/", files={"file": (filename, data)})


def upload_ok(client, data: bytes, filename: str) -> str:
    response = upload(client, data, filename)
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"id", "filename", "status"}
    assert body["filename"] == filename
    return body["id"]


def by_type(features: list[dict]) -> dict[str, dict]:
    return {f["geometry_type"]: f for f in features}


def test_shapefile_end_to_end(client, polygon_zip):
    file_id = upload_ok(client, polygon_zip, "survey.zip")

    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "COMPLETED"
    assert info["feature_count"] == 2
    assert info["crs"] == "EPSG:4326"
    assert info["warnings"] == []
    assert info["uploaded_at"].endswith("Z")

    body = client.get(f"/api/files/{file_id}/measurements/").json()
    assert body["file_id"] == file_id
    assert body["method"] == "utm"
    assert body["count"] == 2
    assert body["total_area_sqm"] == pytest.approx(2 * SQ_KM, rel=2.5e-3)
    first = body["features"][0]
    assert first["index"] == 0
    assert first["geometry_type"] == "Polygon"
    assert first["crs"] == "EPSG:4326"
    assert first["projected_crs"] == "EPSG:32643"
    assert first["area_sqm"] == pytest.approx(SQ_KM, rel=2.5e-3)
    assert first["length_m"] is None
    assert first["properties"] == {"name": "Plot A", "plot_id": 1}
    assert first["geometry"] is None


def test_kml_end_to_end(client, kml_bytes):
    file_id = upload_ok(client, kml_bytes, "survey.kml")

    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "COMPLETED"
    assert info["feature_count"] == 3
    assert info["crs"] == "EPSG:4326"

    body = client.get(f"/api/files/{file_id}/measurements/?method=geodesic").json()
    features = by_type(body["features"])
    assert features["Polygon"]["area_sqm"] == pytest.approx(SQ_KM, rel=1e-4)
    assert features["LineString"]["length_m"] == pytest.approx(1000, rel=1e-4)
    assert features["LineString"]["area_sqm"] is None
    assert body["total_area_sqm"] == pytest.approx(SQ_KM, rel=1e-4)
    assert body["total_length_m"] == pytest.approx(1000, rel=1e-4)


def test_point_is_unmeasurable_not_an_error(client, kml_bytes):
    file_id = upload_ok(client, kml_bytes, "survey.kml")
    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 200
    point = by_type(response.json()["features"])["Point"]
    assert point["measurable"] is False
    assert point["area_sqm"] is None and point["length_m"] is None
    assert point["reason"]


def test_missing_prj_is_reported_as_a_warning(client, polygon_zip_no_prj):
    file_id = upload_ok(client, polygon_zip_no_prj, "survey.zip")
    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "COMPLETED"
    assert info["crs"] == "EPSG:4326"
    assert any("no .prj" in w for w in info["warnings"])


def test_type_is_detected_from_content_not_filename(client, kml_bytes):
    file_id = upload_ok(client, kml_bytes, "misnamed.zip")
    assert client.get(f"/api/files/{file_id}/").json()["status"] == "COMPLETED"


def test_methods_agree_and_pagination(client, polygon_zip):
    file_id = upload_ok(client, polygon_zip, "survey.zip")
    utm = client.get(f"/api/files/{file_id}/measurements/?method=utm").json()
    geodesic = client.get(f"/api/files/{file_id}/measurements/?method=geodesic").json()
    assert geodesic["features"][0]["method"] == "geodesic"
    assert geodesic["features"][0]["projected_crs"] is None
    assert utm["total_area_sqm"] == pytest.approx(geodesic["total_area_sqm"], rel=2.5e-3)

    page = client.get(f"/api/files/{file_id}/measurements/?limit=1&offset=1").json()
    assert page["count"] == 2
    assert [f["index"] for f in page["features"]] == [1]
    # Totals cover the whole file, not just the page.
    assert page["total_area_sqm"] == utm["total_area_sqm"]


def test_include_geometry_returns_geojson(client, polygon_zip):
    file_id = upload_ok(client, polygon_zip, "survey.zip")
    body = client.get(f"/api/files/{file_id}/measurements/?include_geometry=true").json()
    geometry = body["features"][0]["geometry"]
    assert geometry["type"] == "Polygon"
    assert len(geometry["coordinates"][0]) == 5


# --- rejections ---


def test_path_traversal_archive_is_rejected(client, traversal_zip):
    response = upload(client, traversal_zip, "evil.zip")
    assert response.status_code == 400
    assert response.json()["error_type"] == "unsafe_archive"


def test_zip_bomb_is_rejected(client, bomb_zip):
    response = upload(client, bomb_zip, "bomb.zip")
    assert response.status_code == 413
    assert response.json()["error_type"] == "archive_too_large"


def test_zip_without_shapefile_is_rejected(client):
    response = upload(client, zip_of({"readme.txt": b"hello"}), "docs.zip")
    assert response.status_code == 400
    assert response.json()["error_type"] == "invalid_archive"


def test_corrupt_zip_is_rejected(client):
    response = upload(client, b"PK\x03\x04" + b"garbage" * 50, "broken.zip")
    assert response.status_code == 400
    assert response.json()["error_type"] == "invalid_archive"


def test_unsupported_type_is_rejected(client):
    response = upload(client, b"%PDF-1.7 not geospatial", "survey.kml")
    assert response.status_code == 415
    assert set(response.json()) == {"detail", "error_type"}


def test_oversized_upload_is_rejected(client):
    too_big = b"<kml>" + b" " * (get_settings().max_upload_bytes + 200_000)
    response = upload(client, too_big, "big.kml")
    assert response.status_code == 413
    assert response.json()["error_type"] == "file_too_large"


def test_rejected_uploads_leave_nothing_on_disk(client, traversal_zip):
    upload(client, traversal_zip, "evil.zip")
    upload(client, b"%PDF-1.7", "x.kml")
    assert list(get_settings().upload_dir.iterdir()) == []


def test_missing_file_field_is_422(client):
    response = client.post("/api/files/")
    assert response.status_code == 422
    assert response.json()["error_type"] == "validation_error"


@pytest.mark.parametrize("suffix", ["", "measurements/"])
def test_unknown_id_is_404(client, suffix):
    response = client.get(f"/api/files/does-not-exist/{suffix}")
    assert response.status_code == 404
    assert response.json()["error_type"] == "not_found"


@pytest.mark.parametrize("query", ["method=mercator", "limit=0", "limit=5000", "offset=-1"])
def test_bad_query_params_are_422(client, polygon_zip, query):
    file_id = upload_ok(client, polygon_zip, "survey.zip")
    assert client.get(f"/api/files/{file_id}/measurements/?{query}").status_code == 422


def test_measurements_while_processing_is_409(client):
    file_id = uuid.uuid4().hex
    with SessionLocal() as db:
        db.add(
            UploadedFile(
                id=file_id, filename="slow.kml", kind="kml", status=FileStatus.PROCESSING.value
            )
        )
        db.commit()

    assert client.get(f"/api/files/{file_id}/").json()["status"] == "PROCESSING"
    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 409
    assert response.json()["error_type"] == "not_ready"
    assert response.headers["Retry-After"] == "2"


def test_unparseable_file_ends_up_failed(client):
    file_id = upload_ok(client, b"<kml><Document><Placemark><Polygon>", "broken.kml")
    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "FAILED"
    assert info["error_message"]

    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 409
    assert response.json()["error_type"] == "processing_failed"

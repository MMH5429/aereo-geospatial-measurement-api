import pytest

from app.services.archive import (
    ArchiveLimits,
    ArchiveTooLargeError,
    InvalidArchiveError,
    UnsafeArchiveError,
    find_shapefile,
    inspect_archive,
    safe_extract,
)
from tests.conftest import zip_of

LIMITS = ArchiveLimits(
    max_uncompressed_bytes=50 * 1024 * 1024, max_members=100, max_compression_ratio=100
)


def _write(tmp_path, data: bytes):
    path = tmp_path / "upload.zip"
    path.write_bytes(data)
    return path


def test_path_traversal_is_refused_and_nothing_is_written(tmp_path, traversal_zip):
    dest = tmp_path / "out"
    with pytest.raises(UnsafeArchiveError):
        safe_extract(_write(tmp_path, traversal_zip), dest, LIMITS)
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()
    assert not dest.exists() or not any(dest.iterdir())


@pytest.mark.parametrize("name", ["/etc/evil.txt", "C:/evil.txt", "\\evil.txt", "a/../../b"])
def test_absolute_and_escaping_names_are_refused(tmp_path, name):
    with pytest.raises(UnsafeArchiveError):
        safe_extract(_write(tmp_path, zip_of({name: b"x"})), tmp_path / "out", LIMITS)


def test_symlink_entries_are_refused(tmp_path, symlink_zip):
    with pytest.raises(UnsafeArchiveError):
        safe_extract(_write(tmp_path, symlink_zip), tmp_path / "out", LIMITS)


def test_zip_bomb_ratio_is_refused(tmp_path, bomb_zip):
    with pytest.raises(ArchiveTooLargeError, match="ratio"):
        inspect_archive(_write(tmp_path, bomb_zip), LIMITS)


def test_total_uncompressed_size_is_capped(tmp_path, bomb_zip):
    limits = ArchiveLimits(
        max_uncompressed_bytes=1024 * 1024, max_members=100, max_compression_ratio=1e9
    )
    with pytest.raises(ArchiveTooLargeError, match="expands"):
        safe_extract(_write(tmp_path, bomb_zip), tmp_path / "out", limits)


def test_member_count_is_capped(tmp_path):
    data = zip_of({f"f{i}.txt": b"x" for i in range(101)})
    with pytest.raises(ArchiveTooLargeError, match="members"):
        inspect_archive(_write(tmp_path, data), LIMITS)


def test_corrupt_zip_is_invalid(tmp_path):
    with pytest.raises(InvalidArchiveError):
        inspect_archive(_write(tmp_path, b"PK\x03\x04 not really a zip"), LIMITS)


def test_safe_archive_extracts(tmp_path, polygon_zip):
    extracted = safe_extract(_write(tmp_path, polygon_zip), tmp_path / "out", LIMITS)
    assert {p.suffix for p in extracted} >= {".shp", ".shx", ".dbf", ".prj"}
    assert all(p.is_relative_to((tmp_path / "out").resolve()) for p in extracted)


def test_find_shapefile_reports_prj():
    assert find_shapefile(["a/s.shp", "a/s.shx", "a/s.dbf", "a/s.prj"]) == ("a/s.shp", True)
    assert find_shapefile(["S.SHP", "S.SHX", "S.DBF"]) == ("S.SHP", False)


def test_find_shapefile_ignores_macos_noise():
    names = ["s.shp", "s.shx", "s.dbf", "__MACOSX/._s.shp"]
    assert find_shapefile(names) == ("s.shp", False)


@pytest.mark.parametrize(
    "names, message",
    [
        (["readme.txt"], "does not contain"),
        (["a.shp", "a.shx", "a.dbf", "b.shp", "b.shx", "b.dbf"], "2 .shp files"),
        (["a.shp", "a.dbf"], "missing .shx"),
    ],
)
def test_find_shapefile_rejects_unusable_sets(names, message):
    with pytest.raises(InvalidArchiveError, match=message):
        find_shapefile(names)

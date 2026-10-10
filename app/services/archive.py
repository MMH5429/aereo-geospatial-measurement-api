"""Safe handling of uploaded zip archives.

Nothing in an uploaded archive is trusted: member names, declared sizes and
entry types are all checked before a single byte is written to disk.
"""

import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.config import get_settings

# Tiny archives can have extreme ratios without being a threat (a 2 KB .dbf of
# padding compresses very well), so the ratio rule only applies above this size.
RATIO_CHECK_FLOOR_BYTES = 1024 * 1024
_COPY_CHUNK = 1024 * 1024
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
REQUIRED_SIDECARS = (".shx", ".dbf")


class ArchiveError(Exception):
    pass


class InvalidArchiveError(ArchiveError):
    """Corrupt zip, or not a usable Shapefile set."""


class UnsafeArchiveError(ArchiveError):
    """Path traversal, absolute paths or symlink entries."""


class ArchiveTooLargeError(ArchiveError):
    """Too many members, too much uncompressed data, or a suspicious ratio."""


@dataclass(frozen=True)
class ArchiveLimits:
    max_uncompressed_bytes: int
    max_members: int
    max_compression_ratio: float

    @classmethod
    def from_settings(cls) -> "ArchiveLimits":
        s = get_settings()
        return cls(s.max_uncompressed_bytes, s.max_archive_members, s.max_compression_ratio)


def _open(zip_path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise InvalidArchiveError(f"Not a readable zip archive: {exc}") from exc


def _check_members(zf: zipfile.ZipFile, limits: ArchiveLimits) -> list[zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > limits.max_members:
        raise ArchiveTooLargeError(
            f"Archive has {len(infos)} members; the limit is {limits.max_members}"
        )

    total = compressed = 0
    for info in infos:
        name = info.filename
        if stat.S_ISLNK(info.external_attr >> 16):
            raise UnsafeArchiveError(f"Archive contains a symlink entry: {name!r}")
        if name.startswith(("/", "\\")) or _DRIVE_PREFIX.match(name):
            raise UnsafeArchiveError(f"Archive contains an absolute path: {name!r}")
        if ".." in PurePosixPath(name.replace("\\", "/")).parts:
            raise UnsafeArchiveError(f"Archive contains a path traversal entry: {name!r}")
        total += info.file_size
        compressed += info.compress_size

    if total > limits.max_uncompressed_bytes:
        raise ArchiveTooLargeError(
            f"Archive expands to {total} bytes; the limit is {limits.max_uncompressed_bytes}"
        )
    if total > RATIO_CHECK_FLOOR_BYTES and total > compressed * limits.max_compression_ratio:
        raise ArchiveTooLargeError(
            "Archive compression ratio exceeds "
            f"{limits.max_compression_ratio:.0f}:1 and looks like a zip bomb"
        )
    return infos


def _is_noise(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return "__MACOSX" in path.parts or path.name.startswith("._")


def find_shapefile(names: list[str]) -> tuple[str, bool]:
    """Return (the single .shp member, whether it has a .prj).

    Raises InvalidArchiveError unless there is exactly one .shp with its
    mandatory .shx and .dbf sidecars beside it.
    """
    names = [n.replace("\\", "/") for n in names if not _is_noise(n)]
    shps = [n for n in names if n.lower().endswith(".shp")]
    if not shps:
        raise InvalidArchiveError("Archive does not contain a .shp file")
    if len(shps) > 1:
        raise InvalidArchiveError(
            f"Archive contains {len(shps)} .shp files; upload one Shapefile per archive"
        )

    shp = shps[0]
    lowered = {n.lower() for n in names}
    stem = shp[: -len(".shp")].lower()
    missing = [ext for ext in REQUIRED_SIDECARS if stem + ext not in lowered]
    if missing:
        raise InvalidArchiveError(
            f"Shapefile is incomplete: missing {', '.join(missing)} for {shp!r}"
        )
    return shp, stem + ".prj" in lowered


def inspect_archive(zip_path: Path, limits: ArchiveLimits | None = None) -> tuple[str, bool]:
    """Validate an archive without extracting it. Returns find_shapefile()'s result."""
    limits = limits or ArchiveLimits.from_settings()
    with _open(zip_path) as zf:
        infos = _check_members(zf, limits)
        return find_shapefile([i.filename for i in infos if not i.is_dir()])


def safe_extract(
    zip_path: Path, dest_dir: Path, limits: ArchiveLimits | None = None
) -> list[Path]:
    """Extract every member into dest_dir, refusing anything that would escape it."""
    limits = limits or ArchiveLimits.from_settings()
    dest_root = Path(dest_dir).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    written = 0

    with _open(zip_path) as zf:
        for info in _check_members(zf, limits):
            target = (dest_root / info.filename.replace("\\", "/")).resolve()
            if not target.is_relative_to(dest_root):
                raise UnsafeArchiveError(
                    f"Archive member escapes the extraction directory: {info.filename!r}"
                )
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as src, open(target, "wb") as dst:
                    # Declared sizes were checked above; count real bytes too, in
                    # case the headers lie.
                    while chunk := src.read(_COPY_CHUNK):
                        written += len(chunk)
                        if written > limits.max_uncompressed_bytes:
                            raise ArchiveTooLargeError(
                                "Archive expands beyond the limit of "
                                f"{limits.max_uncompressed_bytes} bytes"
                            )
                        dst.write(chunk)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                raise InvalidArchiveError(
                    f"Could not read archive member {info.filename!r}: {exc}"
                ) from exc
            extracted.append(target)
    return extracted

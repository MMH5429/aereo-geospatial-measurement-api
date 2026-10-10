# Geospatial File Measurement API

A FastAPI service that accepts a zipped Shapefile or a KML, extracts its features, and returns
the area of each polygon and the length of each line in square metres and metres.

Measurements are never computed in latitude/longitude degrees. Each feature is reprojected to
the UTM zone that contains it, or measured directly on the WGS84 ellipsoid.

- [Setup](#setup)
- [API](#api)
- [Architecture](#architecture)
- [Design decisions](#design-decisions)
- [Learning](#learning)
- [Future scope](#future-scope)

## Setup

### Docker

```bash
docker compose up --build
```

The API is then at <http://localhost:8000>, with interactive docs at <http://localhost:8000/docs>.

### Local

Requires Python 3.12. The geospatial libraries install as wheels that bundle GDAL, GEOS and
PROJ, so no system packages are needed.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

### Try it

Two sample files are in `samples/` (regenerate them with `python scripts/make_samples.py`).

```bash
curl -F "file=@samples/plots.zip" http://localhost:8000/api/files/
# {"id":"05a0383f...","filename":"plots.zip","status":"PENDING"}

curl http://localhost:8000/api/files/05a0383f.../
curl http://localhost:8000/api/files/05a0383f.../measurements/
```

### Tests

```bash
pytest
```

All test files (Shapefiles, KML, malicious archives) are generated at test time; no binary
fixtures are committed.

### Configuration

Settings are read from environment variables, or a `.env` file.

| Variable | Default | Meaning |
|---|---|---|
| `GEO_DATA_DIR` | `data` | Where the SQLite database and in-flight uploads live |
| `GEO_DATABASE_URL` | SQLite in `GEO_DATA_DIR` | Any SQLAlchemy URL |
| `GEO_MAX_UPLOAD_BYTES` | 100 MB | Largest accepted upload |
| `GEO_MAX_UNCOMPRESSED_BYTES` | 500 MB | Largest total size an archive may expand to |
| `GEO_MAX_ARCHIVE_MEMBERS` | 1000 | Most entries allowed in an archive |
| `GEO_MAX_COMPRESSION_RATIO` | 100 | Archives above this ratio are treated as zip bombs |

## API

Full typed documentation is generated at `/docs` (Swagger) and `/redoc`.

### `POST /api/files/`

Uploads a file as multipart form data in a field named `file`. Accepts a `.zip` containing one
Shapefile (`.shp`, `.shx`, `.dbf`, and optionally `.prj`), or a `.kml`.

The upload is validated before the response is sent; parsing and measuring then run in the
background. Poll the file information endpoint until `status` is `COMPLETED` or `FAILED`.

```
202 Accepted
{ "id": "05a0383f341d41f8a0e632e8bf62966f", "filename": "plots.zip", "status": "PENDING" }
```

### `GET /api/files/{id}/`

```json
{
  "id": "05a0383f341d41f8a0e632e8bf62966f",
  "filename": "plots.zip",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "uploaded_at": "2026-10-10T18:55:56Z",
  "warnings": [],
  "error_message": null
}
```

`status` moves `PENDING` → `PROCESSING` → `COMPLETED` or `FAILED`. On `FAILED`, `error_message`
says why. `warnings` reports assumptions the service made, such as a missing `.prj`.

### `GET /api/files/{id}/measurements/`

| Query parameter | Default | Meaning |
|---|---|---|
| `method` | `utm` | `utm` (per-feature UTM projection) or `geodesic` (WGS84 ellipsoid) |
| `limit` | `100` | Page size, 1 to 1000 |
| `offset` | `0` | Features to skip |
| `include_geometry` | `false` | Include each geometry as GeoJSON, in the source CRS |

```json
{
  "file_id": "ff21ccd225d34e269f3284c2e4b79d1f",
  "method": "utm",
  "total_area_sqm": 1001142.6113,
  "total_length_m": 1500.8712,
  "count": 3,
  "limit": 100,
  "offset": 0,
  "features": [
    {
      "index": 0,
      "geometry_type": "Polygon",
      "crs": "EPSG:4326",
      "measurable": true,
      "geometry_valid": true,
      "area_sqm": 1001142.6113,
      "perimeter_m": 4002.2846,
      "length_m": null,
      "projected_crs": "EPSG:32643",
      "method": "utm",
      "reason": null,
      "properties": { "Name": "Plot A" },
      "geometry": null
    },
    {
      "index": 1,
      "geometry_type": "Point",
      "crs": "EPSG:4326",
      "measurable": false,
      "geometry_valid": true,
      "area_sqm": null,
      "perimeter_m": null,
      "length_m": null,
      "projected_crs": null,
      "method": "utm",
      "reason": "Point geometries have no area or length",
      "properties": { "Name": "Benchmark" },
      "geometry": null
    }
  ]
}
```

`count` and the two totals cover the whole file, not just the returned page. The per-feature
`method` is what was actually used, which can differ from the one requested (see
[CRS handling](#crs-handling)).

### Errors

Every error has the same shape:

```json
{ "detail": "No file with id 'nope'", "error_type": "not_found" }
```

| Status | When |
|---|---|
| 400 | Corrupt zip, unsafe zip (path traversal, absolute paths, symlinks), no Shapefile in the zip, more than one Shapefile, or a missing `.shx`/`.dbf` |
| 404 | Unknown file id |
| 409 | Measurements requested while the file is still processing (with a `Retry-After` header), or after processing failed |
| 413 | Upload over the size limit, or an archive that expands too far or looks like a zip bomb |
| 415 | Content is neither a zip nor a KML |
| 422 | Missing `file` field or invalid query parameters |

## Architecture

### Application structure

```
app/
  main.py               app, error handlers, upload-size middleware
  core/config.py        settings
  core/errors.py        APIError
  api/routes.py         the three endpoints
  schemas/file.py       response models
  db/session.py         engine and session
  db/models.py          UploadedFile, Feature, Measurement
  services/archive.py   safe zip inspection and extraction
  services/ingest.py    type sniffing; Shapefile/KML -> feature records
  services/crs.py       CRS selection and cached transformers
  services/measure.py   area/length for one geometry
  services/processing.py  the background job tying the services together
tests/                  fixtures generated in code
scripts/make_samples.py generates samples/
```

The services have no knowledge of HTTP or the database, so each is tested on its own.

### File-processing flow

```
POST /api/files/
  │  Content-Length over the cap?            -> 413 (before the body is read)
  │  sniff first bytes: zip / KML / neither  -> 415
  │  stream to disk, counting bytes          -> 413
  │  zip only: inspect without extracting    -> 400 / 413
  │      member names, symlinks, count, total size, ratio, .shp/.shx/.dbf present
  │  insert file row (PENDING)
  └─ 202, then in the background:
        PROCESSING
        zip: safe_extract into a per-upload temp dir
        read features (GeoPandas / pyogrio)
        resolve the source CRS
        measure every feature with both methods
        store features + measurements
        COMPLETED, or FAILED with error_message
        finally: delete the temp dir and the uploaded file
```

Per feature the service stores its index, geometry type, geometry (WKB, in the source CRS),
and properties. The CRS is stored once per file and reported on every feature.

### Measurement flow

For each geometry (`services/measure.py`):

1. Null or empty geometry → `measurable: false`, with a reason.
2. Drop Z values. KML carries altitude; area and length are planar quantities.
3. If the geometry is invalid (for example a self-intersecting polygon), repair it with
   `make_valid` and report `geometry_valid: false`.
4. Split it into polygon parts and line parts, recursing into multi-geometries and geometry
   collections. No parts of either kind (points) → `measurable: false`.
5. Measure: polygons give `area_sqm` and `perimeter_m`, lines give `length_m`.

| Geometry | `area_sqm` | `perimeter_m` | `length_m` |
|---|---|---|---|
| Polygon, MultiPolygon | sum of parts | sum of parts | null |
| LineString, MultiLineString, LinearRing | null | null | sum of parts |
| Point, MultiPoint | null | null | null (`measurable: false`) |
| GeometryCollection | from its polygons | from its polygons | from its lines |
| null / empty | null | null | null (`measurable: false`) |

Both methods are computed once at ingest and stored, so `?method=` only selects rows and the
totals are a SQL `SUM`.

### CRS handling

**Resolving the source CRS**

| File | Rule |
|---|---|
| Shapefile with `.prj` | Use the declared CRS |
| Shapefile without `.prj` | Assume EPSG:4326 and say so in `warnings`. If the coordinates are not valid longitude/latitude the file is failed instead, because the assumption is then known to be wrong |
| KML | EPSG:4326, by specification |

**Choosing how to measure**

| Source CRS | `method=utm` | `method=geodesic` |
|---|---|---|
| Geographic (degrees) | Reproject the feature to its own UTM zone | Measure on the WGS84 ellipsoid |
| Projected, metre-based | Measure directly, reported as `method: "native"` | Transform to WGS84, then as above |
| Web Mercator, or projected in other units | Treated like geographic: transform to WGS84, then UTM | As above |

The UTM zone is chosen per feature from the centre of its bounding box. Two cases fall back to
the geodesic calculation, and the feature then reports `method: "geodesic"` with a reason:

- the feature is wider than 6° of longitude, so no single zone can hold it;
- the feature lies outside UTM's latitude range (south of 80°S or north of 84°N).

## Design decisions

### CRS strategy: per-feature UTM, with a geodesic alternative

| Strategy | For | Against |
|---|---|---|
| **Per-feature UTM** (chosen as the default) | Conformal and metre-based; survey-scale features sit well inside one zone; results are in a named CRS a surveyor can reproduce in QGIS | Not exact: see the numbers below. Useless for features wider than a zone |
| **Geodesic on the ellipsoid** (available via `?method=geodesic`) | No projection, so no projection distortion at any size or location | No "projected CRS" to report, and the brief asks for a projection |
| One projected CRS for the whole file | Simple | Wrong as soon as a dataset crosses a zone boundary: features far from the chosen central meridian are distorted |
| Local equal-area projection per feature | Exact area | Distorts length, so it would need a second projection for lines |

Measured UTM error against the geodesic result, for a 1 km square:

| Location | UTM area error |
|---|---|
| On a zone's central meridian (75°E, 13°N) | −0.08% |
| Bengaluru (77.59°E, 12.97°N), 2.6° off the meridian | +0.12% |
| Zone edge at the equator | +0.19% |
| Zone edge at 60°N | −0.01% |

A feature just under the 6° width limit that straddles a zone boundary at the equator came out
0.29% high. So the UTM method is good to roughly 0.3% for area, and the geodesic method should
be used where that is not enough. The tests assert 0.25% for UTM and 0.01% for geodesic on a
1 km square built from geodesic distances.

The EPSG database's UTM lookup uses the regular 6° grid; it does not apply the Norway and
Svalbard zone exceptions. That only changes which of two adjacent zones is used.

### Other decisions

- **FastAPI over Django + DRF.** The service is three endpoints with no admin, auth or
  templates. FastAPI gives typed request validation and OpenAPI docs for free. Django would
  bring GeoDjango, but that needs a system GDAL install and a spatial database.
- **Background processing with `BackgroundTasks`, over synchronous processing or Celery.**
  Parsing a large file should not hold the request open, which is why the brief's file resource
  has a `status`. Celery and a broker would be the production answer but are a lot of moving
  parts for a reviewer to start. The cost: a job is lost if the process dies mid-run, and a
  client must poll (measurements return 409 until the file is ready).
- **Validate before accepting.** Type, size and archive safety are checked synchronously so a
  bad upload gets a real status code, not a `FAILED` record discovered later.
- **Content sniffing over file extensions.** The type is decided from the first bytes. A KML
  named `.zip` is processed as KML; a PDF named `.kml` is rejected.
- **Untrusted archives.** Member names are checked for traversal, absolute paths and symlinks;
  member count, total uncompressed size and compression ratio are capped; and bytes are counted
  again during extraction in case the zip headers lie. The ratio rule is skipped below 1 MB
  uncompressed, where small padded `.dbf` files produce harmless extreme ratios.
- **Compute both methods at ingest, over computing on request.** Reads become simple indexed
  queries and totals do not require re-measuring every feature. The cost is doing the work
  twice per upload.
- **Geometry as WKB, over GeoJSON text.** WKB is compact, lossless and independent of CRS.
  GeoJSON is easier to inspect and is produced on request with `include_geometry=true`.
- **SQLite over PostgreSQL/PostGIS.** Zero setup. Nothing here needs spatial queries yet, and
  the database is reached only through SQLAlchemy, so `GEO_DATABASE_URL` can point elsewhere.
- **One Shapefile per archive.** An archive with several `.shp` files is rejected rather than
  silently processing one of them.
- **Missing `.prj`: assume and warn, over rejecting.** Most such files are EPSG:4326, so the
  job completes and the assumption is stated. Silently guessing would be worse than either.
- **Invalid geometry: repair and flag, over failing.** One bad polygon should not sink a file.
- **Web Mercator is not trusted.** EPSG:3857 is metre-based, but its area is inflated by
  1/cos²(latitude), about fourfold at 60°N. It is reprojected like geographic data.
- **KML rendering fields are dropped.** The KML driver attaches `tessellate`, `extrude`,
  `visibility`, `drawOrder` and `icon` to every feature; these are not attributes of the data.

## Learning

I had not worked with GIS data before this. The things that surprised me:

- **Degrees are not a unit of distance.** A degree of longitude is about 111 km at the equator
  and zero at the poles, so an area in "square degrees" means nothing. The 1 km test square has
  an area of about 0.00008 in raw coordinates.
- **Projections trade one error for another.** UTM is not "accurate"; it is accurate near one
  meridian. I expected an error figure I could quote, and found it depends on where the feature
  sits in its zone. Measuring it was more useful than reading about it.
- **"Metre-based" does not mean "safe to measure".** Web Mercator reports metres and is wrong
  by a factor of four at 60°N.
- **KML is always EPSG:4326**, and carries altitude that has to be dropped before measuring.
- **A Shapefile is several files and holds one geometry type.** The `.prj` is optional, and
  without it the coordinates are just numbers.
- **Geodesic area is signed.** `pyproj` returns a negative area for a clockwise ring, and holes
  are only subtracted if they wind opposite to the shell, so rings must be normalised first.
- **Axis order is a trap.** EPSG:4326 is formally latitude-first; `always_xy=True` is what keeps
  coordinates as (longitude, latitude).
- **A KML folder is a separate layer.** Reading only the default layer silently drops features.
- **Zip files are an attack surface**: path traversal, symlinks and decompression bombs.

## Future scope

- **A real job queue** (Celery or RQ with Redis) so jobs survive restarts and can be retried,
  and a webhook or server-sent events in place of polling.
- **PostGIS**, with geometry columns and spatial indexes, to allow queries such as "features
  within this boundary".
- **Large files:** streaming uploads to object storage, reading features in chunks, and bulk
  inserts. Today a file is read into memory in one go.
- **More formats:** KMZ, GeoJSON, GeoPackage, DXF, and multi-layer archives.
- **Volume from a surface model.** Given a DSM raster, the volume of a stockpile or pit is the
  integral of elevation against a reference plane inside the polygon, which is what drone
  surveys are usually for.
- **Better CRS inference** for files without a `.prj`, for example from the coordinate range.
- **Custom projection per feature** (a transverse Mercator centred on the feature) to remove
  the dependence on position within a UTM zone.
- **Authentication, rate limiting, and deletion/retention** of uploaded files.
- **Database migrations** (Alembic) in place of `create_all`.

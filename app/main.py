import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import router
from app.core.config import get_settings
from app.core.errors import APIError
from app.db.session import init_db

logger = logging.getLogger(__name__)

# Room for multipart boundaries and headers on top of the file itself.
_MULTIPART_OVERHEAD = 64 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Geospatial File Measurement API",
    description=(
        "Upload a zipped Shapefile or a KML and get per-feature area and length, "
        "measured in a projected CRS rather than in degrees."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(router)


def _error(status_code: int, detail, error_type: str, headers=None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "error_type": error_type},
        headers=headers,
    )


@app.middleware("http")
async def reject_oversized_uploads(request: Request, call_next):
    # Runs before the body is read, so an oversized upload is refused without
    # being spooled to disk. Uploads without a Content-Length are caught by the
    # byte count in the upload handler instead.
    if request.method == "POST":
        declared = request.headers.get("content-length", "")
        limit = get_settings().max_upload_bytes
        if declared.isdigit() and int(declared) > limit + _MULTIPART_OVERHEAD:
            return _error(
                413, f"File exceeds the {limit} byte upload limit", "file_too_large"
            )
    return await call_next(request)


@app.exception_handler(APIError)
async def api_error_handler(_: Request, exc: APIError) -> JSONResponse:
    return _error(exc.status_code, exc.detail, exc.error_type, exc.headers)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _error(exc.status_code, exc.detail, "http_error", exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()
    ]
    return _error(422, errors, "validation_error")


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error", exc_info=exc)
    return _error(500, "Internal server error", "internal_error")


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}

"""HTTP layer: checks the upload, hands it to the processor, and shapes the responses."""
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pyproj import CRS
from pyproj.exceptions import CRSError
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from app import config, processor, readers, schemas
from app.db import COMPLETED, Feature, UploadedFile, get_db, init_db
from app.errors import InputError

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Geospatial File Measurement API",
    version="1.0.0",
    description="Upload a zipped Shapefile, KML or KMZ and get the area or length of every feature, in metres.",
    lifespan=lifespan,
)
# Starlette writes the whole upload to a temp file before our code runs, so a size check inside the
# endpoint is too late. This middleware answers 413 as soon as the body passes the limit.
# The extra 64 KB is room for the multipart headers around the file.
app.add_middleware(RequestBodyLimitMiddleware, max_body_size=config.MAX_UPLOAD_BYTES + 64 * 1024)


@app.exception_handler(InputError)
def input_error_handler(request: Request, exc: InputError):
    body = {"detail": exc.message}
    if exc.file_id:
        body["id"] = exc.file_id
        body["status"] = "FAILED"
    return JSONResponse(status_code=exc.status_code, content=body)


# Each route is registered with and without the trailing slash. Otherwise FastAPI answers
# "POST /api/files" with a 307 redirect and an empty body, which looks like nothing happened.

@app.post("/api/files/", status_code=201, response_model=schemas.FileInfo)
@app.post("/api/files", status_code=201, response_model=schemas.FileInfo, include_in_schema=False)
def upload_file(
    file: UploadFile,
    source_crs: str | None = Query(None, description="CRS for a Shapefile without a .prj, e.g. EPSG:32643"),
    db: Session = Depends(get_db),
):
    filename = Path(file.filename or "").name
    ext = Path(filename).suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise InputError(f"Unsupported file type '{ext or filename}'. Upload a .zip (Shapefile), .kml or .kmz.", 415)
    crs = _parse_crs(source_crs)

    work_dir = Path(tempfile.mkdtemp(dir=config.TMP_DIR))
    try:
        path = work_dir / f"upload{ext}"  # never build paths from the client's filename
        with path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        readers.check_upload(path, ext)

        record = UploadedFile(filename=filename, file_format=ext.lstrip("."))
        db.add(record)
        db.commit()
        try:
            processor.process_file(db, record, path, ext, work_dir, crs)
        except InputError as exc:
            exc.file_id = record.id
            raise
        return record
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.get("/api/files/{file_id}/", response_model=schemas.FileInfo)
@app.get("/api/files/{file_id}", response_model=schemas.FileInfo, include_in_schema=False)
def get_file(file_id: str, db: Session = Depends(get_db)):
    return _get_or_404(db, file_id)


@app.get("/api/files/{file_id}/measurements/", response_model=schemas.MeasurementsOut)
@app.get("/api/files/{file_id}/measurements", response_model=schemas.MeasurementsOut, include_in_schema=False)
def get_measurements(
    file_id: str,
    fmt: Literal["json", "geojson"] = Query("json", alias="format", description="geojson returns a FeatureCollection"),
    limit: int | None = Query(None, ge=1, description="Return at most this many features"),
    offset: int = Query(0, ge=0, description="Skip this many features"),
    db: Session = Depends(get_db),
):
    record = _get_or_404(db, file_id)
    if record.status != COMPLETED:
        raise InputError(f"No measurements: the file status is {record.status}. {record.error or ''}".strip(), 409)

    rows = db.query(Feature).filter_by(file_id=file_id).order_by(Feature.index).offset(offset).limit(limit)
    features = [schemas.feature_out(f) for f in rows]
    if fmt == "geojson":
        return JSONResponse(schemas.feature_collection(features), media_type="application/geo+json")

    area, length = db.query(func.sum(Feature.area_sq_m), func.sum(Feature.length_m)).filter_by(file_id=file_id).one()
    return schemas.MeasurementsOut(
        file_id=record.id,
        filename=record.filename,
        status=record.status,
        feature_count=record.feature_count,
        totals=schemas.totals(area, length),
        features=features,
    )


@app.get("/viewer/{file_id}", include_in_schema=False)
def viewer(file_id: str):
    return FileResponse(STATIC_DIR / "viewer.html")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/health")
def health():
    return {"status": "ok"}


def _get_or_404(db: Session, file_id: str) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="File not found.")
    return record


def _parse_crs(value: str | None) -> CRS | None:
    if value is None:
        return None
    try:
        return CRS.from_user_input(value)
    except CRSError:
        raise InputError(f"source_crs '{value}' is not a valid CRS. Use a code such as EPSG:32643.")

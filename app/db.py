"""SQLite through SQLAlchemy: one row per uploaded file, one row per feature inside it."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app import config

PROCESSING, COMPLETED, FAILED = "PROCESSING", "COMPLETED", "FAILED"

config.DATA_DIR.mkdir(parents=True, exist_ok=True)
config.TMP_DIR.mkdir(parents=True, exist_ok=True)
# check_same_thread=False because FastAPI runs sync endpoints in a thread pool.
engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UploadedFile(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    filename: Mapped[str] = mapped_column(String(255))
    file_format: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=PROCESSING)
    crs: Mapped[str | None] = mapped_column(String(255))
    feature_count: Mapped[int | None] = mapped_column(Integer)
    layers: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    features: Mapped[list["Feature"]] = relationship(
        back_populates="file", order_by="Feature.index", cascade="all, delete-orphan"
    )


class Feature(Base):
    __tablename__ = "features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id"), index=True)
    index: Mapped[int] = mapped_column(Integer)  # position in the file, starting at 0
    layer: Mapped[str] = mapped_column(String(255))
    geometry_type: Mapped[str | None] = mapped_column(String(32))
    geometry: Mapped[dict | None] = mapped_column(JSON)  # GeoJSON, always EPSG:4326
    crs: Mapped[str | None] = mapped_column(String(255))  # the CRS the file was in
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    # Stored in m² and m only; hectares, acres and km are derived when responding.
    area_sq_m: Mapped[float | None] = mapped_column(Float)
    length_m: Mapped[float | None] = mapped_column(Float)
    length_3d_m: Mapped[float | None] = mapped_column(Float)
    max_grade_pct: Mapped[float | None] = mapped_column(Float)
    projected_crs: Mapped[str | None] = mapped_column(String(64))
    method: Mapped[str | None] = mapped_column(String(32))
    geodesic_diff_pct: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)

    file: Mapped[UploadedFile] = relationship(back_populates="features")


def init_db() -> None:
    Base.metadata.create_all(engine)
    # A file still PROCESSING at startup means the server stopped mid-upload. Don't leave it stuck.
    with SessionLocal() as db:
        db.query(UploadedFile).filter_by(status=PROCESSING).update(
            {"status": FAILED, "error": "Processing was interrupted by a server restart. Please upload again."}
        )
        db.commit()


def get_db():
    with SessionLocal() as db:
        yield db

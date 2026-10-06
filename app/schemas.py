"""Response shapes. The database stores m² and m; hectares, acres and km are derived here."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db import Feature

SQ_M_PER_HECTARE = 10_000
SQ_M_PER_ACRE = 4046.8564224
M_PER_KM = 1000


def _convert(value: float | None, divisor: float, digits: int) -> float | None:
    # "+ 0.0" turns -0.0 (a tiny negative rounded away) into a plain 0.0
    return None if value is None else round(value / divisor, digits) + 0.0


class FileInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    feature_count: int | None
    crs: str | None
    status: str
    file_format: str
    layers: list[str]
    warnings: list[str]
    error: str | None
    created_at: datetime
    processed_at: datetime | None


class Measurement(BaseModel):
    area_sq_m: float | None
    area_hectares: float | None
    area_acres: float | None
    length_m: float | None
    length_km: float | None
    length_3d_m: float | None
    max_grade_pct: float | None
    projected_crs: str | None
    method: str
    geodesic_diff_pct: float | None


class FeatureOut(BaseModel):
    index: int
    layer: str
    geometry_type: str | None
    geometry: dict | None
    crs: str | None
    properties: dict
    measurement: Measurement | None
    notes: list[str]
    error: str | None


class Totals(BaseModel):
    area_sq_m: float
    area_hectares: float
    area_acres: float
    length_m: float
    length_km: float


class MeasurementsOut(BaseModel):
    file_id: str
    filename: str
    status: str
    feature_count: int
    geometry_crs: str = "EPSG:4326"  # geometry is RFC 7946 GeoJSON; each feature's `crs` is the file's own CRS
    totals: Totals
    features: list[FeatureOut]


def measurement_out(f: Feature) -> Measurement | None:
    if f.method is None:
        return None
    return Measurement(
        area_sq_m=_convert(f.area_sq_m, 1, 2),
        area_hectares=_convert(f.area_sq_m, SQ_M_PER_HECTARE, 4),
        area_acres=_convert(f.area_sq_m, SQ_M_PER_ACRE, 4),
        length_m=_convert(f.length_m, 1, 3),
        length_km=_convert(f.length_m, M_PER_KM, 4),
        length_3d_m=_convert(f.length_3d_m, 1, 3),
        max_grade_pct=_convert(f.max_grade_pct, 1, 2),
        projected_crs=f.projected_crs,
        method=f.method,
        geodesic_diff_pct=_convert(f.geodesic_diff_pct, 1, 6),
    )


def feature_out(f: Feature) -> FeatureOut:
    return FeatureOut(
        index=f.index,
        layer=f.layer,
        geometry_type=f.geometry_type,
        geometry=f.geometry,
        crs=f.crs,
        properties=f.properties,
        measurement=measurement_out(f),
        notes=f.notes,
        error=f.error,
    )


def totals(area_sq_m: float | None, length_m: float | None) -> Totals:
    # A plain sum: if polygons overlap, the overlap is counted twice.
    area, length = area_sq_m or 0.0, length_m or 0.0
    return Totals(
        area_sq_m=round(area, 2),
        area_hectares=round(area / SQ_M_PER_HECTARE, 4),
        area_acres=round(area / SQ_M_PER_ACRE, 4),
        length_m=round(length, 3),
        length_km=round(length / M_PER_KM, 4),
    )


def feature_collection(features: list[FeatureOut]) -> dict:
    """The same results as GeoJSON, ready to drop into QGIS or a MapLibre map."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": f.index,
                "geometry": f.geometry,
                "properties": {
                    **f.properties,
                    "layer": f.layer,
                    **(f.measurement.model_dump() if f.measurement else {}),
                    "notes": f.notes,
                    "error": f.error,
                },
            }
            for f in features
        ],
    }

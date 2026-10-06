"""The pipeline behind POST /api/files/: read -> clean -> measure -> store, and set the status."""
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd
import shapely
from pyproj import CRS
from shapely.geometry import mapping
from sqlalchemy.orm import Session

from app import cleaning, measure, readers
from app.db import COMPLETED, FAILED, Feature, UploadedFile, utcnow
from app.errors import InputError

log = logging.getLogger(__name__)


def process_file(db: Session, record: UploadedFile, path: Path, ext: str, work_dir: Path, source_crs: CRS | None) -> None:
    try:
        result = readers.read_upload(path, ext, work_dir, source_crs)
        features: list[Feature] = []
        for layer in result.layers:
            features += build_features(layer, start_index=len(features))
    except InputError as exc:
        _fail(db, record, exc.message)
        raise
    except Exception:
        # Not the client's fault as far as we know, but still an answer they can act on.
        log.exception("Unexpected error while processing file %s", record.id)
        _fail(db, record, "The file could not be processed.")
        raise InputError("The file could not be processed. It may be corrupt or use an unsupported variant.", 422)

    record.file_format = result.file_format
    record.layers = [layer.name for layer in result.layers]
    record.crs = ", ".join(sorted({readers.crs_label(layer.gdf.crs) for layer in result.layers}))
    record.warnings = result.warnings
    record.features = features
    record.feature_count = len(features)
    record.status = COMPLETED
    record.processed_at = utcnow()
    db.commit()


def _fail(db: Session, record: UploadedFile, message: str) -> None:
    record.status = FAILED
    record.error = message
    record.processed_at = utcnow()
    db.commit()


def build_features(layer: readers.Layer, start_index: int = 0) -> list[Feature]:
    gdf = layer.gdf
    source_crs = readers.crs_label(gdf.crs)
    originals = list(gdf.geometry.to_crs(4326))  # keeps Z, which the slope calculation needs
    cleaned = [cleaning.clean(g) for g in originals]
    parts = [cleaning.split_parts(g) for g, _ in cleaned]
    measurements = measure.measure_all(parts)
    properties = gdf.drop(columns=gdf.geometry.name).to_dict("records")
    # Map ("grid") area in the file's own projected CRS, e.g. what AutoCAD shows for a UTM drawing.
    grid_areas = gdf.geometry.area.tolist() if gdf.crs is not None and gdf.crs.is_projected else None

    features = []
    for i, original in enumerate(originals):
        (flat, notes), (poly, line), m = cleaned[i], parts[i], measurements[i]
        feature = Feature(
            index=start_index + i,
            layer=layer.name,
            geometry_type=original.geom_type if original is not None else None,
            geometry=to_geojson(original),
            crs=source_crs,
            properties={key: json_safe(value) for key, value in properties[i].items()},
            notes=notes + m.notes,
        )
        if flat is None:
            feature.error = "The feature has no geometry (for example a KML GroundOverlay), so it was not measured."
        elif poly is None and line is None:
            if "Point" in flat.geom_type:
                feature.notes.append("Points have no area or length.")
            else:
                feature.notes.append(f"{flat.geom_type} is not a measurable geometry type.")
        else:
            _copy_measurement(feature, m)
            if grid_areas and poly is not None:
                _add_grid_note(feature, grid_areas[i], source_crs)
            if line is not None and layer.z_is_elevation[i] and original.has_z:
                _add_slope(feature, original)
        features.append(feature)
    return features


def _copy_measurement(feature: Feature, m: measure.Measurement) -> None:
    feature.area_sq_m = m.area_sq_m
    feature.length_m = m.length_m
    feature.projected_crs = m.projected_crs
    feature.method = m.method
    feature.geodesic_diff_pct = m.geodesic_diff_pct


def _add_grid_note(feature: Feature, grid_area: float, source_crs: str) -> None:
    """Explain why a CAD/GIS user sees a different number in their own software."""
    if feature.area_sq_m and abs(grid_area - feature.area_sq_m) / feature.area_sq_m * 100 > 0.01:
        feature.notes = feature.notes + [
            f"In the file's own grid ({source_crs}) this is {grid_area:,.2f} m². That is map area; the projection "
            f"stretches it, so the true ground area reported here is {feature.area_sq_m:,.2f} m²."
        ]


def _add_slope(feature: Feature, original) -> None:
    _, line_3d = cleaning.split_parts(original)  # same lines as before, but with their heights
    if line_3d is None or np.ptp(shapely.get_coordinates(line_3d, include_z=True)[:, 2]) == 0:
        return  # flat or no heights: slope length equals map length
    profile = measure.slope_profile(line_3d)
    if profile is None:
        return
    feature.length_3d_m, feature.max_grade_pct = profile
    if feature.max_grade_pct > measure.DGMS_MAX_GRADE_PCT:
        feature.notes = feature.notes + [
            f"Steepest stretch is {feature.max_grade_pct:.1f}% (1 in {100 / feature.max_grade_pct:.0f}), "
            "steeper than the 1 in 16 DGMS limit for mine haul roads."
        ]


def to_geojson(geom) -> dict | None:
    """RFC 7946 GeoJSON: longitude/latitude, outer rings counter-clockwise, 7 decimals (about 1 cm)."""
    if geom is None or geom.is_empty:
        return None
    return _round(mapping(shapely.orient_polygons(geom)), 7)


def _round(value, digits: int):
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, (list, tuple)):
        return [_round(v, digits) for v in value]
    if isinstance(value, dict):
        return {k: _round(v, digits) for k, v in value.items()}
    return value


def json_safe(value):
    """Attribute values straight from GDAL include NaN, NaT, numpy numbers and dates.
    NaN alone makes FastAPI answer 500 ("Out of range float values are not JSON compliant")."""
    if isinstance(value, (list, dict)):
        return value
    if pd.isna(value):
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value

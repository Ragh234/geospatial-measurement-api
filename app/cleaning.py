"""Make each geometry safe to measure, and note anything that had to change (QC notes)."""
import shapely
from shapely.geometry import LineString, MultiLineString, MultiPolygon
from shapely.geometry.base import BaseGeometry


def clean(geom: BaseGeometry | None) -> tuple[BaseGeometry | None, list[str]]:
    """Return a flat (2D), valid copy of the geometry plus QC notes. None means nothing to measure."""
    if geom is None or geom.is_empty:
        return None, []
    notes = []
    flat = shapely.force_2d(geom)  # area and length are measured on the map; heights are handled separately
    if not flat.is_valid:
        reason = shapely.is_valid_reason(flat)
        repaired = shapely.make_valid(flat)
        notes.append(_repair_note(reason, flat, repaired))
        flat = repaired
    return flat, notes


def _repair_note(reason: str, before: BaseGeometry, after: BaseGeometry) -> str:
    note = f"Invalid geometry repaired with make_valid ({reason})."
    # A self-intersecting "bow-tie" has a raw area of 0: its two halves cancel out.
    # buffer(0), the usual quick fix, keeps only one half. make_valid keeps both.
    if after.area > 0:
        change = (before.area - after.area) / after.area * 100
        note += f" Measuring it unrepaired would have been off by {change:+.1f}%."
    return note


def split_parts(geom: BaseGeometry | None) -> tuple[MultiPolygon | None, MultiLineString | None]:
    """Separate the measurable parts: polygons give area, lines give length, points give nothing.

    A KML MultiGeometry often holds a label Point next to the Polygon, and make_valid can turn
    a polygon into a GeometryCollection. Splitting means neither case loses its area.
    """
    if geom is None:
        return None, None
    polygons, lines = [], []
    for part in _flatten(geom):
        if part.geom_type == "Polygon":
            polygons.append(part)
        elif part.geom_type in ("LineString", "LinearRing"):
            lines.append(LineString(part.coords))
    return (MultiPolygon(polygons) if polygons else None, MultiLineString(lines) if lines else None)


def _flatten(geom: BaseGeometry):
    if hasattr(geom, "geoms"):  # Multi* and GeometryCollection, possibly nested
        for g in geom.geoms:
            yield from _flatten(g)
    elif not geom.is_empty:
        yield geom

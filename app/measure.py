"""Area and length in metres, never in degrees.

For each feature:
1. Project it to the UTM zone of its centroid (an EPSG code every GIS tool recognises).
2. UTM stretches the map by a scale factor k that depends on position. Around Bengaluru,
   k = 1.000577, so raw UTM areas are 0.115% too big. Dividing lengths by k and areas by k²
   gives the true size on the WGS84 ellipsoid.
3. Check against a geodesic calculation on the ellipsoid (GeographicLib, the same maths
   QGIS uses). If the two disagree by more than GEODESIC_TOLERANCE_PCT (very large features,
   near the poles, across the 180° meridian), report the geodesic value instead.
"""
from dataclasses import dataclass, field

import geopandas as gpd
import numpy as np
import shapely
from pyproj import Geod, Proj
from shapely.geometry import MultiLineString, MultiPolygon

from app import config

GEOD = Geod(ellps="WGS84")
DGMS_MAX_GRADE_PCT = 100 / 16  # Indian mine haul roads must not be steeper than 1 in 16


@dataclass
class Measurement:
    area_sq_m: float | None = None
    length_m: float | None = None
    projected_crs: str | None = None
    method: str | None = None
    geodesic_diff_pct: float | None = None
    notes: list[str] = field(default_factory=list)


def utm_epsg(lon: float, lat: float) -> int:
    zone = min(int((lon + 180) // 6) + 1, 60)  # min() keeps lon=180 in zone 60
    return (32600 if lat >= 0 else 32700) + zone


def measure_all(parts: list[tuple[MultiPolygon | None, MultiLineString | None]]) -> list[Measurement]:
    """Measure many features at once. parts[i] is (polygonal part, linear part) of feature i in EPSG:4326.

    Features are grouped by UTM zone so each zone needs one vectorised reprojection.
    Reprojecting one feature at a time was ~120x slower on 20,000 polygons.
    """
    results = [Measurement() for _ in parts]
    todo = [i for i, (poly, line) in enumerate(parts) if poly is not None or line is not None]
    if not todo:
        return results

    anchors = [parts[i][0] if parts[i][0] is not None else parts[i][1] for i in todo]
    centroids = shapely.centroid(anchors)
    lon, lat = shapely.get_x(centroids), shapely.get_y(centroids)
    zones = np.array([utm_epsg(x, y) for x, y in zip(lon, lat)])

    for epsg in np.unique(zones):
        sel = np.flatnonzero(zones == epsg)
        ids = [todo[j] for j in sel]
        k = Proj(f"EPSG:{epsg}").get_factors(lon[sel], lat[sel]).meridional_scale
        polys = gpd.GeoSeries([parts[i][0] for i in ids], crs=4326).to_crs(epsg)
        lines = gpd.GeoSeries([parts[i][1] for i in ids], crs=4326).to_crs(epsg)
        areas = polys.area.to_numpy() / k**2  # area scales with k², length with k
        lengths = lines.length.to_numpy() / k
        for i, area, length in zip(ids, areas, lengths):
            poly, line = parts[i]
            results[i] = _check_against_geodesic(
                poly, line, area if poly is not None else None, length if line is not None else None, int(epsg)
            )
    return results


def geodesic_area(poly: MultiPolygon) -> float:
    # The ellipsoid formula relies on ring direction (outer counter-clockwise, holes clockwise).
    # KML doesn't enforce it, and a wrongly-oriented hole gets added instead of subtracted.
    return abs(GEOD.geometry_area_perimeter(shapely.orient_polygons(poly))[0])


def _pct_diff(value: float | None, truth: float | None) -> float | None:
    if value is None or truth is None:
        return None
    if truth == 0:
        return 0.0 if value == 0 else float("inf")
    return (value - truth) / truth * 100


def _check_against_geodesic(poly, line, area, length, epsg: int) -> Measurement:
    geo_area = geodesic_area(poly) if poly is not None else None
    geo_length = GEOD.geometry_length(line) if line is not None else None
    diffs = [d for d in (_pct_diff(area, geo_area), _pct_diff(length, geo_length)) if d is not None]
    worst = max(diffs, key=lambda d: abs(d) if np.isfinite(d) else np.inf)

    if np.isfinite(worst) and abs(worst) <= config.GEODESIC_TOLERANCE_PCT:
        return Measurement(area, length, f"EPSG:{epsg}", "utm_scale_corrected", worst)

    shown = f"{worst:+.3f}%" if np.isfinite(worst) else "an invalid result"
    note = (f"UTM (EPSG:{epsg}) gave {shown} against the ellipsoid for this feature (too large, near a "
            "pole or crossing 180°), so the geodesic value is reported.")
    return Measurement(geo_area, geo_length, None, "geodesic", worst if np.isfinite(worst) else None, [note])


def slope_profile(line_3d: MultiLineString) -> tuple[float, float] | None:
    """For lines with real heights (haul roads, drone tracks): length along the slope and steepest grade %."""
    total, steepest = 0.0, 0.0
    for part in line_3d.geoms:
        coords = np.asarray(part.coords)
        if coords.shape[1] < 3:
            return None
        lon, lat, z = coords.T
        _, _, horizontal = GEOD.inv(lon[:-1], lat[:-1], lon[1:], lat[1:])
        rise = np.diff(z)
        total += float(np.hypot(horizontal, rise).sum())
        usable = horizontal > 1.0  # ignore near-duplicate points, they make grade meaningless
        if usable.any():
            steepest = max(steepest, float((np.abs(rise[usable]) / horizontal[usable]).max() * 100))
    return total, steepest

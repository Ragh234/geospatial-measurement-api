"""Measurement accuracy: known areas and lengths, and the geometry traps found in real files."""
import pytest
import shapely
from pyproj import Proj
from shapely.geometry import GeometryCollection, LineString, MultiLineString, Point, Polygon

from app import cleaning, measure
from tests.factories import BENGALURU, geodesic_area, line_east, square


def measure_one(geom):
    flat, notes = cleaning.clean(geom)
    m = measure.measure_all([cleaning.split_parts(flat)])[0]
    return m, notes


@pytest.mark.parametrize("place,lon,lat", [
    ("Bengaluru, 2.6 deg from the UTM 43N centre line", *BENGALURU),
    ("Kutch, near the UTM 42N centre line", 69.1, 23.3),
    ("Dhanbad coal belt, UTM 45N", 86.43, 23.79),
])
def test_one_square_km_is_one_square_km(place, lon, lat):
    m, _ = measure_one(square(lon, lat, 1000))
    assert m.method == "utm_scale_corrected"
    assert m.area_sq_m == pytest.approx(1_000_000, rel=1e-6)  # within 1 m²


def test_raw_utm_would_overstate_bengaluru_areas():
    """Why the scale-factor correction exists: plain UTM is 0.115% too big around Bengaluru."""
    k = Proj("EPSG:32643").get_factors(*BENGALURU).areal_scale
    assert (k - 1) * 100 == pytest.approx(0.115, abs=0.001)


def test_ten_km_line():
    m, _ = measure_one(line_east(*BENGALURU, 10_000))
    assert m.length_m == pytest.approx(10_000, abs=0.01)
    assert m.area_sq_m is None


def test_point_has_no_measurement():
    m, _ = measure_one(Point(*BENGALURU))
    assert m.method is None and m.area_sq_m is None and m.length_m is None


def test_bow_tie_keeps_both_halves():
    # Digitised with the vertices in the wrong order: the ring crosses itself.
    a, b = BENGALURU
    bow_tie = Polygon([(a, b), (a + 0.01, b + 0.01), (a + 0.01, b), (a, b + 0.01)])
    assert bow_tie.area == 0  # unrepaired, the two triangles cancel out
    left = Polygon([(a, b), (a + 0.005, b + 0.005), (a, b + 0.01)])
    right = Polygon([(a + 0.01, b), (a + 0.005, b + 0.005), (a + 0.01, b + 0.01)])
    m, notes = measure_one(bow_tie)
    assert m.area_sq_m == pytest.approx(geodesic_area(left) + geodesic_area(right), rel=1e-4)
    assert "make_valid" in notes[0]


def test_hole_is_subtracted_even_with_kml_ring_order():
    # KML doesn't fix ring direction: here outer ring and hole are both counter-clockwise.
    outer = square(*BENGALURU, 1000)
    hole = square(*BENGALURU, 400)
    poly = Polygon(outer.exterior.coords, [hole.exterior.coords])
    m, _ = measure_one(poly)
    assert m.area_sq_m == pytest.approx(1_000_000 - 160_000, rel=1e-5)


def test_label_point_does_not_hide_polygon_area():
    # Google Earth / ArcGIS exports often wrap a polygon and its label point in one MultiGeometry.
    m, _ = measure_one(GeometryCollection([Point(*BENGALURU), square(*BENGALURU, 200)]))
    assert m.area_sq_m == pytest.approx(40_000, rel=1e-6)


def test_z_values_are_ignored_for_map_area():
    flat = square(*BENGALURU, 500)
    raised = Polygon([(x, y, 900.0) for x, y in flat.exterior.coords])
    assert measure_one(raised)[0].area_sq_m == pytest.approx(measure_one(flat)[0].area_sq_m)


def test_feature_spanning_many_utm_zones_falls_back_to_geodesic():
    # A transmission line from Kutch to Kolkata crosses five UTM zones; one zone can't hold it.
    line = shapely.segmentize(LineString([(69.1, 23.3), (88.36, 22.57)]), 0.05)
    m, _ = measure_one(line)
    assert m.method == "geodesic"
    assert abs(m.geodesic_diff_pct) > 0.1  # what UTM would have been off by
    assert m.length_m == pytest.approx(measure.GEOD.geometry_length(line), rel=1e-12)


def test_feature_across_the_180th_meridian():
    # Fiji: stored as 179.995 -> -179.995. Planar maths would think it spans the whole globe.
    poly = Polygon([(179.995, -17.0), (-179.995, -17.0), (-179.995, -16.99), (179.995, -16.99)])
    m, _ = measure_one(poly)
    assert m.area_sq_m == pytest.approx(geodesic_area(poly), rel=1e-4)
    assert m.area_sq_m < 2_000_000


@pytest.mark.parametrize("geom", [None, Polygon(), LineString()])
def test_missing_or_empty_geometry_is_not_measured(geom):
    flat, notes = cleaning.clean(geom)
    assert flat is None and notes == []


def test_slope_profile_of_a_haul_road():
    # 1 km east while climbing 100 m: 10% grade, steeper than 1 in 16
    flat = line_east(*BENGALURU, 1000)
    (x0, y0), (x1, y1) = flat.coords
    road = MultiLineString([LineString([(x0, y0, 500), (x1, y1, 600)])])
    length_3d, grade = measure.slope_profile(road)
    assert length_3d == pytest.approx((1000**2 + 100**2) ** 0.5, abs=0.01)
    assert grade == pytest.approx(10, abs=0.01)
    assert grade > measure.DGMS_MAX_GRADE_PCT

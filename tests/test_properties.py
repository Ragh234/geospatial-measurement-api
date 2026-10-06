"""Property-based tests: Hypothesis generates hundreds of random inputs and checks rules that must
always hold, instead of a few hand-picked examples."""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shapely.geometry import LineString, Polygon

from app import cleaning, measure
from tests.factories import line_east, square

# Anywhere in mainland India, from a 10 m plot to a 20 km lease.
india_lon = st.floats(68.5, 97.0)
india_lat = st.floats(8.0, 35.0)
side = st.floats(10, 20_000)


def area_of(geom) -> float:
    flat, _ = cleaning.clean(geom)
    return measure.measure_all([cleaning.split_parts(flat)])[0].area_sq_m


def length_of(geom) -> float:
    flat, _ = cleaning.clean(geom)
    return measure.measure_all([cleaning.split_parts(flat)])[0].length_m


@settings(max_examples=150, deadline=None)
@given(india_lon, india_lat, side)
def test_square_area_is_side_squared_anywhere_in_india(lon, lat, s):
    assert area_of(square(lon, lat, s)) == pytest.approx(s * s, rel=1e-4)


@settings(max_examples=100, deadline=None)
@given(india_lon, india_lat, side, st.integers(0, 3), st.booleans())
def test_area_does_not_depend_on_vertex_order(lon, lat, s, start, reverse):
    ring = list(square(lon, lat, s).exterior.coords)[:-1]
    ring = ring[start:] + ring[:start]
    if reverse:
        ring.reverse()
    assert area_of(Polygon(ring)) == pytest.approx(area_of(square(lon, lat, s)), rel=1e-9)


@settings(max_examples=100, deadline=None)
@given(india_lon, india_lat, st.floats(100, 50_000), st.floats(0.05, 0.95))
def test_length_is_additive(lon, lat, total, split):
    line = line_east(lon, lat, total)
    (x0, y0), (x1, y1) = line.coords
    xm, ym = x0 + (x1 - x0) * split, y0 + (y1 - y0) * split
    parts = length_of(LineString([(x0, y0), (xm, ym)])) + length_of(LineString([(xm, ym), (x1, y1)]))
    # Each piece uses the scale factor at its own centroid, so pieces of a long line can differ from
    # the whole by about one part per million (Hypothesis found 3.6 cm on 36 km). Allow 10 ppm.
    assert parts == pytest.approx(length_of(line), rel=1e-5)

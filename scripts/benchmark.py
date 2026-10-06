"""How long does processing take as files grow?  python -m scripts.benchmark

Times the whole per-layer pipeline (clean, measure, geodesic check, GeoJSON, JSON-safe properties)
on random parcels spread over two UTM zones, plus the naive one-feature-at-a-time reprojection
it replaced.
"""
import time

import geopandas as gpd
import numpy as np
import shapely
from pyproj import Transformer
from shapely.ops import transform

from app import readers
from app.measure import utm_epsg
from app.processor import build_features


def parcels(n: int, seed: int = 0) -> gpd.GeoDataFrame:
    rng = np.random.default_rng(seed)
    lon, lat = rng.uniform(74, 80, n), rng.uniform(12, 16, n)  # Karnataka, zones 43 and 44
    size = rng.uniform(0.0002, 0.001, n)
    return gpd.GeoDataFrame({"plot_no": np.arange(n)}, geometry=shapely.box(lon, lat, lon + size, lat + size), crs=4326)


def main() -> None:
    print(f"{'features':>9} {'pipeline':>10} {'per feature':>12}")
    for n in (1_000, 10_000, 50_000):
        layer = readers.Layer("parcels", parcels(n), [False] * n)
        start = time.perf_counter()
        build_features(layer)
        took = time.perf_counter() - start
        print(f"{n:>9,} {took:>9.2f}s {took / n * 1e6:>10.0f}µs")

    gdf = parcels(10_000)
    start = time.perf_counter()
    for g in gdf.geometry:
        c = g.centroid
        transform(Transformer.from_crs(4326, utm_epsg(c.x, c.y), always_xy=True).transform, g).area
    print(f"\nnaive per-feature reprojection of 10,000 features: {time.perf_counter() - start:.2f}s "
          "(reprojection alone, before any other work)")


if __name__ == "__main__":
    main()

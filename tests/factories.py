"""Builders for test files. The demo files in samples/ are made with these too (scripts/make_samples.py),
so every sample is exactly what the tests check."""
import io
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import shapely
from pyproj import CRS, Geod, Transformer
from shapely.geometry import LineString, Polygon
from shapely.ops import transform

GEOD = Geod(ellps="WGS84")
BENGALURU = (77.59, 12.97)


def square(lon: float, lat: float, side_m: float) -> Polygon:
    """A square with sides of exactly side_m metres on the ground, centred on (lon, lat), in EPSG:4326."""
    local = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +ellps=WGS84 +units=m")
    to_lonlat = Transformer.from_crs(local, 4326, always_xy=True).transform
    h = side_m / 2
    return transform(to_lonlat, Polygon([(-h, -h), (h, -h), (h, h), (-h, h)]))


def line_east(lon: float, lat: float, length_m: float) -> LineString:
    """A straight line heading east that is exactly length_m metres long on the ellipsoid."""
    end_lon, end_lat, _ = GEOD.fwd(lon, lat, 90, length_m)
    return LineString([(lon, lat), (end_lon, end_lat)])


def geodesic_area(poly: Polygon) -> float:
    return abs(GEOD.geometry_area_perimeter(shapely.orient_polygons(poly))[0])


# ---------------------------------------------------------------- Shapefiles

def shapefile_zip(gdf: gpd.GeoDataFrame, name: str = "parcels", *, folder: str = "", drop: tuple[str, ...] = (),
                  mac_junk: bool = False) -> bytes:
    """Write gdf as a Shapefile and return it zipped. drop=(".prj",) leaves sidecar files out."""
    with tempfile.TemporaryDirectory() as tmp:
        gdf.to_file(Path(tmp) / f"{name}.shp", engine="pyogrio")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            if mac_junk:  # what macOS Finder's "Compress" adds
                zf.writestr(f"__MACOSX/{folder}._{name}.shp", b"\x00\x05\x16\x07 Mac OS X resource fork")
            for path in sorted(Path(tmp).iterdir()):
                if path.suffix.lower() not in drop:
                    zf.write(path, f"{folder}{path.name}")
        return buffer.getvalue()


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


# ---------------------------------------------------------------- KML

def coords(points) -> str:
    # 10 decimals so test areas aren't skewed by rounding (7 decimals is ~1 cm, enough to move 0.13 m²)
    return " ".join(",".join(f"{v:.10f}".rstrip("0").rstrip(".") for v in p) for p in points)


def kml_polygon(outer, holes=()) -> str:
    inner = "".join(f"<innerBoundaryIs><LinearRing><coordinates>{coords(h)}</coordinates></LinearRing></innerBoundaryIs>"
                    for h in holes)
    return (f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{coords(outer)}</coordinates>"
            f"</LinearRing></outerBoundaryIs>{inner}</Polygon>")


def kml_line(points, absolute: bool = False) -> str:
    mode = "<altitudeMode>absolute</altitudeMode>" if absolute else ""
    return f"<LineString>{mode}<coordinates>{coords(points)}</coordinates></LineString>"


def kml_point(lon: float, lat: float) -> str:
    return f"<Point><coordinates>{lon},{lat}</coordinates></Point>"


def placemark(name: str, geometry: str, data: dict | None = None, description: str | None = None) -> str:
    extended = ""
    if data:
        extended = "<ExtendedData>" + "".join(
            f'<Data name="{k}"><value>{v}</value></Data>' for k, v in data.items()) + "</ExtendedData>"
    desc = f"<description><![CDATA[{description}]]></description>" if description else ""
    return f"<Placemark><name>{name}</name>{desc}{extended}{geometry}</Placemark>"


def kml(*folders: tuple[str, list[str]]) -> bytes:
    body = "".join(f"<Folder><name>{name}</name>{''.join(marks)}</Folder>" for name, marks in folders)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">'
            f"<Document>{body}</Document></kml>").encode("utf-8")


def arcgis_description(attributes: dict) -> str:
    """The HTML table ArcGIS 'Layer To KML' writes instead of ExtendedData."""
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in attributes.items())
    return f'<html><body><table border="1"><tr><th colspan="2">Feature</th></tr>{rows}</table></body></html>'

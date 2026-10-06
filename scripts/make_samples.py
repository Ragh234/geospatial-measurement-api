"""Write the demo files in samples/. Run from the repo root:  python -m scripts.make_samples

Each file mirrors a kind of data a drone-survey company receives, and each one carries a
real-world quirk the API has to handle. The builders are the same ones the tests use.
"""
import io
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import LineString, Point, Polygon

from tests import factories as f

OUT = Path(__file__).resolve().parent.parent / "samples"
BELLARY = (76.55, 15.08)  # iron-ore belt near Sandur, Karnataka


def offset(lon, lat, east_m, north_m):
    """(lon, lat) moved east/north by a few hundred metres, on the ellipsoid."""
    lon2, lat2, _ = f.GEOD.fwd(lon, lat, 90, east_m)
    lon3, lat3, _ = f.GEOD.fwd(lon2, lat2, 0, north_m)
    return lon3, lat3


def mine_survey_kml() -> bytes:
    lon, lat = BELLARY
    lease = f.square(lon, lat, 1200)
    pit = f.square(*offset(lon, lat, -200, 150), 400)
    island = f.square(*offset(lon, lat, -200, 150), 100)  # unmined block inside the pit
    # A pit outline digitised in the wrong order: the ring crosses itself (a "bow-tie").
    a, b = offset(lon, lat, 250, -250)
    c, d = offset(lon, lat, 450, -50)
    bow_tie = [(a, b), (c, d), (c, b), (a, d), (a, b)]

    ramp = [(*offset(lon, lat, -500, -450), 610), (*offset(lon, lat, -300, -350), 628),
            (*offset(lon, lat, -150, -300), 645), (*offset(lon, lat, 0, -150), 652)]
    track = " ".join(f"<gx:coord>{x} {y} {z}</gx:coord>" for x, y, z in
                     [(*offset(lon, lat, -600, -600), 640), (*offset(lon, lat, 0, 0), 760), (*offset(lon, lat, 600, 600), 760)])
    when = "".join(f"<when>2026-10-01T05:0{i}:00Z</when>" for i in range(3))

    return f.kml(
        ("Mining lease", [
            # Exports often wrap the polygon with a label point in one MultiGeometry.
            f.placemark("Lease ML-2231",
                        f"<MultiGeometry>{f.kml_point(lon, lat)}{f.kml_polygon(lease.exterior.coords)}</MultiGeometry>",
                        data={"lease_id": "ML-2231", "mineral": "Iron ore", "lessee": "Demo Minerals Pvt Ltd"}),
        ]),
        ("Pits", [
            # KML doesn't enforce ring direction: this hole is wound the same way as the outer ring.
            f.placemark("Pit A", f.kml_polygon(pit.exterior.coords, holes=[island.exterior.coords]),
                        data={"bench_count": "6"}),
            f.placemark("Pit B (digitising error)", f.kml_polygon(bow_tie)),
        ]),
        ("Haul roads", [
            # ArcGIS "Layer To KML" puts attributes in an HTML table, not ExtendedData.
            f.placemark("Ramp R1", f.kml_line(ramp, absolute=True),
                        description=f.arcgis_description({"ROAD_ID": "R1", "WIDTH_M": "18", "SURFACE": "Murrum"})),
            f.placemark("Weighbridge", f.kml_point(*offset(lon, lat, -520, -470))),
        ]),
        ("Survey flights", [
            f"<Placemark><name>Flight 2026-10-01</name><gx:Track><altitudeMode>absolute</altitudeMode>{when}{track}"
            "</gx:Track></Placemark>",
            "<Placemark><name>Orthomosaic tile</name><GroundOverlay><LatLonBox><north>15.09</north><south>15.07</south>"
            "<east>76.56</east><west>76.54</west></LatLonBox></GroundOverlay></Placemark>",
        ]),
    )


def village_parcels_zip() -> bytes:
    lon, lat = 77.505, 13.055  # a village on the edge of Bengaluru
    sizes = [(30, 40), (25, 25), (60, 45), (18, 30), (40, 40)]
    parcels = []
    for i, (w, h) in enumerate(sizes):
        x0, y0 = offset(lon, lat, i * 70, 0)
        x1, y1 = offset(x0, y0, w, h)
        parcels.append(shapely.box(x0, y0, x1, y1))
    gdf = gpd.GeoDataFrame({
        "plot_no": pd.array([101, 102, 103, None, 105], dtype="Int64"),  # one plot number left blank
        "survey_no": ["45/1", "45/2", "46", "46/3", "47"],
        "owner": ["ರಾಮಪ್ಪ", "ಲಕ್ಷ್ಮಿ ದೇವಿ", "सीता देवी", "Mohammed Irfan", "ಸುರೇಶ್ ಗೌಡ"],
        "rec_sqm": [1200.0, 625.0, None, 540.0, 1600.0],  # DBF names max out at 10 chars
    }, geometry=parcels, crs=4326)
    # Zipped on a Mac, inside a folder, and without the .cpg that says the text is UTF-8.
    return f.shapefile_zip(gdf, "parcels", folder="svamitva_export/", drop=(".cpg",), mac_junk=True)


def cad_export_zip() -> bytes:
    """Parcels drawn in AutoCAD in UTM 43N metres and exported without a .prj."""
    x, y = 774_000, 1_434_000
    gdf = gpd.GeoDataFrame({"block": ["A", "B"]}, geometry=[
        Polygon([(x, y), (x + 100, y), (x + 100, y + 80), (x, y + 80)]),
        Polygon([(x + 120, y), (x + 220, y), (x + 220, y + 50), (x + 120, y + 50)]),
    ], crs=32643)
    return f.shapefile_zip(gdf, "site_plan", drop=(".prj",))


def transmission_line_zip() -> bytes:
    """A Bengaluru-Chennai line crossing from UTM zone 43 into 44, plus its towers: two layers in one zip."""
    line = shapely.segmentize(LineString([(77.59, 12.97), (78.69, 12.95), (80.27, 13.08)]), 0.02)
    towers = [Point(c) for c in list(line.coords)[::10]]
    files = {}
    for name, gdf in {
        "line": gpd.GeoDataFrame({"voltage_kv": [400], "circuit": ["Bengaluru-Chennai"]}, geometry=[line], crs=4326),
        "towers": gpd.GeoDataFrame({"tower_no": list(range(1, len(towers) + 1))}, geometry=towers, crs=4326),
    }.items():
        with zipfile.ZipFile(io.BytesIO(f.shapefile_zip(gdf, name))) as zf:
            files.update({n: zf.read(n) for n in zf.namelist()})
    return f.zip_bytes(files)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    samples = {
        "bellary_mine_survey.kml": mine_survey_kml(),
        "village_parcels.zip": village_parcels_zip(),
        "cad_export_utm43n_no_prj.zip": cad_export_zip(),
        "transmission_line.zip": transmission_line_zip(),
        "not_really_a.zip": b"This is a text file that was renamed to .zip",
    }
    for name, data in samples.items():
        (OUT / name).write_bytes(data)
        print(f"wrote samples/{name} ({len(data):,} bytes)")


if __name__ == "__main__":
    main()

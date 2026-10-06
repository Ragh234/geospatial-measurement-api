"""The HTTP contract: endpoints, response shapes and status codes."""
import geopandas as gpd
import pandas as pd
import pytest

from tests import factories as f


@pytest.fixture
def mine_kml():
    lease = f.square(*f.BENGALURU, 1000)
    return f.kml(
        ("Lease", [f.placemark("Lease boundary", f.kml_polygon(lease.exterior.coords), data={"lease_id": "ML-1"})]),
        ("Roads", [f.placemark("Haul road", f.kml_line(f.line_east(*f.BENGALURU, 2000).coords)),
                   f.placemark("Weighbridge", f.kml_point(*f.BENGALURU))]),
    )


def test_upload_returns_completed_file_info(upload, mine_kml):
    r = upload("survey.kml", mine_kml)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "COMPLETED"
    assert body["filename"] == "survey.kml"
    assert body["feature_count"] == 3
    assert body["crs"] == "EPSG:4326"
    assert body["layers"] == ["Lease", "Roads"]


def test_file_info_endpoint(client, upload, mine_kml):
    file_id = upload("survey.kml", mine_kml).json()["id"]
    r = client.get(f"/api/files/{file_id}/")
    assert r.status_code == 200
    assert {"id", "filename", "feature_count", "crs", "status"} <= r.json().keys()


def test_measurements_endpoint(client, upload, mine_kml):
    file_id = upload("survey.kml", mine_kml).json()["id"]
    body = client.get(f"/api/files/{file_id}/measurements/").json()
    lease, road, point = body["features"]

    assert lease["index"] == 0 and lease["geometry_type"] == "Polygon" and lease["crs"] == "EPSG:4326"
    assert lease["geometry"]["type"] == "Polygon"
    assert lease["properties"]["lease_id"] == "ML-1"
    m = lease["measurement"]
    assert m["area_sq_m"] == pytest.approx(1_000_000, abs=1)
    assert m["area_hectares"] == pytest.approx(100, abs=0.001)
    assert m["area_acres"] == pytest.approx(247.105, abs=0.001)
    assert m["projected_crs"] == "EPSG:32643" and m["method"] == "utm_scale_corrected"

    assert road["measurement"]["length_m"] == pytest.approx(2000, abs=0.01)
    assert road["measurement"]["length_km"] == pytest.approx(2.0, abs=0.0001)
    assert point["measurement"] is None and "no area or length" in point["notes"][0]
    assert body["totals"]["area_hectares"] == pytest.approx(100, abs=0.001)


def test_geojson_export(client, upload, mine_kml):
    file_id = upload("survey.kml", mine_kml).json()["id"]
    r = client.get(f"/api/files/{file_id}/measurements/", params={"format": "geojson"})
    assert r.headers["content-type"].startswith("application/geo+json")
    fc = r.json()
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 3
    assert fc["features"][0]["properties"]["area_sq_m"] == pytest.approx(1_000_000, abs=1)


def test_pagination(client, upload, mine_kml):
    file_id = upload("survey.kml", mine_kml).json()["id"]
    body = client.get(f"/api/files/{file_id}/measurements/", params={"limit": 1, "offset": 1}).json()
    assert [feat["index"] for feat in body["features"]] == [1]
    assert body["totals"]["length_m"] == pytest.approx(2000, abs=0.01)  # totals still cover the whole file


def test_routes_work_without_trailing_slash(client, mine_kml):
    r = client.post("/api/files", files={"file": ("survey.kml", mine_kml)}, follow_redirects=False)
    assert r.status_code == 201
    file_id = r.json()["id"]
    assert client.get(f"/api/files/{file_id}", follow_redirects=False).status_code == 200
    assert client.get(f"/api/files/{file_id}/measurements", follow_redirects=False).status_code == 200


def test_zipped_shapefile_with_blank_cells(client, upload):
    gdf = gpd.GeoDataFrame(
        {"plot_no": pd.array([7, None], dtype="Int64"), "area_ha": [0.25, None]},
        geometry=[f.square(*f.BENGALURU, 50), f.square(77.6, 12.97, 50)], crs=4326,
    )
    r = upload("plots.zip", f.shapefile_zip(gdf))
    assert r.status_code == 201
    features = client.get(f"/api/files/{r.json()['id']}/measurements/").json()["features"]
    assert features[0]["properties"] == {"plot_no": 7, "area_ha": 0.25}
    assert features[1]["properties"] == {"plot_no": None, "area_ha": None}  # NaN would have been a 500


def test_source_crs_for_shapefile_without_prj(upload):
    gdf = gpd.GeoDataFrame(geometry=[f.square(*f.BENGALURU, 100)], crs=4326).to_crs(32643)
    data = f.shapefile_zip(gdf, drop=(".prj",))
    failed = upload("cad.zip", data)
    assert failed.status_code == 422 and failed.json()["status"] == "FAILED" and "source_crs" in failed.json()["detail"]
    ok = upload("cad.zip", data, source_crs="EPSG:32643")
    assert ok.status_code == 201 and ok.json()["crs"] == "EPSG:32643"


def test_invalid_source_crs(upload, mine_kml):
    assert upload("survey.kml", mine_kml, source_crs="EPSG:not-a-code").status_code == 400


@pytest.mark.parametrize("filename,data,status", [
    ("notes.txt", b"hello", 415),
    ("survey.kml", b"", 400),
    ("parcels.zip", b"definitely not a zip", 400),
    ("parcels.zip", f.zip_bytes({"readme.txt": b"no shapefile here"}), 400),
    ("survey.kml", b"<kml><Document><Placemark><Point>broken", 422),
])
def test_bad_uploads_get_clear_4xx(upload, filename, data, status):
    r = upload(filename, data)
    assert r.status_code == status
    assert r.json()["detail"]


def test_oversized_upload_is_rejected(upload):
    # conftest sets the limit to 1 MB
    assert upload("big.kml", b"<kml>" + b" " * (2 * 1024 * 1024)).status_code == 413


def test_failed_file_is_stored_and_has_no_measurements(client, upload):
    r = upload("broken.kml", b"<kml><Document><Placemark><Point>broken")
    file_id = r.json()["id"]
    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "FAILED" and info["error"]
    assert client.get(f"/api/files/{file_id}/measurements/").status_code == 409


def test_unknown_file_id(client):
    assert client.get("/api/files/does-not-exist/").status_code == 404
    assert client.get("/api/files/does-not-exist/measurements/").status_code == 404


def test_one_bad_feature_does_not_fail_the_file(client, upload):
    good = f.placemark("Pit", f.kml_polygon(f.square(*f.BENGALURU, 100).exterior.coords))
    overlay = ("<Placemark><name>Ortho tile</name><GroundOverlay><LatLonBox><north>13</north><south>12.9</south>"
               "<east>77.6</east><west>77.5</west></LatLonBox></GroundOverlay></Placemark>")
    r = upload("site.kml", f.kml(("Site", [good, overlay])))
    assert r.json()["status"] == "COMPLETED"
    pit, tile = client.get(f"/api/files/{r.json()['id']}/measurements/").json()["features"]
    assert pit["measurement"]["area_sq_m"] == pytest.approx(10_000, abs=0.1)
    assert tile["error"] and tile["measurement"] is None

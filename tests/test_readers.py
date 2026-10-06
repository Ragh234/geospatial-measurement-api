"""Reading real-world files: the quirks that break naive readers."""
import io
import zipfile

import geopandas as gpd
import pandas as pd
import pytest
from pyproj import CRS
from app import config, readers
from app.errors import InputError
from tests import factories as f


def read(tmp_path, filename: str, data: bytes, source_crs=None) -> readers.ReadResult:
    path = tmp_path / filename
    path.write_bytes(data)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    ext = path.suffix.lower()
    readers.check_upload(path, ext)
    return readers.read_upload(path, ext, work, CRS.from_user_input(source_crs) if source_crs else None)


@pytest.fixture
def parcels():
    return gpd.GeoDataFrame(
        {"plot_no": pd.array([101, None], dtype="Int64"), "owner": ["ರಾಮಪ್ಪ", "सीता देवी"]},
        geometry=[f.square(*f.BENGALURU, 30), f.square(77.6, 12.97, 40)],
        crs=4326,
    )


def test_zip_from_a_mac_with_nested_folder(tmp_path, parcels):
    data = f.shapefile_zip(parcels, folder="export/survey/", mac_junk=True)
    result = read(tmp_path, "parcels.zip", data)
    assert [layer.name for layer in result.layers] == ["parcels"]
    assert len(result.layers[0].gdf) == 2


def test_missing_cpg_keeps_kannada_and_hindi_names(tmp_path, parcels):
    result = read(tmp_path, "parcels.zip", f.shapefile_zip(parcels, drop=(".cpg",)))
    assert list(result.layers[0].gdf["owner"]) == ["ರಾಮಪ್ಪ", "सीता देवी"]
    assert any("UTF-8" in w for w in result.warnings)


def test_blank_cell_does_not_turn_plot_numbers_into_floats(tmp_path, parcels):
    gdf = read(tmp_path, "parcels.zip", f.shapefile_zip(parcels)).layers[0].gdf
    assert gdf["plot_no"].iloc[0] == 101 and str(gdf["plot_no"].dtype) == "Int64"
    assert pd.isna(gdf["plot_no"].iloc[1])


def test_missing_prj_assumes_lon_lat_with_warning(tmp_path, parcels):
    result = read(tmp_path, "parcels.zip", f.shapefile_zip(parcels, drop=(".prj",)))
    assert readers.crs_label(result.layers[0].gdf.crs) == "EPSG:4326"
    assert any("no .prj" in w for w in result.warnings)


def test_projected_coordinates_without_prj_ask_for_source_crs(tmp_path, parcels):
    data = f.shapefile_zip(parcels.to_crs(32643), drop=(".prj",))  # a CAD export in UTM 43N
    with pytest.raises(InputError, match="source_crs") as err:
        read(tmp_path, "cad.zip", data)
    assert err.value.status_code == 422
    result = read(tmp_path, "cad.zip", data, source_crs="EPSG:32643")
    assert readers.crs_label(result.layers[0].gdf.crs) == "EPSG:32643"


def test_missing_shx_is_rebuilt(tmp_path, parcels):
    result = read(tmp_path, "parcels.zip", f.shapefile_zip(parcels, drop=(".shx",)))
    assert len(result.layers[0].gdf) == 2
    assert any(".shx" in w for w in result.warnings)


def test_missing_dbf_still_gives_geometry(tmp_path, parcels):
    result = read(tmp_path, "parcels.zip", f.shapefile_zip(parcels, drop=(".dbf",)))
    assert len(result.layers[0].gdf) == 2
    assert any(".dbf" in w for w in result.warnings)


def test_two_shapefiles_in_one_zip_become_two_layers(tmp_path, parcels):
    with_roads = f.zip_bytes({
        **_unzip(f.shapefile_zip(parcels, "parcels")),
        **_unzip(f.shapefile_zip(gpd.GeoDataFrame(geometry=[f.line_east(*f.BENGALURU, 500)], crs=4326), "roads")),
    })
    result = read(tmp_path, "village.zip", with_roads)
    assert sorted(layer.name for layer in result.layers) == ["parcels", "roads"]


def test_zip_without_shapefile(tmp_path):
    with pytest.raises(InputError, match="No .shp"):
        read(tmp_path, "notes.zip", f.zip_bytes({"readme.txt": b"hello"}))


def test_corrupt_zip(tmp_path):
    with pytest.raises(InputError, match="not a zip"):
        read(tmp_path, "broken.zip", b"this is not a zip")
    with pytest.raises(InputError, match="corrupt"):
        read(tmp_path, "truncated.zip", f.zip_bytes({"a.shp": b"x" * 100})[:30])


def test_zip_slip_is_rejected(tmp_path):
    with pytest.raises(InputError, match="unsafe"):
        read(tmp_path, "evil.zip", f.zip_bytes({"../../escape.shp": b"x"}))


def test_zip_bomb_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_UNZIPPED_BYTES", 1000)
    with pytest.raises(InputError, match="zip bomb") as err:
        read(tmp_path, "bomb.zip", f.zip_bytes({"big.shp": b"0" * 5000}))
    assert err.value.status_code == 413


def test_every_kml_folder_is_read(tmp_path):
    data = f.kml(
        ("Lease", [f.placemark("Lease", f.kml_polygon(f.square(*f.BENGALURU, 500).exterior.coords))]),
        ("Roads", [f.placemark("R1", f.kml_line(f.line_east(*f.BENGALURU, 300).coords)),
                   f.placemark("Gate", f.kml_point(*f.BENGALURU))]),
    )
    result = read(tmp_path, "mine.kml", data)
    assert [layer.name for layer in result.layers] == ["Lease", "Roads"]
    assert sum(len(layer.gdf) for layer in result.layers) == 3


def test_kml_attributes_from_extended_data_and_arcgis_html(tmp_path):
    poly = f.kml_polygon(f.square(*f.BENGALURU, 100).exterior.coords)
    data = f.kml(("Pits", [
        f.placemark("Pit 1", poly, data={"bench": "B3"}),
        f.placemark("Pit 2", poly, description=f.arcgis_description({"PIT_ID": "P-2", "DEPTH_M": "42"})),
    ]))
    gdf = read(tmp_path, "pits.kml", data).layers[0].gdf
    assert gdf.loc[0, "bench"] == "B3"
    assert gdf.loc[1, "PIT_ID"] == "P-2" and gdf.loc[1, "DEPTH_M"] == "42"
    assert not {"tessellate", "extrude", "visibility", "drawOrder"} & set(gdf.columns)


def test_kml_altitude_mode_decides_if_z_is_a_height(tmp_path):
    pts = [(77.59, 12.97, 500), (77.6, 12.97, 560)]
    data = f.kml(("Roads", [f.placemark("ramp", f.kml_line(pts, absolute=True)),
                            f.placemark("draped", f.kml_line(pts))]))
    assert read(tmp_path, "roads.kml", data).layers[0].z_is_elevation == [True, False]


def test_kmz(tmp_path):
    inner = f.kml(("Lease", [f.placemark("L", f.kml_polygon(f.square(*f.BENGALURU, 100).exterior.coords))]))
    result = read(tmp_path, "site.kmz", f.zip_bytes({"doc.kml": inner}))
    assert result.file_format == "kmz" and len(result.layers[0].gdf) == 1


def test_kml_with_no_placemarks(tmp_path):
    with pytest.raises(InputError, match="no features"):
        read(tmp_path, "empty.kml", f.kml(("Empty", [])))


def test_wrong_extension_and_empty_file(tmp_path):
    with pytest.raises(InputError, match="does not look like KML"):
        read(tmp_path, "parcels.kml", f.zip_bytes({"a.shp": b"x"}))
    with pytest.raises(InputError, match="empty"):
        read(tmp_path, "empty.kml", b"")


def test_html_table_parser_handles_nested_tables():
    html = "<table><tr><td><table><tr><td>A</td><td>1</td></tr></table></td></tr><tr><td>B</td><td>2</td></tr></table>"
    assert readers.parse_html_attributes(html) == {"A": "1", "B": "2"}
    assert readers.parse_html_attributes("just text") == {}


def _unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}

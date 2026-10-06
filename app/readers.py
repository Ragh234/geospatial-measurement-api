"""Turn an uploaded file into GeoDataFrames, one per layer.

Real files are messy, so each reader deals with the quirks that show up in practice
(Mac zips, missing sidecar files, KML folders, attributes hidden in HTML) and reports
anything it had to assume as a warning instead of failing.
"""
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from pyproj import CRS

from app import config
from app.errors import InputError

# Without this GDAL refuses to open a Shapefile whose .shx index is missing; with it, GDAL rebuilds it.
pyogrio.set_gdal_config_options({"SHAPE_RESTORE_SHX": True})

# Columns LIBKML adds to every placemark. They describe display/styling, not the feature.
KML_DISPLAY_COLUMNS = ["tessellate", "extrude", "visibility", "drawOrder", "icon", "timestamp", "begin", "end"]


@dataclass
class Layer:
    name: str
    gdf: gpd.GeoDataFrame  # geometry still in the file's own CRS
    z_is_elevation: list[bool]  # per feature: is Z a real height? (needed for 3D length)


@dataclass
class ReadResult:
    file_format: str
    layers: list[Layer]
    warnings: list[str] = field(default_factory=list)


def crs_label(crs: CRS | None) -> str | None:
    if crs is None:
        return None
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg else crs.name


def check_upload(path: Path, ext: str) -> None:
    """Cheap checks before any parsing: empty, too big, or content that doesn't match the extension."""
    size = path.stat().st_size
    if size == 0:
        raise InputError("The uploaded file is empty.")
    if size > config.MAX_UPLOAD_BYTES:
        raise InputError(f"The file is larger than the {config.MAX_UPLOAD_MB} MB limit.", 413)
    with path.open("rb") as f:
        head = f.read(4096)
    if ext in (".zip", ".kmz") and not head.startswith(b"PK\x03\x04"):
        raise InputError(f"The file is named {ext} but is not a zip archive.")
    if ext == ".kml" and b"<kml" not in head.lower():
        raise InputError("The file is named .kml but does not look like KML.")


def read_upload(path: Path, ext: str, work_dir: Path, source_crs: CRS | None) -> ReadResult:
    result = READERS[ext](path, work_dir, source_crs)
    if not any(len(layer.gdf) for layer in result.layers):
        raise InputError("The file contains no features.", 422)
    return result


# ---------------------------------------------------------------- zip handling

def _is_junk(name: str) -> bool:
    """Files that zip tools add but that are not data, e.g. macOS's __MACOSX/._parcels.shp."""
    p = Path(name)
    return "__MACOSX" in p.parts or p.name.startswith("._") or p.name in (".DS_Store", "Thumbs.db")


def extract_zip(path: Path, dest: Path) -> Path:
    try:
        with zipfile.ZipFile(path) as zf:
            members = [m for m in zf.infolist() if not m.is_dir() and not _is_junk(m.filename)]
            if sum(m.file_size for m in members) > config.MAX_UNZIPPED_BYTES:
                raise InputError("The zip expands to far more than its own size (possible zip bomb).", 413)
            root = dest.resolve()
            for m in members:
                # "Zip slip": a name like ../../app/main.py would write outside our folder.
                if not (dest / m.filename).resolve().is_relative_to(root):
                    raise InputError("The zip contains unsafe file paths.")
                zf.extract(m, dest)
    except (zipfile.BadZipFile, EOFError):
        raise InputError("The zip archive is corrupt or incomplete.")
    return dest


def _files_with_suffix(folder: Path, suffix: str) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == suffix)


# ---------------------------------------------------------------- Shapefile

def read_zip(path: Path, work_dir: Path, source_crs: CRS | None) -> ReadResult:
    folder = extract_zip(path, work_dir / "unzipped")
    shps = _files_with_suffix(folder, ".shp")
    if not shps:
        kmls = _files_with_suffix(folder, ".kml")
        if kmls:  # someone zipped a KML instead of a Shapefile; read it rather than refuse
            result = read_kml(kmls[0], work_dir, source_crs)
            result.warnings.append("The zip had no Shapefile, so the KML inside it was read instead.")
            return result
        raise InputError("No .shp file found in the zip. A Shapefile needs at least .shp, .shx and .dbf files.")

    result = ReadResult("shapefile", [])
    for shp in shps:  # several Shapefiles in one zip become several layers
        result.layers.append(_read_shapefile(shp, source_crs, result.warnings))
    return result


def _sidecar(shp: Path, suffix: str) -> Path | None:
    """Find parcels.dbf / parcels.DBF next to parcels.shp, whatever the case."""
    for p in shp.parent.iterdir():
        if p.stem == shp.stem and p.suffix.lower() == suffix:
            return p
    return None


def _read_shapefile(shp: Path, source_crs: CRS | None, warnings: list[str]) -> Layer:
    name = shp.stem
    if _sidecar(shp, ".shx") is None:
        warnings.append(f"{name}: the .shx index was missing and has been rebuilt.")
    dbf = _sidecar(shp, ".dbf")
    if dbf is None:
        warnings.append(f"{name}: the .dbf file was missing, so features have no attributes.")

    encoding = None
    if dbf is not None and _sidecar(shp, ".cpg") is None:
        encoding = _guess_dbf_encoding(dbf)
        if encoding:
            warnings.append(f"{name}: no .cpg file, so the attribute text encoding was detected as {encoding}.")

    try:
        gdf = pyogrio.read_dataframe(shp, encoding=encoding)
    except Exception as exc:
        raise InputError(f"{name}: could not read this Shapefile ({_short_reason(exc)}).", 422)

    gdf = _restore_integer_columns(gdf, shp, encoding)
    gdf = _resolve_crs(gdf, name, source_crs, warnings)
    return Layer(name, gdf, z_is_elevation=[True] * len(gdf))  # Shapefile Z values are elevations


def _guess_dbf_encoding(dbf: Path) -> str | None:
    """Without a .cpg GDAL assumes Latin-1, which garbles Kannada or Hindi names.
    If the attribute bytes are valid UTF-8 they almost certainly are UTF-8."""
    data = dbf.read_bytes()
    header_length = int.from_bytes(data[8:10], "little")
    records = data[header_length:]
    if records.isascii():
        return None  # plain ASCII reads the same in every encoding
    try:
        records.decode("utf-8")
        return "UTF-8"
    except UnicodeDecodeError:
        return "CP1252"


def _restore_integer_columns(gdf: gpd.GeoDataFrame, shp: Path, encoding: str | None) -> gpd.GeoDataFrame:
    """A blank cell turns a whole integer column into floats (plot 12 becomes 12.0). Put integers back."""
    info = pyogrio.read_info(shp, encoding=encoding)
    for column, dtype in zip(info["fields"], info["dtypes"]):
        if dtype.startswith("int") and column in gdf and gdf[column].dtype.kind == "f":
            gdf[column] = gdf[column].astype("Int64")
    return gdf


def _resolve_crs(gdf: gpd.GeoDataFrame, name: str, source_crs: CRS | None, warnings: list[str]) -> gpd.GeoDataFrame:
    if source_crs is not None:
        if gdf.crs is not None and gdf.crs != source_crs:
            warnings.append(f"{name}: the file says {crs_label(gdf.crs)}, but {crs_label(source_crs)} was used as requested.")
        return gdf.set_crs(source_crs, allow_override=True)
    if gdf.crs is not None:
        return gdf

    minx, miny, maxx, maxy = gdf.total_bounds
    if np.isnan(minx) or (-180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90):
        warnings.append(f"{name}: no .prj file, so coordinates were assumed to be EPSG:4326 (longitude/latitude).")
        return gdf.set_crs(4326)
    # Typical of CAD exports: metres in some projected CRS, but no .prj to say which one.
    raise InputError(
        f"{name}: there is no .prj file and the coordinates (x={minx:.0f}, y={miny:.0f}) are not "
        "longitude/latitude, so the CRS cannot be guessed. Upload again with ?source_crs=<code>, "
        "for example ?source_crs=EPSG:32643 for UTM zone 43N.",
        422,
    )


# ---------------------------------------------------------------- KML / KMZ

def read_kml(path: Path, work_dir: Path, source_crs: CRS | None) -> ReadResult:
    result = ReadResult("kml", [])
    try:
        # Each KML <Folder> is a separate layer. Reading only the default layer silently drops the rest.
        names = [name for name, _ in pyogrio.list_layers(path)]
        for name in names:
            gdf = pyogrio.read_dataframe(path, layer=name)
            if len(gdf):
                result.layers.append(_tidy_kml_layer(name, gdf))
    except Exception as exc:
        raise InputError(f"Could not read this file as KML ({_short_reason(exc)}).", 422)
    if source_crs is not None:
        result.warnings.append("KML coordinates are always longitude/latitude (EPSG:4326), so source_crs was ignored.")
    return result


def read_kmz(path: Path, work_dir: Path, source_crs: CRS | None) -> ReadResult:
    kmls = _files_with_suffix(extract_zip(path, work_dir / "kmz"), ".kml")
    if not kmls:
        raise InputError("The KMZ archive does not contain a .kml file.")
    result = read_kml(kmls[0], work_dir, source_crs)
    result.file_format = "kmz"
    return result


def _tidy_kml_layer(name: str, gdf: gpd.GeoDataFrame) -> Layer:
    # KML heights only mean something with altitudeMode=absolute; the default clamps to the ground.
    modes = gdf["altitudeMode"] if "altitudeMode" in gdf else pd.Series(None, index=gdf.index)
    z_is_elevation = (modes == "absolute").tolist()
    gdf = gdf.drop(columns=[c for c in KML_DISPLAY_COLUMNS + ["altitudeMode"] if c in gdf])

    # ArcGIS "Layer to KML" puts attributes in an HTML table inside <description>. Unpack them.
    if "description" in gdf:
        parsed = gdf["description"].map(parse_html_attributes)
        has_table = parsed.map(bool)
        if has_table.any():
            table = pd.DataFrame(parsed.tolist(), index=gdf.index)
            gdf.loc[has_table, "description"] = None
            for column in table.columns:
                gdf[column if column not in gdf else f"{column}_2"] = table[column]

    empty = [c for c in gdf.columns if c != gdf.geometry.name and gdf[c].isna().all()]
    return Layer(name, gdf.drop(columns=empty), z_is_elevation)


class _TableRows(HTMLParser):
    """Collects the text of each <td>/<th> cell, row by row."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_html_attributes(description) -> dict:
    """Read 'NAME | value' rows out of an HTML table. Anything else returns {}."""
    if not isinstance(description, str) or "<td" not in description.lower():
        return {}
    parser = _TableRows()
    parser.feed(description)
    return {row[0]: row[1] for row in parser.rows if len(row) == 2 and row[0]}


def _short_reason(exc: Exception) -> str:
    """GDAL messages contain our temp paths; keep the reason, drop the paths."""
    text = re.sub(r"(?:[A-Za-z]:)?[\\/]\S+", "<file>", str(exc))
    return text[:200]


READERS = {".zip": read_zip, ".kml": read_kml, ".kmz": read_kmz}

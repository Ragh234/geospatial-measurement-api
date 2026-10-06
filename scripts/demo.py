"""Call every endpoint against a running server with the files in samples/.

    python -m scripts.demo                      # default http://127.0.0.1:8000
    python -m scripts.demo http://localhost:8000
"""
import json
import sys
from pathlib import Path

import httpx2 as httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def show(title: str, response: httpx.Response) -> dict:
    print(f"\n### {title}\n{response.request.method} {response.request.url.path}"
          f"{'?' + response.request.url.query.decode() if response.request.url.query else ''} -> {response.status_code}")
    body = response.json()
    print(json.dumps(body, indent=2, ensure_ascii=False)[:1500])
    return body


def table(measurements: dict) -> None:
    print(f"\n{'#':>2}  {'layer':16} {'name':26} {'type':18} {'area (ha)':>10} {'length (m)':>11}  method / notes")
    for feat in measurements["features"]:
        m = feat["measurement"] or {}
        name = str(feat["properties"].get("Name") or feat["properties"].get("owner") or feat["properties"].get("block") or "")[:26]
        extra = m.get("projected_crs") or m.get("method") or ""
        if feat["error"]:
            extra = "ERROR: " + feat["error"][:60]
        elif feat["notes"]:
            extra += "  | " + feat["notes"][0][:70]
        area = "" if m.get("area_hectares") is None else f"{m['area_hectares']:.4f}"
        length = "" if m.get("length_m") is None else f"{m['length_m']:,.1f}"
        print(f"{feat['index']:>2}  {feat['layer'][:16]:16} {name:26} {str(feat['geometry_type']):18} {area:>10} {length:>11}  {extra}")
    print("totals:", measurements["totals"])


def upload(client: httpx.Client, name: str, **params) -> httpx.Response:
    with (SAMPLES / name).open("rb") as fh:
        return client.post("/api/files/", files={"file": (name, fh)}, params=params)


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=60) as client:
        show("Health", client.get("/health"))

        for name in ("bellary_mine_survey.kml", "village_parcels.zip", "transmission_line.zip"):
            info = show(f"Upload {name}", upload(client, name))
            show("File info", client.get(f"/api/files/{info['id']}/"))
            table(client.get(f"/api/files/{info['id']}/measurements/").json())

        failed = show("CAD export without .prj (coordinates are metres, not degrees)",
                      upload(client, "cad_export_utm43n_no_prj.zip"))
        show("The FAILED record is kept", client.get(f"/api/files/{failed['id']}/"))
        show("...and has no measurements", client.get(f"/api/files/{failed['id']}/measurements/"))
        info = show("Same file with ?source_crs=EPSG:32643", upload(client, "cad_export_utm43n_no_prj.zip", source_crs="EPSG:32643"))
        table(client.get(f"/api/files/{info['id']}/measurements/").json())

        first = client.get(f"/api/files/{info['id']}/measurements/", params={"limit": 1}).json()["features"][0]
        print("\n### One feature in full\n" + json.dumps(first, indent=2, ensure_ascii=False))
        fc = client.get(f"/api/files/{info['id']}/measurements/", params={"format": "geojson"})
        print(f"\n### GeoJSON export -> {fc.status_code} {fc.headers['content-type']}, {len(fc.json()['features'])} features")

        show("Renamed text file", upload(client, "not_really_a.zip"))
        show("Wrong extension", client.post("/api/files/", files={"file": ("notes.txt", b"hello")}))
        show("Empty file", client.post("/api/files/", files={"file": ("empty.kml", b"")}))
        show("Unknown id", client.get("/api/files/does-not-exist/"))
        print(f"\nOpen the map: {BASE}/viewer/{info['id']}")


if __name__ == "__main__":
    main()

"""Every tunable limit in one place. Each can be overridden with an environment variable."""
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
TMP_DIR = DATA_DIR / "tmp"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'geomeasure.db').as_posix()}")

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
# A zip may legitimately expand a few times; anything far beyond that is a zip bomb.
MAX_UNZIPPED_BYTES = 10 * MAX_UPLOAD_BYTES

ALLOWED_EXTENSIONS = {".zip", ".kml", ".kmz"}

# If the projected (UTM) result and the geodesic result differ by more than this,
# the geodesic value is used. 0.01% is 1 cm per 100 m.
GEODESIC_TOLERANCE_PCT = 0.01

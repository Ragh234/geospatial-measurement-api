import os
import tempfile

# Must be set before the app is imported: a throwaway database and a 1 MB upload limit.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="geomeasure-test-")
os.environ["MAX_UPLOAD_MB"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def upload(client):
    """upload("name.kml", data, source_crs=None) -> response"""
    def _upload(filename: str, data: bytes, **params):
        return client.post("/api/files/", files={"file": (filename, data)}, params=params)
    return _upload

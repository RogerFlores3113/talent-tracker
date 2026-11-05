import pytest

from talent.store import Store

# The app only answers on a local address, and checks that a POST comes from its own page.
# Tests talk to it the way the app's own window does.
LOCAL_URL = "http://127.0.0.1:8765"


def local_client(store, **kwargs):
    from fastapi.testclient import TestClient

    from talent.web import create_app

    return TestClient(create_app(store), base_url=LOCAL_URL, headers={"Origin": LOCAL_URL}, **kwargs)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TALENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TYPESENSE_URL", raising=False)
    monkeypatch.delenv("TYPESENSE_API_KEY", raising=False)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "talent.db")
    yield s
    s.close()


@pytest.fixture
def client(store):
    return local_client(store)

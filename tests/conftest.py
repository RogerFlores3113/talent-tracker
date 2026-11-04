import pytest

from talent.store import Store


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

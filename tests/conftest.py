import re
import pytest

from talent.store import Store

# The app only answers on a local address, and checks that a POST comes from its own page.
# Tests talk to it the way the app's own window does.
LOCAL_URL = "http://127.0.0.1:8765"


def local_client(store, origin: bool = True, unlocked: bool = True, **kwargs):
    """origin: send our own Origin header, like the app's own pages. unlocked: carry the
    app window's key cookie, like a browser opened from the app icon."""
    from fastapi.testclient import TestClient

    from talent import paths
    from talent.web import create_app, key_cookie_name

    c = TestClient(create_app(store), base_url=LOCAL_URL, headers={"Origin": LOCAL_URL} if origin else None, **kwargs)
    if unlocked:
        key = paths.app_key()
        c.cookies.set(key_cookie_name(key), key)
    return c


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TALENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TYPESENSE_URL", raising=False)
    monkeypatch.delenv("TYPESENSE_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_card_key_gap(monkeypatch):
    """A Shortlist card key waits a moment before it works; tests press at once (the gap has its own test)."""
    from talent import mcp_server
    monkeypatch.setattr(mcp_server, "CARD_MIN_GAP", 0)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "talent.db")
    yield s
    s.close()


@pytest.fixture
def client(store):
    return local_client(store)


def uncaution(text: str) -> str:
    """A tool reply without its leading "Caution: ..." line (cycle 8 C adds one when text looks like instructions)."""
    return re.sub(r"\A(?:Caution: [^\n]*\n)+", "", text)


def flash_of(response, key: str = "notice") -> str:
    """The one-line message (notice, flash_error or error) the app sent after a form. It travels in
    a short-lived signed cookie, not the address, so names never land in browser history."""
    from talent.web import decode_flash

    for r in [response, *getattr(response, "history", [])]:
        for raw in r.headers.get_list("set-cookie"):
            name, _, rest = raw.partition("=")
            if name.startswith("talent_flash_"):
                return decode_flash(rest.split(";", 1)[0]).get(key, "")
    return ""

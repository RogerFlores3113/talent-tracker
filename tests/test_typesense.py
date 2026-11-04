import json

import httpx

from talent.typesense_index import SearchService, TypesenseIndex


class FakeTypesense:
    """Just enough of Typesense's REST API to exercise sync and search."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.collection = False
        self.up = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("down", request=request)
        assert request.headers["X-TYPESENSE-API-KEY"] == "k"
        path, m = request.url.path, request.method
        if path == "/health":
            return httpx.Response(200, json={"ok": True})
        if path == "/collections/candidates" and m == "GET":
            return httpx.Response(200 if self.collection else 404, json={})
        if path == "/collections/candidates" and m == "DELETE":
            self.docs.clear(); self.collection = False
            return httpx.Response(200, json={})
        if path == "/collections" and m == "POST":
            self.collection = True
            return httpx.Response(201, json={})
        if path.endswith("/documents/import"):
            lines = [json.loads(l) for l in request.content.decode().splitlines() if l]
            for d in lines:
                self.docs[d["id"]] = d
            return httpx.Response(200, text="\n".join('{"success": true}' for _ in lines))
        if "/documents/" in path and m == "DELETE":
            return httpx.Response(200 if self.docs.pop(path.rsplit("/", 1)[1], None) else 404, json={})
        if path.endswith("/documents/search"):
            q = request.url.params["q"].lower()
            hits = [{"document": d, "highlights": [], "text_match": 1}
                    for d in self.docs.values() if q == "*" or q in json.dumps(d).lower()]
            return httpx.Response(200, json={"found": len(hits), "hits": hits})
        return httpx.Response(404, json={})


def make(store):
    fake = FakeTypesense()
    index = TypesenseIndex("http://ts:8108", "k", client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    return fake, index


def test_sync_mirrors_writes_and_deletes(store):
    fake, index = make(store)
    index.ensure_collection()
    a = store.add_candidate(name="Ana", about="react")
    b = store.add_candidate(name="Bo", about="python")
    store.update_candidate(a["id"], about="react native")
    assert index.sync(store) == 2
    assert fake.docs[str(a["id"])]["about"] == "react native"
    store.delete_candidate(b["id"])
    index.sync(store)
    assert set(fake.docs) == {str(a["id"])}
    assert store.outbox_batch() == []


def test_search_uses_typesense_and_falls_back_when_down(store):
    fake, index = make(store)
    index.ensure_collection()
    store.add_candidate(name="Ana", about="react")
    svc = SearchService(store, index)
    r = svc.search("react")
    assert r.engine == "typesense" and r.hits[0].candidate["name"] == "Ana"
    fake.up = False
    r = svc.search("react")
    assert r.engine == "fts5" and r.total == 1
    assert svc.engine == "built-in"  # backs off instead of retrying every keystroke


def test_rebuild_reindexes_everything(store):
    fake, index = make(store)
    for n in range(3):
        store.add_candidate(name=f"P{n}")
    assert index.rebuild(store) == 3 and len(fake.docs) == 3
    assert store.outbox_batch() == []


def test_without_typesense_outbox_is_pruned(store):
    store.add_candidate(name="Z")
    SearchService(store, None)
    assert store.outbox_batch() == []

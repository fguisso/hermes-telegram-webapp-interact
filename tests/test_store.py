import threading
import time

import pytest

from interact.store import Page, PageStore, new_page_id


def _page(**kw):
    now = time.time()
    return Page(id=kw.pop("id", new_page_id()), title="T", backend="fake", created_at=now, expires_at=now + 60, **kw)


def test_put_get_update_delete(tmp_path):
    store = PageStore(tmp_path / "pages.json")
    page = _page(initial_state={"a": 1})
    store.put(page)
    assert store.get(page.id).initial_state == {"a": 1}

    updated = store.update(page.id, lambda p: p.state.update(b=2))
    assert updated.state == {"b": 2}
    assert PageStore(tmp_path / "pages.json").get(page.id).state == {"b": 2}  # persisted

    assert store.update("missing", lambda p: None) is None
    store.delete(page.id)
    assert store.get(page.id) is None


def test_failed_mutation_is_not_persisted(tmp_path):
    store = PageStore(tmp_path / "pages.json")
    page = _page()
    store.put(page)

    def boom(p):
        p.status = "submitted"
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        store.update(page.id, boom)
    assert store.get(page.id).status == "active"


def test_concurrent_updates_do_not_lose_writes(tmp_path):
    store = PageStore(tmp_path / "pages.json")
    page = _page(state={"n": 0})
    store.put(page)

    def bump():
        for _ in range(20):
            store.update(page.id, lambda p: p.state.update(n=p.state["n"] + 1))

    threads = [threading.Thread(target=bump) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.get(page.id).state["n"] == 100


def test_page_ids_are_url_safe_and_unique():
    ids = {new_page_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(len(i) == 12 and i.replace("-", "").replace("_", "").isalnum() for i in ids)

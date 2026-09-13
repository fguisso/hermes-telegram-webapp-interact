import json

import pytest

from interact.service import InteractService
from interact.settings import Settings

SESSION = {
    "platform": "telegram",
    "chat_id": "42",
    "thread_id": "",
    "user_id": "42",
    "session_key": "agent:main:telegram:dm:42",
}


def payload(page_id, state, action="approve", extra=None):
    body = {"v": 1, "p": page_id, "a": action, "s": state}
    if extra is not None:
        body["x"] = extra
    return json.dumps(body)


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.deploys = []
        self.removed = []

    def deploy(self, page_id, html, *, title, remote_id=None):
        remote = remote_id or f"r-{page_id}"
        self.deploys.append({"page_id": page_id, "html": html, "title": title, "remote_id": remote_id})
        return f"https://pages.example.com/p/{remote}/", remote

    def remove(self, page_id, remote_id, *, tombstone_html, title, purge=False):
        self.removed.append({"page_id": page_id, "remote_id": remote_id, "purge": purge, "html": tombstone_html})
        return {"tombstoned": True}


class FakeBridge:
    available = True

    def __init__(self):
        self.sent = []

    def send_page_button(self, chat_id, thread_id, text, button_text, url):
        self.sent.append({"chat_id": chat_id, "text": text, "button_text": button_text, "url": url})
        return "777"


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


@pytest.fixture
def injected():
    return []


@pytest.fixture
def service(settings, injected):
    def injector(content, session_key):
        injected.append((content, session_key))
        return True

    return InteractService(settings, backend=FakeBackend(), bridge=FakeBridge(), injector=injector)

"""Registry of interactive pages: which conversation asked for each page, where it was deployed and
what came back. One JSON file guarded by a thread lock plus ``flock`` (gateway and CLI processes may
share the same ``HERMES_HOME``)."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

ACTIVE, SUBMITTED, CLOSED, EXPIRED = "active", "submitted", "closed", "expired"


@dataclass
class Page:
    id: str
    title: str
    backend: str
    created_at: float
    expires_at: float
    status: str = ACTIVE
    url: str = ""
    remote_id: str = ""  # backend handle: pipa uuid, command-backend id, ...
    session_key: str = ""
    platform: str = ""
    chat_id: str = ""
    thread_id: str = ""
    user_id: str = ""
    initial_state: dict = field(default_factory=dict)
    main_button: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)  # last submitted state
    submissions: list = field(default_factory=list)
    updated_at: float = 0.0

    def is_open(self, now: Optional[float] = None) -> bool:
        return self.status == ACTIVE and (time.time() if now is None else now) < self.expires_at

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Page":
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


def new_page_id() -> str:
    return secrets.token_urlsafe(9)  # 12 url-safe chars, 72 bits


class PageStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_suffix(".lock"), "a+") as handle:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self) -> dict[str, Page]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        return {page_id: Page.from_dict(data) for page_id, data in (raw.get("pages") or {}).items()}

    def _write(self, pages: dict[str, Page]) -> None:
        payload = json.dumps(
            {"version": 2, "pages": {page_id: page.to_dict() for page_id, page in pages.items()}},
            ensure_ascii=False,
            indent=1,
        )
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".pages-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self, page_id: str) -> Optional[Page]:
        with self._locked():
            return self._read().get(page_id)

    def all(self) -> list[Page]:
        with self._locked():
            return sorted(self._read().values(), key=lambda page: page.created_at)

    def put(self, page: Page) -> None:
        with self._locked():
            pages = self._read()
            pages[page.id] = page
            self._write(pages)

    def update(self, page_id: str, mutate: Callable[[Page], None]) -> Optional[Page]:
        """Apply ``mutate`` under the lock; an exception from it aborts the write."""
        with self._locked():
            pages = self._read()
            page = pages.get(page_id)
            if page is None:
                return None
            mutate(page)
            page.updated_at = time.time()
            self._write(pages)
            return page

    def delete(self, page_id: str) -> None:
        with self._locked():
            pages = self._read()
            if pages.pop(page_id, None) is not None:
                self._write(pages)

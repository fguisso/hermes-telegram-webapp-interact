"""Core of PluginInteract: page lifecycle and the hand-off back to the agent.

Pages are static. The Mini App keeps its state in localStorage and returns the final answer with
``Telegram.WebApp.sendData()``, which reaches the bot as a ``web_app_data`` message (bridge.py). The
only thing kept here is a small registry mapping page ids to the conversation that asked for them.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .backends import Backend, DeployError, make_backend
from .bridge import TelegramBridge
from .render import render_page, render_tombstone
from .settings import Settings
from .store import ACTIVE, CLOSED, EXPIRED, SUBMITTED, Page, PageStore, new_page_id

logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 4096  # Telegram's sendData limit
MAX_INITIAL_STATE_BYTES = 3500  # leaves room for the envelope and the user's edits
MAX_HTML_BYTES = 1_500_000
MAX_INJECT_STATE_CHARS = 12_000
RETENTION_SECONDS = 7 * 86400
_ACTION_RE = re.compile(r"^[\w.:-]{1,64}$")

MESSAGES = {
    "saved": "Salvo neste aparelho",
    "error": "Não foi possível salvar neste aparelho",
    "submitted": "Enviado!",
    "alreadySent": "Você já enviou esta página.",
    "expired": "Esta página expirou.",
    "tooLarge": "Resposta grande demais para o Telegram ({n} bytes, máx. 4096).",
    "notKeyboard": "Abra esta página pelo botão do teclado do bot para enviar.",
    "preview": "Fora do Telegram — isto seria enviado ao bot:",
    "close": "Fechar",
}

Injector = Callable[[str, Optional[str]], bool]


class InteractError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _check_state(state: Any) -> dict:
    if state is None:
        return {}
    if not isinstance(state, dict):
        raise InteractError(400, "invalid_state", "initial_state must be a JSON object")
    if _json_size(state) > MAX_INITIAL_STATE_BYTES:
        raise InteractError(
            413, "state_too_large",
            f"initial_state exceeds {MAX_INITIAL_STATE_BYTES} bytes; the answer travels in one Telegram message "
            f"(max {MAX_PAYLOAD_BYTES} bytes). Keep only ids and editable values in the state and render "
            "read-only data straight into the HTML.",
        )
    return state


def _check_html(html: Any) -> str:
    if not isinstance(html, str) or not html.strip():
        raise InteractError(400, "invalid_html", "html is required")
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise InteractError(413, "html_too_large", f"html exceeds {MAX_HTML_BYTES} bytes")
    return html


def _check_main_button(value: Any) -> dict:
    if not value:
        return {}
    if not isinstance(value, dict) or not str(value.get("text") or "").strip():
        raise InteractError(400, "invalid_main_button", "main_button needs {text, action}")
    return {"text": str(value["text"])[:64], "action": _clean_action(value.get("action"))}


def _clean_action(action: Any) -> str:
    text = str(action or "submit").strip()
    return text if _ACTION_RE.match(text) else "submit"


def _same_user(telegram_id: str, stored: str) -> bool:
    stored = str(stored or "")
    return bool(stored) and (stored == telegram_id or stored.rsplit(":", 1)[-1] == telegram_id)


def format_submission(page: Page, record: dict) -> str:
    """The message injected into the agent's session when the user submits."""
    who = (record.get("user") or {}).get("name") or "The user"
    state_json = json.dumps(record["state"], ensure_ascii=False, indent=2)
    if len(state_json) > MAX_INJECT_STATE_CHARS:
        state_json = state_json[:MAX_INJECT_STATE_CHARS] + "\n… (truncated — call interact_get_state for the full state)"
    lines = [
        f'[PluginInteract] {who} submitted the interactive page "{page.title}" (page_id: {page.id}).',
        f"action: {record['action']}",
    ]
    if record.get("extra") is not None:
        lines.append("extra: " + json.dumps(record["extra"], ensure_ascii=False))
    lines += ["final state:", "```json", state_json, "```"]
    return "\n".join(lines)


class InteractService:
    def __init__(
        self,
        settings: Settings,
        *,
        backend: Optional[Backend] = None,
        bridge: Optional[TelegramBridge] = None,
        injector: Optional[Injector] = None,
        store: Optional[PageStore] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self._backend = backend
        self._store = store
        self.bridge = bridge or TelegramBridge()
        self.injector = injector
        self.clock = clock

    @property
    def store(self) -> PageStore:
        if self._store is None:
            self._store = PageStore(self.settings.data_dir / "pages.json")
        return self._store

    @property
    def backend(self) -> Backend:
        if self._backend is None:
            self._backend = make_backend(self.settings)
        return self._backend

    def _require(self, page_id: str) -> Page:
        page = self.store.get(page_id)
        if page is None:
            raise InteractError(404, "not_found", f"no page {page_id}")
        return page

    def _status(self, page: Page) -> str:
        return EXPIRED if page.status == ACTIVE and not page.is_open(self.clock()) else page.status

    def _ttl_minutes(self, requested: Any) -> int:
        try:
            minutes = int(requested) if requested is not None else self.settings.default_ttl_minutes
        except (TypeError, ValueError):
            minutes = self.settings.default_ttl_minutes
        return max(5, min(minutes, self.settings.max_ttl_minutes))

    def _render(self, page: Page, html: str) -> str:
        config = {
            "pageId": page.id,
            "title": page.title,
            "initialState": page.initial_state,
            "expiresAt": int(page.expires_at * 1000),
            "mainButton": page.main_button or None,
            "locale": self.settings.locale,
            "currency": self.settings.currency,
            "messages": MESSAGES,
        }
        return render_page(html, config=config, title=page.title)

    def _deploy(self, page: Page, html: str) -> tuple[str, str]:
        try:
            return self.backend.deploy(page.id, self._render(page, html), title=page.title,
                                       remote_id=page.remote_id or None)
        except DeployError as exc:
            raise InteractError(502, "deploy_failed", str(exc)) from exc

    # ------------------------------------------------------------------ agent side

    def create_page(
        self,
        *,
        title: str,
        html: str,
        initial_state: Any = None,
        session: Optional[dict] = None,
        message: str = "",
        button_text: str = "",
        main_button: Any = None,
        ttl_minutes: Any = None,
        send: bool = True,
    ) -> dict:
        title = (title or "").strip()[:120]
        if not title:
            raise InteractError(400, "invalid_title", "title is required")
        html = _check_html(html)
        session = session or {}
        now = self.clock()
        page = Page(
            id=new_page_id(),
            title=title,
            backend=self.backend.name,
            created_at=now,
            expires_at=now + self._ttl_minutes(ttl_minutes) * 60,
            session_key=session.get("session_key", ""),
            platform=session.get("platform", ""),
            chat_id=session.get("chat_id", ""),
            thread_id=session.get("thread_id", ""),
            user_id=session.get("user_id", ""),
            initial_state=_check_state(initial_state),
            main_button=_check_main_button(main_button),
            updated_at=now,
        )
        self.sweep_expired()
        page.url, page.remote_id = self._deploy(page, html)
        self.store.put(page)

        result = {
            "page_id": page.id,
            "title": page.title,
            "url": page.url,
            "status": page.status,
            "backend": page.backend,
            "expires_at": _iso(page.expires_at),
        }
        result.update(self._deliver(page, message, button_text) if send else {"delivered": "none"})
        return result

    def _deliver(self, page: Page, message: str, button_text: str) -> dict:
        if page.platform != "telegram" or not page.chat_id:
            return {"delivered": "url", "note": "Not a Telegram conversation: share the URL. Only a Telegram "
                                                "keyboard button can send the answer back."}
        if not self.bridge.available:
            return {"delivered": "url", "note": "Telegram adapter not wired in this process: share the URL."}
        try:
            self.bridge.send_page_button(page.chat_id, page.thread_id, message.strip() or f"📋 {page.title}",
                                         button_text.strip() or self.settings.button_text, page.url)
        except Exception as exc:
            return {"delivered": "failed", "error": str(exc)}
        return {"delivered": "telegram",
                "note": "The user got a keyboard button; do not paste the URL. Wait for the [PluginInteract] message."}

    def update_page(self, page_id: str, *, html: str, title: str = "", initial_state: Any = None) -> dict:
        page = self._require(page_id)
        if self._status(page) != ACTIVE:
            raise InteractError(410, "page_closed", f"page is {self._status(page)}")
        html = _check_html(html)
        if title.strip():
            page.title = title.strip()[:120]
        if initial_state is not None:
            page.initial_state = _check_state(initial_state)
        url, remote_id = self._deploy(page, html)

        def apply(p: Page) -> None:
            p.title, p.url, p.remote_id, p.initial_state = page.title, url, remote_id, page.initial_state

        self.store.update(page_id, apply)
        return {"page_id": page_id, "url": url, "status": ACTIVE, "updated": True,
                "note": "Same URL. Edits the user already made stay on their device (localStorage)."}

    def close_page(self, page_id: str, *, purge: bool = False, reason: str = CLOSED) -> dict:
        page = self._require(page_id)
        info: dict = {}
        try:
            info = self.backend.remove(page.id, page.remote_id, tombstone_html=render_tombstone(page.title, reason),
                                       title=page.title, purge=purge)
        except DeployError as exc:
            info["warning"] = f"backend removal failed: {exc}"

        def apply(p: Page) -> None:
            if p.status == ACTIVE:
                p.status = reason

        page = self.store.update(page_id, apply) or page
        return {"page_id": page_id, "status": page.status, **info}

    def sweep_expired(self) -> int:
        """Expire overdue pages and forget old finished ones; returns how many pages expired."""
        now = self.clock()
        expired = 0
        for page in self.store.all():
            if page.status == ACTIVE and page.expires_at <= now:
                try:
                    self.close_page(page.id, reason=EXPIRED)
                    expired += 1
                except Exception:
                    logger.warning("PluginInteract: expiring %s failed", page.id, exc_info=True)
            elif page.status != ACTIVE and max(page.updated_at, page.expires_at) + RETENTION_SECONDS < now:
                self.store.delete(page.id)
        return expired

    def get_state(self, page_id: str) -> dict:
        page = self._require(page_id)
        last = page.submissions[-1] if page.submissions else None
        return {
            "page_id": page.id,
            "title": page.title,
            "status": self._status(page),
            "url": page.url,
            "expires_at": _iso(page.expires_at),
            "submitted": None if last is None else {
                "at": _iso(last["at"]), "action": last["action"], "user": last.get("user"),
                "extra": last.get("extra"), "state": last["state"],
            },
            "note": "Unsubmitted edits live only on the user's device.",
        }

    def list_pages(self, *, session_key: Optional[str] = None, include_closed: bool = False) -> list[dict]:
        pages = []
        for page in self.store.all():
            if session_key and page.session_key != session_key:
                continue
            status = self._status(page)
            if not include_closed and status != ACTIVE:
                continue
            pages.append({"page_id": page.id, "title": page.title, "status": status, "url": page.url,
                          "expires_at": _iso(page.expires_at)})
        return pages

    # ------------------------------------------------------------------ Telegram side

    def _user_allowed(self, page: Page, user_id: str) -> bool:
        if page.user_id:
            return _same_user(user_id, page.user_id) or user_id in self.settings.allowed_users
        return not self.settings.allowed_users or user_id in self.settings.allowed_users

    def handle_web_app_data(self, *, data: str, user_id: str, chat_id: str = "", user_name: str = "") -> str:
        """Handle a ``web_app_data`` message (the page's sendData). Returns the reply for the chat.

        Telegram delivers these only from the Mini App the user opened through our keyboard button,
        with the real sender attached, so the sender check below is the authorization."""
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict) or payload.get("v") != 1 or not isinstance(payload.get("p"), str) \
                or not isinstance(payload.get("s"), dict):
            logger.warning("PluginInteract: ignoring malformed web_app_data from %s", user_id)
            return "⚠️ Não entendi os dados enviados pela página."

        page = self.store.get(payload["p"])
        if page is None:
            return "⚠️ Não encontrei esta página (talvez já tenha sido removida)."
        if not self._user_allowed(page, user_id):
            logger.warning("PluginInteract: user %s tried to submit page %s of %s", user_id, page.id, page.user_id)
            return "⚠️ Esta página foi criada para outra pessoa."

        now = self.clock()
        record = {"at": now, "action": _clean_action(payload.get("a")), "state": payload["s"],
                  "extra": payload.get("x"), "user": {"id": user_id, "name": user_name or None}}

        def apply(p: Page) -> None:
            if p.status != ACTIVE:
                raise InteractError(410, f"page_{p.status}")
            if now >= p.expires_at:
                raise InteractError(410, "page_expired")
            p.status, p.state = SUBMITTED, record["state"]
            p.submissions.append(record)

        try:
            page = self.store.update(page.id, apply) or page
        except InteractError as exc:
            return {
                "page_submitted": "Você já enviou esta página.",
                "page_expired": "⌛ Esta página expirou.",
            }.get(exc.code, "🔒 Esta página foi encerrada.")

        if self._notify_agent(page, record):
            return f"✅ Recebido: “{page.title}”."
        return (f"Recebi “{page.title}”, mas não consegui avisar o agente automaticamente. "
                f"Peça: “veja o resultado da página {page.id}”.")

    def _notify_agent(self, page: Page, record: dict) -> bool:
        if self.injector is None:
            return False
        try:
            return bool(self.injector(format_submission(page, record), page.session_key or None))
        except Exception:
            logger.warning("PluginInteract: injecting the submission of %s failed", page.id, exc_info=True)
            return False

"""Tool schemas and handlers exposed to the agent (toolset ``interact``)."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .bridge import current_session
from .service import InteractError, InteractService

logger = logging.getLogger(__name__)

TOOLSET = "interact"

CREATE_DESCRIPTION = """\
PluginInteract: build a small static interactive page and send it to the user as a Telegram Mini App (keyboard button).
Use it when the user wants to see, review, edit, compare, choose or approve something visually, or mentions
"PluginInteract"/"Interact" (e.g. "review this price table", "let me pick the options", "show me a form").
The page is deployed for you. Edits stay on the user's device (localStorage) until they tap a submit button; then
the page sends {action, state} through Telegram and you receive a message starting with "[PluginInteract]" with the
final state as JSON — continue the conversation from it. When delivered == "telegram" do NOT paste the URL.

Limit: the answer travels in ONE Telegram message (max 4096 bytes of JSON). Keep initial_state to what the user can
change (ids + editable values, short keys); render read-only data (names, descriptions) straight into the HTML.

HTML: a self-contained, mobile-first fragment (or full document) in the user's language. Inline <style>/<script>
only (jsdelivr/cdnjs allowed); no network calls; no <form action>. The Telegram theme is applied automatically.
Ready classes: ix-stack, ix-stack-sm, ix-row, ix-between, ix-grid, ix-card, ix-muted, ix-num, ix-badge, ix-chip,
ix-field, ix-btn (+ ix-secondary | ix-danger | ix-ghost | ix-block), ix-table inside div.ix-table-wrap.

State (window.Interact is preloaded):
- Bind elements to initial_state with dot paths ("rows.0.price").
- data-bind="path" on input/select/textarea keeps state in sync (number -> Number, checkbox -> bool,
  several checkboxes sharing a path -> array of values). On other elements it displays the value;
  data-format="currency|number|percent|json".
- data-action="approve" on a button sends the state with that action and closes the Mini App; data-confirm="…"
  asks first; data-extra='{"row":2}' adds context. Use distinct actions for distinct outcomes.
- data-set="path" data-value='"x"' sets a value on click (chips; aria-pressed is managed);
  data-toggle="path" flips a boolean; data-show-if="path" | "path=value" | "!path" shows conditionally.
- <form data-interact data-action="send"> collects named fields into state on submit.
- JS: Interact.get(path), Interact.set(path, v), Interact.update({...}), Interact.onChange(fn),
  Interact.submit(action, extra). Elements added later by your script are bound automatically.
- <p data-interact-status></p> shows the local save status."""

SCHEMAS: dict[str, dict] = {
    "interact_create": {
        "name": "interact_create",
        "description": CREATE_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short page title (shown in the chat and the tab)."},
                "html": {"type": "string", "description": "Page body (or full document) following the rules above."},
                "initial_state": {"type": "object", "description": "Editable data the page starts with (compact, < 3.5 KB)."},
                "message": {"type": "string", "description": "Text sent with the button. Defaults to the title."},
                "button_text": {"type": "string", "description": "Label of the keyboard button (default: Abrir)."},
                "main_button": {
                    "type": "object",
                    "description": "Optional Telegram bottom button that submits: {\"text\": \"Enviar\", \"action\": \"submit\"}.",
                    "properties": {"text": {"type": "string"}, "action": {"type": "string"}},
                },
                "ttl_minutes": {"type": "integer", "description": "How long the page accepts answers (default from settings)."},
            },
            "required": ["title", "html"],
        },
    },
    "interact_update": {
        "name": "interact_update",
        "description": "Redeploy an open PluginInteract page with new HTML (and optionally title/initial_state). The URL "
                       "stays the same; edits the user already made stay on their device.",
        "parameters": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Page id returned by interact_create."},
                "html": {"type": "string", "description": "New page HTML."},
                "title": {"type": "string", "description": "New title (optional)."},
                "initial_state": {"type": "object", "description": "New initial state (optional)."},
            },
            "required": ["page_id", "html"],
        },
    },
    "interact_get_state": {
        "name": "interact_get_state",
        "description": "Status of a PluginInteract page and the last submitted answer (unsubmitted edits live only on "
                       "the user's device).",
        "parameters": {
            "type": "object",
            "properties": {"page_id": {"type": "string", "description": "Page id."}},
            "required": ["page_id"],
        },
    },
    "interact_close": {
        "name": "interact_close",
        "description": "Close a PluginInteract page: its URL is replaced by a 'closed' notice and answers are refused. "
                       "purge=true also asks the host to delete it; on pipa that needs a human to confirm the returned "
                       "purge_verify_url in a browser.",
        "parameters": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Page id."},
                "purge": {"type": "boolean", "description": "Hard-delete on the host (pipa: needs human step-up)."},
            },
            "required": ["page_id"],
        },
    },
    "interact_list": {
        "name": "interact_list",
        "description": "List PluginInteract pages of this conversation (open ones unless include_closed).",
        "parameters": {
            "type": "object",
            "properties": {"include_closed": {"type": "boolean", "description": "Also list submitted/closed/expired pages."}},
        },
    },
}


def _ok(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _fail(exc: Exception) -> str:
    if isinstance(exc, InteractError):
        return _ok({"error": exc.code, "message": exc.message})
    logger.exception("PluginInteract tool failed")
    return _ok({"error": "internal_error", "message": str(exc)})


def _guard(fn: Callable[[dict], Any]) -> Callable[..., str]:
    def handler(args: dict, **_: Any) -> str:
        try:
            return _ok(fn(args or {}))
        except Exception as exc:
            return _fail(exc)

    handler.__name__ = fn.__name__
    return handler


def make_handlers(service: InteractService) -> dict[str, Callable[..., str]]:
    def interact_create(args: dict) -> dict:
        return service.create_page(
            title=args.get("title", ""),
            html=args.get("html", ""),
            initial_state=args.get("initial_state"),
            session=current_session(),
            message=args.get("message") or "",
            button_text=args.get("button_text") or "",
            main_button=args.get("main_button"),
            ttl_minutes=args.get("ttl_minutes"),
        )

    def interact_update(args: dict) -> dict:
        return service.update_page(args.get("page_id", ""), html=args.get("html", ""),
                                   title=args.get("title") or "", initial_state=args.get("initial_state"))

    def interact_get_state(args: dict) -> dict:
        return service.get_state(args.get("page_id", ""))

    def interact_close(args: dict) -> dict:
        return service.close_page(args.get("page_id", ""), purge=bool(args.get("purge")))

    def interact_list(args: dict) -> dict:
        session_key = current_session().get("session_key") or None
        return {"pages": service.list_pages(session_key=session_key, include_closed=bool(args.get("include_closed")))}

    return {fn.__name__: _guard(fn) for fn in (interact_create, interact_update, interact_get_state, interact_close, interact_list)}


def register_tools(ctx: Any, service: InteractService) -> None:
    handlers = make_handlers(service)
    for name, schema in SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handlers[name],
            description=schema["description"].splitlines()[0],
            emoji="🪁",
        )

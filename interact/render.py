"""Turn agent-authored HTML into a self-contained static Mini App page.

The page gets, in order: charset/viewport, a Content-Security-Policy, Telegram's WebApp script, the
theme stylesheet, the page config and the SDK. Nothing is fetched at runtime besides those scripts.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

ASSETS = Path(__file__).with_name("assets")
TELEGRAM_JS = "https://telegram.org/js/telegram-web-app.js"
CDNS = "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com"
CSP = "; ".join(
    [
        "default-src 'self'",
        f"script-src 'self' 'unsafe-inline' https://telegram.org {CDNS}",
        f"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com {CDNS}",
        "font-src 'self' data: https://fonts.gstatic.com",
        "img-src 'self' data: blob: https:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'none'",
    ]
)

_HEAD_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_HTML_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title\b", re.IGNORECASE)

TOMBSTONE_LABELS = {
    "submitted": "Respostas enviadas",
    "expired": "Página expirada",
    "closed": "Página encerrada",
}


@lru_cache(maxsize=None)
def asset(name: str) -> str:
    return (ASSETS / name).read_text("utf-8")


def script_json(value: Any) -> str:
    """JSON that is safe inside ``<script>`` (no ``</script>`` break-out, no JS line separators)."""
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def render_page(body_html: str, *, config: dict, title: str) -> str:
    head = "\n".join(
        [
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
            f'<meta http-equiv="Content-Security-Policy" content="{html_lib.escape(CSP, quote=True)}">',
            '<meta name="referrer" content="no-referrer">',
            f'<script src="{TELEGRAM_JS}"></script>',
            f"<style>{asset('interact.css')}</style>",
            f"<script>window.__INTERACT__ = {script_json(config)};</script>",
            f"<script>{asset('interact.js')}</script>",
        ]
    )
    if not _TITLE_RE.search(body_html):
        head += f"\n<title>{html_lib.escape(title)}</title>"

    if match := _HEAD_RE.search(body_html):
        return f"{body_html[:match.end()]}\n{head}\n{body_html[match.end():]}"
    if match := _HTML_RE.search(body_html):
        return f"{body_html[:match.end()]}\n<head>\n{head}\n</head>\n{body_html[match.end():]}"
    return (
        '<!doctype html>\n<html lang="pt-BR">\n<head>\n'
        f"{head}\n</head>\n<body>\n<main class=\"ix-page\">\n{body_html}\n</main>\n</body>\n</html>\n"
    )


def render_tombstone(title: str, status: str) -> str:
    label = TOMBSTONE_LABELS.get(status, TOMBSTONE_LABELS["closed"])
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{html_lib.escape(title)}</title>
<script src="{TELEGRAM_JS}"></script>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f4f5f7; color: #15181d; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #17191d; color: #eceef2; }} .muted {{ color: #9aa0aa; }} }}
  main {{ text-align: center; padding: 24px; max-width: 420px; }}
  .icon {{ font-size: 44px; }}
  h1 {{ font-size: 1.3rem; margin: 12px 0 4px; }}
  p {{ margin: 4px 0; }}
  .muted {{ color: #6b7280; font-size: .92rem; }}
</style>
</head>
<body>
<main>
  <div class="icon" aria-hidden="true">🪁</div>
  <h1>{html_lib.escape(label)}</h1>
  <p>{html_lib.escape(title)}</p>
  <p class="muted">Volte para a conversa com o agente.</p>
</main>
<script>window.Telegram && Telegram.WebApp && Telegram.WebApp.ready();</script>
</body>
</html>
"""

"""``hermes interact …`` / ``python -m interact …`` subcommands and the ``/interact`` slash command."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from typing import Any, Callable

from .bridge import current_session
from .render import asset
from .service import InteractError, InteractService

DEMO_TITLE = "Revisão da tabela de preços"
DEMO_STATE = {
    "rows": [
        {"product": "Plano Básico", "current": 49.9, "price": 54.9, "approved": False},
        {"product": "Plano Pro", "current": 99.9, "price": 109.9, "approved": False},
        {"product": "Plano Equipe", "current": 249.0, "price": 269.0, "approved": False},
        {"product": "Add-on Suporte", "current": 29.0, "price": 29.0, "approved": True},
    ],
    "effective": "next_month",
    "notes": "",
}


def setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="interact_command")
    sub.add_parser("demo", help="Deploy the demo price-review page to the configured backend")
    sub.add_parser("list", help="List pages (all, including closed)")
    state = sub.add_parser("state", help="Show a page's status and last answer")
    state.add_argument("page_id")
    close = sub.add_parser("close", help="Close a page")
    close.add_argument("page_id")
    close.add_argument("--purge", action="store_true", help="Also delete it on the host (pipa: human step-up)")
    sub.add_parser("doctor", help="Check the deploy backend")


def handle(service: InteractService, args: argparse.Namespace) -> int:
    command = getattr(args, "interact_command", None) or "doctor"
    try:
        if command == "demo":
            result = service.create_page(title=DEMO_TITLE, html=asset("demo.html"), initial_state=DEMO_STATE,
                                         session={}, send=False)
            _print(result)
            print(f"\nOpen: {result['url']}\n(outside Telegram the submit buttons show the payload that would be sent)")
        elif command == "list":
            _print(service.list_pages(include_closed=True))
        elif command == "state":
            _print(service.get_state(args.page_id))
        elif command == "close":
            _print(service.close_page(args.page_id, purge=args.purge))
        elif command == "doctor":
            return _doctor(service)
    except InteractError as exc:
        print(f"error: {exc.code}: {exc.message}")
        return 1
    return 0


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _doctor(service: InteractService) -> int:
    s = service.settings
    checks: list[tuple[str, bool, str]] = [("backend", True, s.backend), ("data_dir", True, str(s.data_dir))]
    if s.backend == "pipa":
        pipa_bin = str(s.pipa.get("bin") or "pipa")
        found = shutil.which(pipa_bin)
        checks.append(("pipa CLI", bool(found), found or f"{pipa_bin} not on PATH"))
        if found:
            proc = subprocess.run([pipa_bin, "--json", "server"], capture_output=True, text=True, timeout=30)
            checks.append(("pipa server", proc.returncode == 0, (proc.stdout or proc.stderr).strip()[:300]))
    else:
        checks.append(("command.deploy", bool(s.command.get("deploy")), s.command.get("deploy") or "missing"))
    ok = True
    for name, passed, detail in checks:
        ok &= passed
        print(f"{'✓' if passed else '✗'} {name}: {detail}")
    return 0 if ok else 1


def make_slash(service: InteractService) -> Callable[[str], str]:
    def interact_command(raw_args: str) -> str:
        parts = (raw_args or "").split()
        if parts[:1] == ["close"] and len(parts) > 1:
            try:
                service.close_page(parts[1])
            except InteractError as exc:
                return f"Não consegui encerrar {parts[1]}: {exc.message}"
            return f"Página {parts[1]} encerrada."
        pages = service.list_pages(session_key=current_session().get("session_key") or None)
        if not pages:
            return "Nenhuma página PluginInteract aberta nesta conversa."
        return "\n".join(f"• {p['title']} — {p['page_id']} (expira {p['expires_at']})" for p in pages)

    return interact_command

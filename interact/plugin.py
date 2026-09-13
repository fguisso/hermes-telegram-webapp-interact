"""Hermes entry point: ``register(ctx)`` wires the tools, the Telegram hook, commands and the prompt."""

from __future__ import annotations

import logging
from functools import partial
from typing import Any, Optional

from . import cli, tools
from .service import InteractService
from .settings import Settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "PluginInteract (tools interact_*): when the user wants to see, review, edit, compare, choose or approve "
    "something visually — or mentions PluginInteract/Interact — build a focused, mobile-first page with "
    "interact_create instead of replying with a long text table. It opens inside Telegram as a Mini App; the "
    "user's answer arrives later as a message starting with \"[PluginInteract]\" carrying the final state as JSON."
)


def register(ctx: Any) -> None:
    settings = Settings.load(getattr(ctx, "get_config", None))
    service = InteractService(settings, injector=partial(_inject, ctx))

    tools.register_tools(ctx, service)
    if hasattr(ctx, "register_platform_handler"):
        ctx.register_platform_handler("telegram", partial(_wire_telegram, service))
    else:  # Hermes without native platform handlers: pages deploy, but answers can't come back
        logger.warning("PluginInteract: this Hermes has no ctx.register_platform_handler; "
                       "update Hermes so Telegram answers (web_app_data) reach the agent")
    ctx.register_command("interact", handler=cli.make_slash(service),
                         description="List or close PluginInteract pages", args_hint="[close <page_id>]")
    ctx.register_cli_command("interact", help="PluginInteract pages: demo, list, state, close, doctor",
                             setup_fn=cli.setup, handler_fn=partial(cli.handle, service))
    register_section = getattr(ctx, "register_system_prompt_section", None)  # absent on older Hermes
    if register_section is not None:
        register_section("interact.usage", SYSTEM_PROMPT)


def _inject(ctx: Any, content: str, session_key: Optional[str]) -> bool:
    return bool(ctx.inject_message(content, role="user", session_key=session_key or None))


def _wire_telegram(service: InteractService, native: Any, adapter: Any) -> None:
    service.bridge.wire(native, on_web_app_data=service.handle_web_app_data)

"""Plugin settings.

Read from ``plugins.entries.<plugin-id>.settings`` in Hermes' ``config.yaml`` (through
``ctx.get_config``). Every key can be overridden by an ``INTERACT_*`` env var, which is also how the
standalone runner (``python -m interact``) is configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

BACKENDS = ("pipa", "command")


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [str(item).strip() for item in value if str(item).strip()]


@dataclass
class Settings:
    backend: str = "pipa"
    default_ttl_minutes: int = 1440
    max_ttl_minutes: int = 10080
    button_text: str = "Abrir"
    locale: str = "pt-BR"
    currency: str = "BRL"
    pipa: dict = field(default_factory=dict)
    command: dict = field(default_factory=dict)
    data_dir: Path = field(default_factory=lambda: hermes_home() / "interact")
    # Telegram users (besides the one who asked) whose answers are accepted.
    allowed_users: set[str] = field(default_factory=set)

    @classmethod
    def load(
        cls,
        get_config: Optional[Callable[..., Any]] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> "Settings":
        env = os.environ if env is None else env

        def cfg(key: str) -> Any:
            if get_config is None:
                return None
            try:
                return get_config(key, None)
            except Exception:
                return None

        def pick(key: str, env_key: str, default: Any) -> Any:
            if env.get(env_key) not in (None, ""):
                return env[env_key]
            value = cfg(key)
            return default if value is None else value

        pipa = dict(cfg("pipa") or {})
        for key, env_key in (
            ("bin", "INTERACT_PIPA_BIN"),
            ("zone", "INTERACT_PIPA_ZONE"),
            ("workspace", "INTERACT_PIPA_WORKSPACE"),
            ("headless", "INTERACT_PIPA_HEADLESS"),
            ("force_zone", "INTERACT_PIPA_FORCE_ZONE"),
            ("purge", "INTERACT_PIPA_PURGE"),
        ):
            if env.get(env_key):
                pipa[key] = env[env_key]

        command = dict(cfg("command") or {})
        for key, env_key in (
            ("deploy", "INTERACT_COMMAND_DEPLOY"),
            ("remove", "INTERACT_COMMAND_REMOVE"),
            ("url_template", "INTERACT_COMMAND_URL"),
        ):
            if env.get(env_key):
                command[key] = env[env_key]

        backend = str(pick("backend", "INTERACT_BACKEND", "pipa")).strip().lower()
        if backend not in BACKENDS:
            raise ValueError(f"unknown interact backend {backend!r} (expected one of: {', '.join(BACKENDS)})")

        data_dir = pick("data_dir", "INTERACT_DATA_DIR", "")
        allowed = set(as_list(env.get("TELEGRAM_ALLOWED_USERS")))
        allowed |= set(as_list(pick("allowed_users", "INTERACT_ALLOWED_USERS", [])))

        return cls(
            backend=backend,
            default_ttl_minutes=as_int(pick("default_ttl_minutes", "INTERACT_TTL_MINUTES", 1440), 1440),
            max_ttl_minutes=as_int(pick("max_ttl_minutes", "INTERACT_MAX_TTL_MINUTES", 10080), 10080),
            button_text=str(pick("button_text", "INTERACT_BUTTON_TEXT", "Abrir")),
            locale=str(pick("locale", "INTERACT_LOCALE", "pt-BR")),
            currency=str(pick("currency", "INTERACT_CURRENCY", "BRL")),
            pipa=pipa,
            command=command,
            data_dir=Path(data_dir).expanduser() if data_dir else hermes_home() / "interact",
            allowed_users=allowed,
        )

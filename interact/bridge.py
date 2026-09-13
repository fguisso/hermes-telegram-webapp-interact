"""Hermes/Telegram glue.

* Captures the gateway's python-telegram-bot ``Application`` (``ctx.register_platform_handler``) to
  send the page as a keyboard ``web_app`` button — the only kind of button whose Mini App may call
  ``Telegram.WebApp.sendData()``.
* Registers a ``web_app_data`` handler: that is how the page's final answer reaches the bot.
* Reads the current session's routing ids for tool calls.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

SESSION_FIELDS = {
    "platform": "HERMES_SESSION_PLATFORM",
    "chat_id": "HERMES_SESSION_CHAT_ID",
    "thread_id": "HERMES_SESSION_THREAD_ID",
    "user_id": "HERMES_SESSION_USER_ID",
    "session_key": "HERMES_SESSION_KEY",
}

# on_data(data=..., user_id=..., chat_id=..., user_name=...) -> reply text for the chat
WebAppDataCallback = Callable[..., str]


def current_session() -> dict[str, str]:
    """Routing ids of the conversation the current tool call belongs to (empty strings in CLI)."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        def get_session_env(name: str, default: str = "") -> str:
            return os.getenv(name, default)
    return {field: str(get_session_env(env, "") or "") for field, env in SESSION_FIELDS.items()}


def _chat(chat_id: str) -> Any:
    text = str(chat_id).strip()
    return int(text) if text.lstrip("-").isdigit() else text


def _is_private(chat_id: str) -> bool:
    text = str(chat_id).strip()
    return text.isdigit() and int(text) > 0  # user ids are positive, groups/channels negative


def make_web_app_data_handler(on_data: WebAppDataCallback) -> Callable[..., Coroutine]:
    async def on_web_app_data(update: Any, context: Any) -> None:
        message = update.effective_message
        payload = getattr(message, "web_app_data", None)
        if payload is None:
            return
        user = update.effective_user
        reply = await asyncio.to_thread(
            on_data,
            data=payload.data,
            user_id=str(user.id) if user else "",
            chat_id=str(message.chat_id),
            user_name=(user.full_name if user else "") or "",
        )
        from telegram import ReplyKeyboardRemove

        await message.reply_text(reply, reply_markup=ReplyKeyboardRemove())

    return on_web_app_data


class TelegramBridge:
    def __init__(self) -> None:
        self._app: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def wire(self, native: Any, on_web_app_data: Optional[WebAppDataCallback] = None) -> None:
        """Platform-handler factory target: runs inside the adapter's ``connect()`` coroutine,
        before the core handlers are added (core never matches ``web_app_data`` messages)."""
        if native is None or not hasattr(native, "bot"):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self._app, self._loop = native, loop
        if on_web_app_data is not None and hasattr(native, "add_handler"):
            from telegram.ext import MessageHandler, filters

            native.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, make_web_app_data_handler(on_web_app_data)))

    @property
    def available(self) -> bool:
        return self._app is not None and self._loop is not None and not self._loop.is_closed()

    def _call(self, coro: Coroutine, timeout: float = 20.0) -> Any:
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            raise RuntimeError("Telegram bridge is not wired")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:  # never block the loop we need
            loop.create_task(coro)
            return None
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    def send_page_button(self, chat_id: str, thread_id: str, text: str, button_text: str, url: str) -> str:
        """Send ``text`` with a keyboard button that opens ``url`` as a Mini App; return the message id."""
        from telegram import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

        if not url.startswith("https://"):
            raise RuntimeError("Telegram only opens https:// Mini Apps — deploy to an https host")
        if not _is_private(chat_id):
            raise RuntimeError("Telegram allows web_app keyboard buttons only in private chats")
        markup = ReplyKeyboardMarkup(
            [[KeyboardButton(button_text, web_app=WebAppInfo(url=url))]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        thread = str(thread_id or "").strip()
        extra = {"message_thread_id": int(thread)} if thread.isdigit() and int(thread) > 1 else {}
        message = self._call(self._app.bot.send_message(chat_id=_chat(chat_id), text=text, reply_markup=markup, **extra))
        return str(getattr(message, "message_id", "") or "")

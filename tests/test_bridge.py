"""TelegramBridge against real python-telegram-bot types, with the bot on another event loop the
way the gateway runs it (tool handlers call in from worker threads)."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

telegram = pytest.importorskip("telegram")
from telegram.ext import filters  # noqa: E402

from interact.bridge import TelegramBridge, make_web_app_data_handler  # noqa: E402


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=1000 + len(self.calls))


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()
        self.handlers = []

    def add_handler(self, handler, group=0):
        self.handlers.append(handler)


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


def _wire(loop, app, on_data=None):
    bridge = TelegramBridge()

    async def connect():  # what the adapter's connect() does when it runs plugin factories
        bridge.wire(app, on_web_app_data=on_data)

    asyncio.run_coroutine_threadsafe(connect(), loop).result(5)
    return bridge


def test_sends_a_one_time_keyboard_web_app_button(loop):
    app = FakeApp()
    bridge = _wire(loop, app)
    assert bridge.available
    assert bridge.send_page_button("42", "", "Revise", "Abrir", "https://pages.example.com/p/x/") == "1001"

    kwargs = app.bot.calls[0]
    markup = kwargs["reply_markup"]
    button = markup.keyboard[0][0]
    assert isinstance(markup, telegram.ReplyKeyboardMarkup) and markup.one_time_keyboard and markup.resize_keyboard
    assert button.text == "Abrir" and button.web_app.url == "https://pages.example.com/p/x/"
    assert kwargs["chat_id"] == 42 and kwargs["text"] == "Revise" and "message_thread_id" not in kwargs


def test_refuses_http_urls_and_group_chats(loop):
    bridge = _wire(loop, FakeApp())
    with pytest.raises(RuntimeError, match="https"):
        bridge.send_page_button("42", "", "t", "Abrir", "http://127.0.0.1/p/x/")
    with pytest.raises(RuntimeError, match="private chats"):
        bridge.send_page_button("-100123", "", "t", "Abrir", "https://p.example/x/")


def test_registers_a_web_app_data_handler(loop):
    app = FakeApp()
    _wire(loop, app, on_data=lambda **kw: "ok")
    (handler,) = app.handlers
    assert handler.filters is filters.StatusUpdate.WEB_APP_DATA


def test_web_app_data_handler_replies_and_removes_the_keyboard(loop):
    received, replies = [], []

    def on_data(**kwargs):
        received.append(kwargs)
        return "✅ Recebido"

    async def reply_text(text, reply_markup=None):
        replies.append((text, reply_markup))

    message = SimpleNamespace(web_app_data=SimpleNamespace(data='{"v":1}'), chat_id=42, reply_text=reply_text)
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=42, full_name="Ana Lima"))
    asyncio.run_coroutine_threadsafe(make_web_app_data_handler(on_data)(update, None), loop).result(5)

    assert received == [{"data": '{"v":1}', "user_id": "42", "chat_id": "42", "user_name": "Ana Lima"}]
    text, markup = replies[0]
    assert text == "✅ Recebido" and isinstance(markup, telegram.ReplyKeyboardRemove)


def test_unwired_bridge_is_unavailable():
    bridge = TelegramBridge()
    bridge.wire(FakeApp())  # no running loop -> cannot reach the bot
    assert not bridge.available

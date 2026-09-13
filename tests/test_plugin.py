"""Load the plugin through ``register(ctx)`` with a stand-in for Hermes' PluginContext."""

import argparse
import json
import sys

import pytest

from interact import plugin
from interact.tools import SCHEMAS


class FakeCtx:
    def __init__(self, config=None):
        self.config = config or {}
        self.tools, self.platform_handlers, self.commands, self.cli, self.sections = {}, {}, {}, {}, {}
        self.injected = []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kwargs}

    def register_platform_handler(self, platform, factory):
        self.platform_handlers[platform] = factory

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)

    def register_system_prompt_section(self, id, content, **kwargs):
        self.sections[id] = content

    def inject_message(self, content, role="user", *, session_key=None):
        self.injected.append((content, session_key))
        return True


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    for name in ("INTERACT_BACKEND", "HERMES_SESSION_PLATFORM", "HERMES_SESSION_KEY"):
        monkeypatch.delenv(name, raising=False)
    deploy = f"{sys.executable} -c \"print('https://static.example/{{page_id}}/')\""
    ctx = FakeCtx({"data_dir": str(tmp_path), "backend": "command", "command": {"deploy": deploy}})
    plugin.register(ctx)
    return ctx


def test_register_wires_everything(ctx):
    assert set(ctx.tools) == set(SCHEMAS)
    for name, entry in ctx.tools.items():
        assert entry["toolset"] == "interact" and entry["schema"]["name"] == name
        assert entry["schema"]["parameters"]["type"] == "object"
    assert "telegram" in ctx.platform_handlers
    assert "interact" in ctx.commands and "interact" in ctx.cli
    assert "interact.usage" in ctx.sections


def test_tools_end_to_end_outside_telegram(ctx, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cli")
    result = json.loads(ctx.tools["interact_create"]["handler"](
        {"title": "Demo", "html": "<p>x</p>", "initial_state": {"a": 1}}))
    assert result["delivered"] == "url" and result["url"] == f"https://static.example/{result['page_id']}/"

    state = json.loads(ctx.tools["interact_get_state"]["handler"]({"page_id": result["page_id"]}))
    assert state["status"] == "active" and state["submitted"] is None

    listed = json.loads(ctx.tools["interact_list"]["handler"]({}))
    assert [p["page_id"] for p in listed["pages"]] == [result["page_id"]]
    assert result["page_id"] in ctx.commands["interact"]("")

    closed = json.loads(ctx.tools["interact_close"]["handler"]({"page_id": result["page_id"]}))
    assert closed == {"page_id": result["page_id"], "status": "closed", "tombstoned": True}


def test_telegram_answer_reaches_the_session(ctx, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_KEY", "agent:main:telegram:dm:7")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "7")
    page_id = json.loads(ctx.tools["interact_create"]["handler"]({"title": "T", "html": "<p>x</p>"}))["page_id"]

    class App:  # the factory registers the web_app_data handler on the native app
        bot = object()
        handlers = []

        def add_handler(self, handler, group=0):
            self.handlers.append(handler)

    app = App()
    ctx.platform_handlers["telegram"](app, None)
    assert len(app.handlers) == 1

    service = ctx.commands["interact"].__closure__[0].cell_contents
    reply = service.handle_web_app_data(data=json.dumps({"v": 1, "p": page_id, "a": "ok", "s": {"k": 1}}),
                                        user_id="7", user_name="Bo")
    assert reply.startswith("✅")
    assert ctx.injected[0][1] == "agent:main:telegram:dm:7" and '"k": 1' in ctx.injected[0][0]


def test_tool_errors_are_json(ctx):
    assert json.loads(ctx.tools["interact_get_state"]["handler"]({"page_id": "missing"}))["error"] == "not_found"
    assert json.loads(ctx.tools["interact_create"]["handler"]({"title": "x"}))["error"] == "invalid_html"


def test_cli_parser_builds(ctx):
    setup_fn, handler_fn = ctx.cli["interact"]
    parser = argparse.ArgumentParser()
    setup_fn(parser)
    args = parser.parse_args(["close", "abc", "--purge"])
    assert args.interact_command == "close" and args.purge is True
    assert handler_fn(parser.parse_args(["list"])) == 0

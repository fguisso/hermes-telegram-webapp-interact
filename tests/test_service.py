import json

import pytest
from conftest import SESSION, payload

from interact.service import InteractError


def _create(service, **kw):
    params = dict(title="Preços", html="<p data-bind='x'></p>", initial_state={"x": 1}, session=SESSION)
    params.update(kw)
    return service.create_page(**params)


def test_create_deploys_a_static_page_and_sends_the_keyboard_button(service):
    result = _create(service, message="Revise, por favor")
    assert result["delivered"] == "telegram"
    assert result["url"].startswith("https://pages.example.com/p/")
    assert service.bridge.sent[0] == {"chat_id": "42", "text": "Revise, por favor", "button_text": "Abrir",
                                      "url": result["url"]}

    html = service.backend.deploys[0]["html"]
    config = json.loads(html.split("window.__INTERACT__ = ", 1)[1].split(";</script>", 1)[0])
    assert config["pageId"] == result["page_id"] and config["initialState"] == {"x": 1}
    assert config["expiresAt"] > 0 and "apiBase" not in config

    page = service.store.get(result["page_id"])
    assert page.session_key == SESSION["session_key"] and page.user_id == "42"


def test_create_outside_telegram_returns_the_url(service):
    result = _create(service, session={"platform": "cli"})
    assert result["delivered"] == "url" and not service.bridge.sent


def test_delivery_failure_is_reported(service):
    def boom(*args):
        raise RuntimeError("Telegram allows web_app keyboard buttons only in private chats")

    service.bridge.send_page_button = boom
    result = _create(service, session={**SESSION, "chat_id": "-100"})
    assert result["delivered"] == "failed" and "private chats" in result["error"]


@pytest.mark.parametrize("kwargs, code", [
    ({"title": "  "}, "invalid_title"),
    ({"html": ""}, "invalid_html"),
    ({"initial_state": ["not", "an", "object"]}, "invalid_state"),
    ({"initial_state": {"big": "x" * 4000}}, "state_too_large"),
    ({"main_button": {"action": "go"}}, "invalid_main_button"),
])
def test_create_validates_input(service, kwargs, code):
    with pytest.raises(InteractError) as err:
        _create(service, **kwargs)
    assert err.value.code == code


def test_web_app_data_is_injected_into_the_session(service, injected):
    page_id = _create(service)["page_id"]
    reply = service.handle_web_app_data(data=payload(page_id, {"x": 2}, extra={"row": 1}),
                                        user_id="42", chat_id="42", user_name="Ana Lima")
    assert reply == "✅ Recebido: “Preços”."

    content, session_key = injected[0]
    assert session_key == SESSION["session_key"]
    assert content.startswith('[PluginInteract] Ana Lima submitted the interactive page "Preços"')
    assert "action: approve" in content and '"x": 2' in content and 'extra: {"row": 1}' in content

    state = service.get_state(page_id)
    assert state["status"] == "submitted" and state["submitted"]["state"] == {"x": 2}

    again = service.handle_web_app_data(data=payload(page_id, {"x": 3}), user_id="42")
    assert again == "Você já enviou esta página." and len(injected) == 1


def test_web_app_data_from_another_user_is_refused(service, injected):
    page_id = _create(service)["page_id"]
    assert "outra pessoa" in service.handle_web_app_data(data=payload(page_id, {}), user_id="99")
    assert not injected and service.get_state(page_id)["status"] == "active"


def test_allowed_users_and_prefixed_hermes_ids(service, injected):
    service.settings.allowed_users = {"99"}
    page_id = _create(service)["page_id"]
    assert service.handle_web_app_data(data=payload(page_id, {}), user_id="99").startswith("✅")
    other = _create(service, session={**SESSION, "user_id": "telegram:42"})["page_id"]
    assert service.handle_web_app_data(data=payload(other, {}), user_id="42").startswith("✅")


@pytest.mark.parametrize("data", ["not json", "[]", '{"v": 2, "p": "x", "s": {}}', '{"v": 1, "p": "x", "s": []}'])
def test_malformed_web_app_data(service, data, injected):
    assert service.handle_web_app_data(data=data, user_id="42").startswith("⚠️ Não entendi")
    assert not injected


def test_unknown_page(service):
    assert "Não encontrei" in service.handle_web_app_data(data=payload("nope00000000", {}), user_id="42")


def test_expired_and_closed_pages_refuse_answers(service, injected):
    page_id = _create(service, ttl_minutes=10)["page_id"]
    created = service.store.get(page_id).created_at
    service.clock = lambda: created + 11 * 60
    assert service.handle_web_app_data(data=payload(page_id, {}), user_id="42") == "⌛ Esta página expirou."

    service.clock = lambda: created
    closed = _create(service)["page_id"]
    service.close_page(closed)
    assert "encerrada" in service.handle_web_app_data(data=payload(closed, {}), user_id="42")
    assert not injected


def test_failed_injection_tells_the_user(service):
    service.injector = lambda content, key: False
    page_id = _create(service)["page_id"]
    assert page_id in service.handle_web_app_data(data=payload(page_id, {}), user_id="42")


def test_close_tombstones(service):
    page_id = _create(service)["page_id"]
    result = service.close_page(page_id, purge=True)
    assert result["status"] == "closed" and result["tombstoned"] is True
    removed = service.backend.removed[0]
    assert removed["remote_id"] == f"r-{page_id}" and removed["purge"] is True
    assert "Página encerrada" in removed["html"]


def test_sweep_expires_overdue_pages(service):
    page_id = _create(service, ttl_minutes=10)["page_id"]
    created = service.store.get(page_id).created_at
    service.clock = lambda: created + 11 * 60
    assert service.get_state(page_id)["status"] == "expired"
    assert service.sweep_expired() == 1
    assert service.store.get(page_id).status == "expired"
    assert "Página expirada" in service.backend.removed[0]["html"]

    service.clock = lambda: created + 30 * 86400
    service.sweep_expired()
    assert service.store.get(page_id) is None  # forgotten after the retention window


def test_update_redeploys_to_the_same_remote(service):
    page_id = _create(service)["page_id"]
    first_url = service.store.get(page_id).url
    result = service.update_page(page_id, html="<h1>v2</h1>", title="Preços v2", initial_state={"x": 9})
    assert result["url"] == first_url
    assert service.backend.deploys[-1]["remote_id"] == f"r-{page_id}"
    assert '"initialState": {"x": 9}' in service.backend.deploys[-1]["html"]
    assert service.store.get(page_id).title == "Preços v2"


def test_list_scopes_to_the_session(service):
    _create(service)
    _create(service, session={**SESSION, "session_key": "other"})
    assert len(service.list_pages(session_key=SESSION["session_key"])) == 1
    assert len(service.list_pages()) == 2

"""OAuth provider integration: real SDK payloads with isolated credentials."""
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from typer.testing import CliRunner

from tradingagents.llm_clients import chatgpt_auth as auth
from tradingagents.llm_clients.factory import create_llm_client
from tradingagents.llm_clients.openai_client import ChatGPTPlanChatOpenAI


@pytest.fixture
def signed_in(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "chatgpt-auth.json")
    auth._write({"account": {"email": "test@example.com", "client_id": "test"},
                 "tokens": {"access_token": "plan-token", "expires_at": time.time() + 3600}})


def llm(**kwargs):
    return create_llm_client(" ChatGPT ", "gpt-6-sol", **kwargs).get_llm()


def test_plan_credential_endpoint_and_headers(signed_in, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://relay.invalid/v1")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-api")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-api")
    model = llm(reasoning_effort="high")
    assert isinstance(model, ChatGPTPlanChatOpenAI)
    assert model.openai_api_key() == "plan-token"
    assert model.openai_api_base == auth.API_BASE
    assert model.reasoning_effort == "high"
    for client in (model.root_client, model.root_async_client):
        assert client.organization is None and client.project is None


def test_payload_and_tool_binding(signed_in):
    model = llm(max_tokens=500)
    payload = model._get_request_payload([SystemMessage("Be brief"), HumanMessage("Hi")],
                                         temperature=0.2, top_p=0.9, max_tokens=99, store=True)
    assert [item["role"] for item in payload["input"]] == ["developer", "user"]
    assert payload["stream"] is True and payload["store"] is False
    assert not {"temperature", "top_p", "max_tokens", "max_output_tokens"} & payload.keys()
    tool = {"type": "function", "function": {"name": "quote", "description": "Get quote",
             "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}}}}}
    bound = model.bind_tools([tool])
    assert bound.kwargs["tools"][0]["function"]["name"] == "quote"


def test_credential_reloaded_per_request(signed_in):
    model = llm()
    data = json.loads(auth.AUTH_FILE.read_text())
    data["tokens"]["access_token"] = "rotated-token"
    auth._write(data)
    assert model.openai_api_key() == "rotated-token"


def test_missing_login_fails_before_request(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "missing.json")
    with pytest.raises(auth.ChatGPTAuthError, match="login chatgpt"):
        llm()


@pytest.mark.parametrize("kwargs", [{"base_url": "https://relay.invalid/v1"}, {"api_key": "wrong"}])
def test_rejects_custom_endpoint_and_key(signed_in, kwargs):
    with pytest.raises(ValueError):
        llm(**kwargs)


def test_login_logout_commands(monkeypatch):
    from cli.main import app
    monkeypatch.setattr(auth, "sign_in", lambda **kw: "test@example.com")
    monkeypatch.setattr(auth, "sign_out", lambda: "revoked")
    runner = CliRunner()
    login = runner.invoke(app, ["login", "chatgpt"])
    assert login.exit_code == 0, login.output
    assert "test@example.com" in login.output
    logout = runner.invoke(app, ["logout", "chatgpt"])
    assert logout.exit_code == 0 and "Signed out" in logout.output


def test_login_error_is_actionable(monkeypatch):
    from cli.main import app
    def fail(**kw):
        raise auth.ChatGPTAuthError("Sign-in timed out; try again")
    monkeypatch.setattr(auth, "sign_in", fail)
    result = CliRunner().invoke(app, ["login", "chatgpt"])
    assert result.exit_code == 1 and "timed out" in result.output


def test_cli_provider_menu():
    from cli import utils
    def select(message, choices, **kw):
        selected = next(c.value for c in choices if c.value[0] == "chatgpt")
        return SimpleNamespace(ask=lambda: selected)
    with patch.object(utils.questionary, "select", select):
        assert utils.select_llm_provider() == ("chatgpt", auth.API_BASE)


def test_model_listing_uses_plan_token(signed_in, monkeypatch):
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"models": [
        {"slug": "gpt-6-sol", "visibility": "list", "priority": 2},
        {"slug": "hidden", "visibility": "hide"},
        {"slug": "gpt-6-luna", "visibility": "list", "priority": 1}]})
    with patch.object(auth.requests, "get", return_value=response) as get:
        assert auth.list_models() == [("gpt-6-luna", "gpt-6-luna"), ("gpt-6-sol", "gpt-6-sol")]
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer plan-token"


@pytest.mark.parametrize("terminal", ["completed", "incomplete", "failed", "interrupted"])
@pytest.mark.parametrize("async_mode", [False, True])
def test_real_sdk_stream_and_tool_roundtrip(signed_in, terminal, async_mode):
    import httpx
    from langchain_core.messages import ToolMessage
    events = [
        {"type": "response.created", "response": {"id": "resp_test", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"id": "msg_test", "type": "message", "role": "assistant", "content": []}},
        {"type": "response.output_text.delta", "item_id": "msg_test", "output_index": 0,
         "content_index": 0, "delta": "OK"},
    ]
    if terminal != "interrupted":
        events.append({"type": "response." + terminal, "response": {
            "id": "resp_test", "object": "response", "model": "gpt-6-sol", "created_at": 1,
            "status": terminal, "output": [], "error": None if terminal != "failed" else
            {"code": "subscription_sharing_usage_limit_exceeded", "message": "Plan limit reached"},
            "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}}})
    body = "".join("event: " + e["type"] + "\n" + "data: " + json.dumps(e) + "\n\n" for e in events)
    sent = []
    def handle(request):
        assert str(request.url) == auth.API_BASE + "/responses"
        assert request.headers["authorization"] == "Bearer plan-token"
        sent.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
    tool = {"type": "function", "function": {"name": "quote", "description": "Get quote",
            "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}}}}}
    model = llm(http_client=httpx.Client(transport=httpx.MockTransport(handle)),
                http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), max_retries=0)
    bound = model.bind_tools([tool])
    messages = [SystemMessage("Analyze"), HumanMessage("Quote"),
                AIMessage(
                    content="", tool_calls=[{"name": "quote", "args": {"ticker": "600519"}, "id": "call1"}]),
                ToolMessage(content="100", tool_call_id="call1")]
    def invoke():
        if async_mode:
            import asyncio
            return asyncio.run(bound.ainvoke(messages))
        return bound.invoke(messages)
    if terminal == "completed":
        result = invoke()
        assert result.content == "OK"
        assert result.usage_metadata["total_tokens"] == 11
    else:
        with pytest.raises((ValueError, RuntimeError), match="incomplete|limit|response.completed"):
            invoke()
    payload = sent[0]
    assert payload["tools"][0]["type"] == "namespace"
    assert payload["tools"][0]["tools"][0]["name"] == "quote"
    assert any(item.get("type") == "function_call_output" for item in payload["input"])



def test_status_command(signed_in):
    from cli.main import app
    result = CliRunner().invoke(app, ["status", "chatgpt"])
    assert result.exit_code == 0 and "test@example.com" in result.output


def test_web_sidebar_chatgpt_selection_and_models(signed_in):
    from streamlit.testing.v1 import AppTest
    from web.components.sidebar import _PROVIDER_KEYS
    app = AppTest.from_string("from web.components.sidebar import _render_llm_config; _render_llm_config()")
    app.run(timeout=15)
    app.selectbox(key="llm_provider_idx").select(_PROVIDER_KEYS.index("chatgpt")).run(timeout=15)
    assert not app.exception
    assert app.session_state["llm_provider"] == "chatgpt"
    assert app.session_state["quick_think_llm"] == "gpt-6-luna"
    assert app.session_state["deep_think_llm"] == "gpt-6-sol"
    assert app.text_input(key="llm_base_url").disabled

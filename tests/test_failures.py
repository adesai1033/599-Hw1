"""End-to-end through the HTTP layer with a scripted LLM: contract, the three MCP failure modes,
and request-level guards. Synchronous on purpose: TestClient runs the app on its own loop.
Launches only market_data (tools/list needs no key) and the two test servers; no network.
"""
import json
import logging
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import main
from tests.conftest import ScriptedChatModel, call

ROOT = Path(__file__).resolve().parent.parent
SERVERS = ROOT / "tests" / "servers"
TIMEOUT_SENTENCE = "That took too long to answer. Please try a narrower question."
ERROR_SENTENCE = "Something went wrong while answering. Please try again."


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for key in ("FINNHUB_API_KEY", "TWELVEDATA_API_KEY", "FRED_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def market_data_entry():
    entry = json.loads((ROOT / "mcp_config.json").read_text())["market_data"]
    return {**entry, "args": [str(ROOT / "mcp_servers" / "market_data.py")]}


def script_entry(filename, description):
    return {
        "transport": "stdio", "command": "python", "args": [str(SERVERS / filename)], "description": description,
    }


def make_app(tmp_path, servers, script, model_cls=ScriptedChatModel):
    manifest = tmp_path / "mcp.json"
    manifest.write_text(json.dumps(servers))
    model = model_cls(script=script)
    return main.create_app(str(manifest), llm=model), model


def chat(client, query="hi", session_id="s1"):
    return client.post("/chat", json={"query": query, "session_id": session_id})


def tool_message(app, session_id="s1"):
    state = app.state.graph.get_state({"configurable": {"thread_id": session_id}})
    return next(m for m in state.values["messages"] if isinstance(m, ToolMessage))


def test_chat_contract(tmp_path):
    app, _ = make_app(tmp_path, {"market_data": market_data_entry()}, [AIMessage("hello")])
    with TestClient(app) as client:
        reply = chat(client)
    assert reply.status_code == 200 and reply.json() == {"response": "hello"}


def test_invalid_bodies_are_422(tmp_path):
    app, _ = make_app(tmp_path, {"market_data": market_data_entry()}, [AIMessage("x")])
    with TestClient(app) as client:
        assert client.post("/chat", json={"query": "hi"}).status_code == 422
        assert chat(client, "   ").status_code == 422
        assert chat(client, "q" * 4001).status_code == 422


def test_sessions_share_history_only_within_a_session(tmp_path):
    script = [AIMessage("a1"), AIMessage("a2"), AIMessage("b1")]
    app, model = make_app(tmp_path, {"market_data": market_data_entry()}, script)
    with TestClient(app) as client:
        chat(client, "first", "a")
        chat(client, "second", "a")
        chat(client, "third", "b")
    assert [m.content for m in model.calls[1][1:]] == ["first", "a1", "second"]
    assert [m.content for m in model.calls[2][1:]] == ["third"]


def test_health_reports_servers_and_tools(tmp_path):
    app, _ = make_app(tmp_path, {"market_data": market_data_entry()}, [AIMessage("x")])
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok" and body["servers"] == {"market_data": "up"} and body["unavailable"] == {}
    assert set(body["tools"]) == {
        "get_quote", "get_price_history", "compare_performance", "get_company_profile", "get_company_news",
    }


def test_startup_requires_an_llm_key_unless_a_model_is_injected(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    manifest = tmp_path / "mcp.json"
    manifest.write_text(json.dumps({"market_data": market_data_entry()}))
    with pytest.raises(RuntimeError):
        with TestClient(main.create_app(str(manifest))):
            pass
    app, _ = make_app(tmp_path, {"market_data": market_data_entry()}, [AIMessage("x")])
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


# Failure mode 1: transport.
def test_server_unavailable_at_startup(tmp_path):
    broken = {"transport": "stdio", "command": "/nonexistent/binary", "args": [], "description": "a broken test server"}
    app, model = make_app(tmp_path, {"market_data": market_data_entry(), "broken": broken}, [AIMessage("still here")])
    with TestClient(app) as client:
        health = client.get("/health").json()
        reply = chat(client)
    assert health["servers"] == {"market_data": "up", "broken": "down"}
    assert health["unavailable"] == {"broken": "a broken test server"}
    assert reply.status_code == 200 and reply.json()["response"] == "still here"
    system = model.calls[0][0]
    assert isinstance(system, SystemMessage) and "Unavailable this session: a broken test server" in system.content


def test_server_dies_mid_call_and_next_session_self_heals(tmp_path):
    flaky = {"flaky": script_entry("flaky_server.py", "a flaky test server")}
    script = [call("die"), AIMessage("The data server disconnected."), call("garbage_then_ok"), AIMessage("fine")]
    app, _ = make_app(tmp_path, flaky, script)
    with TestClient(app) as client:
        first = chat(client, "kill it", "one")
        second = chat(client, "try again", "two")
        died, healed = tool_message(app, "one"), tool_message(app, "two")
    assert first.status_code == 200 and first.json()["response"] == "The data server disconnected."
    assert died.status == "error" and "Connection closed" in str(died.content)
    assert second.status_code == 200
    assert healed.status == "success" and "ok after garbage" in str(healed.content)


# Failure mode 2: tool execution error.
def test_tool_error_for_invalid_symbol(tmp_path):
    script = [call("get_quote", "N V DA"), AIMessage("That isn't a valid ticker.")]
    app, _ = make_app(tmp_path, {"market_data": market_data_entry()}, script)
    with TestClient(app) as client:
        reply = chat(client)
        message = tool_message(app)
    assert reply.status_code == 200 and reply.json()["response"] == "That isn't a valid ticker."
    assert message.status == "error" and "Invalid symbol" in str(message.content)


def test_tool_error_for_unconfigured_provider_hides_the_key_name(tmp_path):
    script = [call("get_quote", "NVDA"), AIMessage("Market data isn't configured.")]
    app, _ = make_app(tmp_path, {"market_data": market_data_entry()}, script)
    with TestClient(app) as client:
        reply = chat(client)
        message = tool_message(app)
    assert reply.status_code == 200
    assert "not configured" in str(message.content) and "FINNHUB" not in str(message.content)


# Failure mode 3: malformed / off-schema response.
def test_off_schema_result_becomes_a_tool_error(tmp_path):
    script = [call("offschema"), AIMessage("The server returned something I couldn't read.")]
    app, _ = make_app(tmp_path, {"offschema": script_entry("offschema_server.py", "an off-schema test server")}, script)
    with TestClient(app) as client:
        reply = chat(client)
        message = tool_message(app)
    assert reply.status_code == 200 and reply.json()["response"] == "The server returned something I couldn't read."
    assert message.status == "error"
    assert "Error invoking tool 'offschema'" in str(message.content) and "valid list" in str(message.content)


def test_stdout_garbage_is_absorbed_by_the_sdk(tmp_path):
    # The SDK drops the non-JSON-RPC line and the real response still arrives.
    script = [call("garbage_then_ok"), AIMessage("fine")]
    app, _ = make_app(tmp_path, {"flaky": script_entry("flaky_server.py", "a flaky test server")}, script)
    with TestClient(app) as client:
        reply = chat(client)
        message = tool_message(app)
    assert reply.status_code == 200 and message.status == "success"


# Request-level guards.
class SlowModel(ScriptedChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        time.sleep(3)
        return super()._generate(messages, stop, run_manager, **kwargs)


class BrokenModel(ScriptedChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("llm down")


def test_slow_request_times_out_with_200(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "1")
    app, _ = make_app(tmp_path, {"market_data": market_data_entry()}, [AIMessage("late")], SlowModel)
    with TestClient(app) as client:
        with caplog.at_level(logging.INFO, logger="main"):
            reply = chat(client)
    assert reply.status_code == 200 and reply.json()["response"] == TIMEOUT_SENTENCE
    assert len([r for r in caplog.records if r.name == "main" and r.levelno == logging.WARNING]) == 1


def test_llm_exception_returns_200_and_logs_the_traceback(tmp_path, caplog):
    app, _ = make_app(tmp_path, {"market_data": market_data_entry()}, [AIMessage("unused")], BrokenModel)
    with TestClient(app) as client:
        with caplog.at_level(logging.INFO, logger="main"):
            reply = chat(client, session_id="boom")
    assert reply.status_code == 200 and reply.json()["response"] == ERROR_SENTENCE
    errors = [r for r in caplog.records if r.name == "main" and r.levelno == logging.ERROR]
    assert len(errors) == 1 and "boom" in errors[0].getMessage() and errors[0].exc_info


def test_request_log_has_no_query_text(tmp_path, caplog):
    app, _ = make_app(tmp_path, {"market_data": market_data_entry()}, [AIMessage("ok")])
    with TestClient(app) as client:
        with caplog.at_level(logging.INFO, logger="main"):
            chat(client, "a very private question", "s1")
    infos = [r.getMessage() for r in caplog.records if r.name == "main" and r.levelno == logging.INFO]
    assert any(re.fullmatch(r"chat session=s1 query_chars=\d+ elapsed_ms=\d+", m) for m in infos)
    assert not any("private" in m for m in infos)

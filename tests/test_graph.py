"""Graph loop-back, memory, failure handling, config loading, and discovery. No OpenAI calls.

Graph-logic tests use a scripted fake model and local tools. Discovery tests spawn the real
market_data server over stdio (tools/list needs no key and no network).
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from pydantic import Field

import agent
import mcp_client
from agent import EMPTY_ANSWER, RECURSION_ANSWER, SYSTEM_PROMPT, ask, build_graph, system_message
from mcp_client import discover_tools, load_config
from mcp_servers import market_data

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parent.parent
MARKET_DATA_TOOLS = {"get_quote", "get_price_history", "compare_performance", "get_company_profile", "get_company_news"}


class ScriptedChatModel(BaseChatModel):
    script: list[AIMessage]
    calls: list = Field(default_factory=list)
    bound_tools: list = Field(default_factory=list)
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        message = self.script[self.cursor]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = list(tools)
        return self


def call(name, symbol, call_id):
    return AIMessage("", tool_calls=[{"name": name, "args": {"symbol": symbol}, "id": call_id}])


@pytest.fixture(autouse=True)
def repo_root(monkeypatch):
    monkeypatch.chdir(ROOT)


@pytest.fixture
def quote_calls():
    return []


@pytest.fixture
def tools(quote_calls):
    @tool
    def fake_quote(symbol: str) -> dict:
        """Return a fake quote for a ticker."""
        quote_calls.append({"symbol": symbol})
        return {"symbol": symbol, "price": 133}

    @tool
    def failing_tool(symbol: str) -> dict:
        """Always fails."""
        raise ValueError(f"No quote found for {symbol}.")

    return [fake_quote, failing_tool]


def run_config(session="s1"):
    return {"configurable": {"thread_id": session}}


async def thread(graph, session="s1"):
    return (await graph.aget_state(run_config(session))).values["messages"]


async def test_loop_back_on_tool_call(tools, quote_calls):
    model = ScriptedChatModel(script=[call("fake_quote", "NVDA", "c1"), AIMessage("NVDA is at 133.")])
    graph = build_graph(tools, model, MemorySaver())
    assert await ask(graph, "quote for NVDA", "s1") == "NVDA is at 133."
    kinds = [type(m) for m in await thread(graph)]
    assert kinds == [HumanMessage, AIMessage, ToolMessage, AIMessage]
    assert quote_calls == [{"symbol": "NVDA"}]
    assert len(model.calls) == 2
    assert [t.name for t in model.bound_tools] == ["fake_quote", "failing_tool"]


async def test_multi_step_chain(tools, quote_calls):
    model = ScriptedChatModel(script=[
        call("fake_quote", "NVDA", "c1"), call("fake_quote", "AMD", "c2"), AIMessage("done"),
    ])
    graph = build_graph(tools, model, MemorySaver())
    await ask(graph, "compare", "s1")
    assert len(await thread(graph)) == 6
    assert [c["symbol"] for c in quote_calls] == ["NVDA", "AMD"]
    assert len(model.calls) == 3


async def test_no_tool_needed(tools, quote_calls):
    model = ScriptedChatModel(script=[AIMessage("A P/E ratio is price over earnings.")])
    graph = build_graph(tools, model, MemorySaver())
    await ask(graph, "what's a P/E ratio?", "s1")
    assert len(await thread(graph)) == 2 and quote_calls == [] and len(model.calls) == 1


async def test_system_prompt_is_prepended_not_stored(tools):
    model = ScriptedChatModel(script=[call("fake_quote", "NVDA", "c1"), AIMessage("ok")])
    graph = build_graph(tools, model, MemorySaver())
    await ask(graph, "quote", "s1")
    for sent in model.calls:
        assert isinstance(sent[0], SystemMessage) and sent[0].content == SYSTEM_PROMPT
    assert not any(isinstance(m, SystemMessage) for m in await thread(graph))


async def test_memory_within_thread_and_isolation_across_threads(tools):
    model = ScriptedChatModel(script=[AIMessage("a1"), AIMessage("a2"), AIMessage("b1")])
    graph = build_graph(tools, model, MemorySaver())
    await ask(graph, "q1", "a")
    await ask(graph, "q2", "a")
    second_input = model.calls[1]
    assert [m.content for m in second_input[1:]] == ["q1", "a1", "q2"]
    assert len(await thread(graph, "a")) == 4
    await ask(graph, "q3", "b")
    assert [m.content for m in model.calls[2][1:]] == ["q3"]
    assert len(await thread(graph, "b")) == 2 and len(await thread(graph, "a")) == 4


async def test_tool_error_is_graceful(tools):
    model = ScriptedChatModel(script=[call("failing_tool", "ZZZZQ", "c1"), AIMessage("I couldn't get a quote for ZZZZQ.")])
    graph = build_graph(tools, model, MemorySaver())
    assert await ask(graph, "quote ZZZZQ", "s1") == "I couldn't get a quote for ZZZZQ."
    tool_message = next(m for m in await thread(graph) if isinstance(m, ToolMessage))
    assert tool_message.status == "error" and "No quote found for ZZZZQ" in str(tool_message.content)
    assert any(isinstance(m, ToolMessage) for m in model.calls[1])


async def test_recursion_guard(tools, caplog):
    model = ScriptedChatModel(script=[call("fake_quote", "NVDA", f"c{i}") for i in range(50)])
    graph = build_graph(tools, model, MemorySaver())
    with caplog.at_level(logging.INFO, logger="agent"):
        answer = await ask(graph, "loop forever", "s1", recursion_limit=6)
    assert answer == RECURSION_ANSWER
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_final_text_handles_content_blocks():
    blocks = [{"type": "text", "text": "Hello "}, {"type": "text", "text": "world"}, {"type": "tool_use", "text": "SHOULD NOT APPEAR"}]
    assert agent._final_text(AIMessage(content=blocks)) == "Hello world"
    assert agent._final_text(AIMessage(content="")) == EMPTY_ANSWER


async def test_graph_works_with_zero_tools():
    model = ScriptedChatModel(script=[AIMessage("A P/E ratio is price over earnings.")])
    graph = build_graph([], model, MemorySaver())
    assert "price over earnings" in await ask(graph, "what's a P/E ratio?", "s1")
    assert model.bound_tools == []


async def test_tool_calls_and_results_are_logged(tools, caplog):
    model = ScriptedChatModel(script=[call("fake_quote", "NVDA", "c1"), AIMessage("ok")])
    graph = build_graph(tools, model, MemorySaver())
    with caplog.at_level(logging.INFO, logger="agent"):
        await ask(graph, "quote", "s1")
    messages = [r.getMessage() for r in caplog.records]
    assert len([m for m in messages if "tool_call name=fake_quote" in m]) == 1
    assert len([m for m in messages if "tool_result name=fake_quote status=success" in m]) == 1


async def test_load_config_substitutes_env(tmp_path, monkeypatch, caplog):
    manifest = tmp_path / "mcp.json"
    manifest.write_text(json.dumps({"s": {
        "transport": "stdio", "command": "python", "args": [],
        "env": {"FINNHUB_API_KEY": "${FINNHUB_API_KEY}", "OTHER": "${UNSET_VAR_XYZ}"},
    }}))
    monkeypatch.setenv("FINNHUB_API_KEY", "abc")
    monkeypatch.delenv("UNSET_VAR_XYZ", raising=False)
    with caplog.at_level(logging.WARNING, logger="mcp_client"):
        config = load_config(manifest)
    assert config["s"]["env"] == {"FINNHUB_API_KEY": "abc", "OTHER": ""}
    assert config["s"]["command"] == sys.executable
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and "UNSET_VAR_XYZ" in warned[0].getMessage()


async def test_load_config_include():
    assert list(load_config("mcp_config.json", include={"market_data"})) == ["market_data"]
    with pytest.raises(ValueError):
        load_config("mcp_config.json", include={"nope"})


async def discover_market_data():
    return await asyncio.wait_for(
        discover_tools(load_config("mcp_config.json", include={"market_data"})), timeout=60
    )


async def test_real_discovery(monkeypatch):
    for key in ("FINNHUB_API_KEY", "TWELVEDATA_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    found, unavailable = await discover_market_data()
    assert {t.name for t in found} == MARKET_DATA_TOOLS and len(found) == 5 and unavailable == {}
    for found_tool in found:
        docstring = getattr(market_data, found_tool.name).__doc__
        assert found_tool.description.strip() == docstring.strip()


async def test_dead_server_is_skipped(caplog):
    config = load_config("mcp_config.json", include={"market_data"})
    config["broken"] = {"transport": "stdio", "command": "/nonexistent/binary", "args": []}
    with caplog.at_level(logging.INFO, logger="mcp_client"):
        found, unavailable = await asyncio.wait_for(discover_tools(config), timeout=60)
    assert len(found) == 5 and unavailable == {"broken": "broken"}
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "broken" in errors[0].getMessage()


async def test_real_mcp_tool_error_becomes_graceful_graph_step(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    found, _ = await discover_market_data()
    model = ScriptedChatModel(script=[call("get_quote", "N V DA", "c1"), AIMessage("That isn't a valid ticker.")])
    graph = build_graph(found, model, MemorySaver())
    assert await ask(graph, "quote N V DA", "s1") == "That isn't a valid ticker."
    tool_message = next(m for m in await thread(graph) if isinstance(m, ToolMessage))
    assert tool_message.status == "error" and "Invalid symbol" in str(tool_message.content)


async def test_tool_name_collision_raises():
    base = load_config("mcp_config.json", include={"market_data"})["market_data"]
    with pytest.raises(RuntimeError) as excinfo:
        await asyncio.wait_for(discover_tools({"first": base, "second": base}), timeout=60)
    assert "first" in str(excinfo.value) and "second" in str(excinfo.value)


async def test_load_config_expands_headers_and_keeps_tools(tmp_path, monkeypatch):
    manifest = tmp_path / "mcp.json"
    manifest.write_text(json.dumps({"h": {
        "transport": "streamable_http", "url": "https://example.test/mcp/",
        "headers": {"Authorization": "Bearer ${TAVILY_API_KEY}"}, "tools": ["a", "b"],
    }}))
    monkeypatch.setenv("TAVILY_API_KEY", "tv-secret")
    loaded = load_config(manifest)["h"]
    assert loaded["headers"] == {"Authorization": "Bearer tv-secret"} and loaded["tools"] == ["a", "b"]


async def test_allowlist_filters_and_warns_about_missing(caplog):
    config = load_config("mcp_config.json", include={"market_data"})
    config["market_data"]["tools"] = ["get_quote", "compare_performance", "not_a_tool"]
    with caplog.at_level(logging.INFO, logger="mcp_client"):
        found, unavailable = await asyncio.wait_for(discover_tools(config), timeout=60)
    assert [t.name for t in found] == ["get_quote", "compare_performance"] and unavailable == {}
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "allowlisted" in r.getMessage()]
    assert len(warned) == 1 and "market_data" in warned[0] and "not_a_tool" in warned[0]
    assert any("(2 of 5 kept)" in r.getMessage() for r in caplog.records)


async def test_real_discovery_of_both_stdio_servers(monkeypatch):
    for key in ("FINNHUB_API_KEY", "TWELVEDATA_API_KEY", "FRED_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    config = load_config("mcp_config.json", include={"market_data", "fred"})
    found, unavailable = await asyncio.wait_for(discover_tools(config), timeout=60)
    assert {t.name for t in found} == MARKET_DATA_TOOLS | {"get_series", "search_series", "get_macro_snapshot"}
    assert len(found) == 8 and unavailable == {}


async def test_dead_http_server_is_skipped_quickly(caplog):
    config = load_config("mcp_config.json", include={"market_data"})
    config["dead_http"] = {"transport": "streamable_http", "url": "http://127.0.0.1:9/mcp/"}
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="mcp_client"):
        found, unavailable = await asyncio.wait_for(discover_tools(config), timeout=60)
    assert time.monotonic() - started < 15
    assert len(found) == 5 and unavailable == {"dead_http": "dead_http"}
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "dead_http" in errors[0].getMessage()


async def test_tavily_manifest_is_streamable_http():
    tavily = load_config("mcp_config.json", include={"tavily"})["tavily"]
    assert tavily["transport"] == "streamable_http" and tavily["url"].startswith("https://mcp.tavily.com")
    assert tavily["headers"]["Authorization"].startswith("Bearer ")
    assert tavily["tools"] == ["tavily_search", "tavily_extract"]


@pytest.mark.network
async def test_discovery_tolerates_invalid_tavily_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "invalid")
    config = load_config("mcp_config.json", include={"tavily"})
    found, unavailable = await asyncio.wait_for(discover_tools(config), timeout=30)
    assert set(unavailable) == {"tavily"} or {t.name for t in found} <= {"tavily_search", "tavily_extract"}


async def test_system_message_without_unavailable_servers():
    for empty in (None, {}):
        message = system_message(empty)
        assert isinstance(message, SystemMessage) and message.content == SYSTEM_PROMPT


async def test_system_message_lists_unavailable_servers():
    content = system_message({"fred": "macro data", "tavily": "web search"}).content
    assert content.startswith(SYSTEM_PROMPT)
    assert "Unavailable this session: macro data; web search." in content
    assert "do not offer to look it up later" in content


async def test_discovery_returns_descriptions_of_failed_servers():
    base = load_config("mcp_config.json", include={"market_data"})
    broken = {"transport": "stdio", "command": "/nonexistent/binary", "args": []}
    config = {**base, "broken": {**broken, "description": "fake broken server"}}
    _, unavailable = await asyncio.wait_for(discover_tools(config), timeout=60)
    assert unavailable == {"broken": "fake broken server"}
    _, unavailable = await asyncio.wait_for(discover_tools({**base, "broken": broken}), timeout=60)
    assert unavailable == {"broken": "broken"}


async def test_manifest_only_keys_stay_out_of_the_connection(tmp_path, monkeypatch):
    manifest = tmp_path / "mcp.json"
    manifest.write_text(json.dumps({"s": {
        "transport": "stdio", "command": "x", "args": [], "tools": ["a"], "description": "d",
    }}))
    config = load_config(manifest)
    assert config["s"]["description"] == "d" and config["s"]["tools"] == ["a"]
    seen = []

    class RecordingClient:
        def __init__(self, connections):
            seen.append(connections)

        async def get_tools(self):
            raise RuntimeError("stop here")

    monkeypatch.setattr(mcp_client, "MultiServerMCPClient", RecordingClient)
    _, unavailable = await discover_tools(config)
    assert unavailable == {"s": "d"}
    assert "tools" not in seen[0]["s"] and "description" not in seen[0]["s"]


async def test_graph_tells_the_model_which_servers_are_down(tools):
    model = ScriptedChatModel(script=[AIMessage("ok")])
    graph = build_graph(tools, model, MemorySaver(), unavailable={"fred": "macro data"})
    await ask(graph, "rates?", "s1")
    first = model.calls[0][0]
    assert isinstance(first, SystemMessage) and "Unavailable this session: macro data." in first.content
    assert not any(isinstance(m, SystemMessage) for m in await thread(graph))


async def test_graph_without_unavailable_servers_uses_plain_prompt(tools):
    model = ScriptedChatModel(script=[AIMessage("ok")])
    await ask(build_graph(tools, model, MemorySaver()), "hi", "s1")
    assert model.calls[0][0].content == SYSTEM_PROMPT


async def test_repl_starts_and_reaches_the_prompt():
    env = {**os.environ, "OPENAI_API_KEY": "test-key-not-used"}
    result = subprocess.run(
        [sys.executable, "scripts/repl.py", "--servers", "market_data"],
        input="", capture_output=True, text=True, cwd=ROOT, env=env, timeout=60,
    )
    assert result.returncode == 0 and "> " in result.stdout

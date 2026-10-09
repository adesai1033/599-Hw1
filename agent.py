"""LangGraph agent: agent node, tools node, conditional edge, MemorySaver."""
import json
import logging
import os
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from mcp_client import discover_tools, load_config

logger = logging.getLogger("agent")

SYSTEM_PROMPT = """You are a market-watcher assistant. You answer questions about US stocks,
companies, and the macroeconomic backdrop using the tools available to you.

Rules:
- Use a tool when the answer depends on current or historical data you do not have.
  Do not call a tool for definitions, concepts, or general knowledge.
- When a question compares several tickers, prefer one `compare_performance` call over
  several `get_price_history` calls.
- Choosing between news tools: use `get_company_news` for recent headlines about one
  specific ticker. Use `tavily_search` for anything broader — market-wide moves, sector
  news, macroeconomic events, analyst commentary, or explaining *why* something happened.
  Use `tavily_extract` only when you already have a URL and need its full text.
- For questions about interest rates, inflation, unemployment, or whether a move is
  "about the market" rather than one company, call `get_macro_snapshot` first; use
  `get_series` for the history of one indicator and `search_series` when you need a
  FRED series ID you do not know.
- When you use web search results, name the source in your answer (publication and
  date), and prefer results from the last few days for "why did X move" questions.
- Read tool results carefully; do not restate numbers you did not receive.
- If a tool returns an error, tell the user plainly what could not be retrieved and
  answer with whatever you do have. Never invent data to fill a gap.
- Refer back to earlier turns in this conversation when the user does; "what about
  six months instead" means the same tickers as before over a six-month window.
- Be concise. Lead with the answer, then the supporting numbers.
"""
EMPTY_ANSWER = "I wasn't able to produce an answer."
RECURSION_ANSWER = "I couldn't finish answering that within the allowed number of steps. Try a narrower question."


def system_message(unavailable: dict[str, str] | None) -> SystemMessage:
    if not unavailable:
        return SystemMessage(SYSTEM_PROMPT)
    return SystemMessage(
        f"{SYSTEM_PROMPT}\nUnavailable this session: {'; '.join(unavailable.values())}. "
        "If a question touches any of these, begin your answer by saying plainly that this data is "
        "unavailable right now, then answer with what you do have; do not offer to look it up later."
    )


def build_llm() -> ChatOpenAI:
    # No temperature: the gpt-5 family rejects non-default values.
    return ChatOpenAI(model=os.environ.get("OPENAI_MODEL", "gpt-5-mini"))


def build_graph(
    tools: list[BaseTool],
    llm: BaseChatModel,
    checkpointer: MemorySaver | None = None,
    unavailable: dict[str, str] | None = None,
) -> CompiledStateGraph:
    model = llm.bind_tools(tools) if tools else llm
    tool_node = ToolNode(tools, handle_tool_errors=True)
    system = system_message(unavailable)

    async def agent_node(state: MessagesState) -> dict:
        response = await model.ainvoke([system, *state["messages"]])
        for call in response.tool_calls:
            logger.info("tool_call name=%s args=%s", call["name"], json.dumps(call["args"]))
        return {"messages": [response]}

    async def tools_node(state: MessagesState, config: RunnableConfig) -> dict:
        result = await tool_node.ainvoke(state, config)
        for message in result["messages"]:
            logger.info(
                "tool_result name=%s status=%s chars=%d", message.name, message.status, len(str(message.content))
            )
        return result

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer or MemorySaver())


def _final_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return content.strip() or EMPTY_ANSWER


async def ask(graph: CompiledStateGraph, query: str, session_id: str, recursion_limit: int = 25) -> str:
    config: RunnableConfig = {"configurable": {"thread_id": session_id}, "recursion_limit": recursion_limit}
    try:
        result = await graph.ainvoke({"messages": [HumanMessage(query)]}, config)
    except GraphRecursionError:
        logger.warning("recursion limit reached for session %s", session_id)
        return RECURSION_ANSWER
    last_ai = next((m for m in reversed(result["messages"]) if isinstance(m, AIMessage)), None)
    return _final_text(last_ai) if last_ai else EMPTY_ANSWER


async def build_agent(
    config_path: str | Path, include: set[str] | None = None
) -> tuple[CompiledStateGraph, dict[str, str]]:
    tools, unavailable = await discover_tools(load_config(config_path, include))
    return build_graph(tools, build_llm(), unavailable=unavailable), unavailable

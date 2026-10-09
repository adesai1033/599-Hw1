# AGENTS.md
Read before touching any code.

## What this project is

CSCI 599 Assignment 1: a market-watcher agent. A user asks finance questions in natural
language ("why did NVDA drop today?", "compare TSLA and RIVN over the last month",
"what about six months instead?") and the agent reasons over the query, picks tools
exposed by three MCP servers, invokes them over the real MCP protocol, and synthesizes
an answer. It remembers the conversation per `session_id` and runs on Google Cloud Run. 
Every tool returns live data.

## Stack (fixed — do not substitute)

| Layer | Choice |
|---|---|
| LLM | OpenAI via `langchain-openai` (`ChatOpenAI`), key from `OPENAI_API_KEY` |
| Agent framework | LangGraph, **explicit** `StateGraph` |
| MCP client | `langchain-mcp-adapters` → `MultiServerMCPClient` |
| HTTP | FastAPI + uvicorn, `POST /chat` |
| Memory | `langgraph.checkpoint.memory.MemorySaver`, `thread_id = session_id` |
| MCP server framework (ours) | FastMCP (`mcp` Python SDK 1.x), stdio transport |
| MCP transports | stdio (market_data, fred), streamable HTTP (tavily) |
| Deployment | Docker (multi-stage, non-root) → Cloud Run, `us-west1`, `--max-instances 1` |

## MCP servers

1. **`market_data`** (ours, `mcp_servers/market_data.py`) — Finnhub for quotes, profiles and
   news; Twelve Data for price history, with `yfinance` as fallback.
   Tools: `get_quote(symbol)`, `get_price_history(symbol, period)`,
   `compare_performance(symbols, period)`, `get_company_profile(symbol)`,
   `get_company_news(symbol, days)`. `period ∈ {1w, 1m, 3m, 6m, 1y}`. Keys from
   `FINNHUB_API_KEY` and `TWELVEDATA_API_KEY`. In-process TTL cache (5 min). Every
   upstream call goes through one `_fetch_*` function so a source can be swapped without
   touching tool signatures.
2. **`fred`** (ours, `mcp_servers/fred.py`) — wraps the St. Louis Fed FRED API.
   Tools: `get_series(series_id, period)`, `search_series(query)`, `get_macro_snapshot()`. Key from `FRED_API_KEY`.
3. **`tavily`** (external, official hosted Tavily MCP server) — web/news search. Reached
   over streamable HTTP at `https://mcp.tavily.com/mcp/` with a bearer header built from
   `TAVILY_API_KEY`. Only `tavily_search` and `tavily_extract` are exposed to the agent
   (per-server `tools` allowlist in `mcp_config.json`). We do not modify it; we cite it
   in the README.

Server manifests live in `mcp_config.json`. The path is passed to the app via
`MCP_SERVERS_CONFIG`.

## Repository layout

```
main.py              FastAPI app, /chat endpoint, reads PORT (default 8083), binds 0.0.0.0
agent.py             LangGraph graph: agent node, tools node, conditional edge, MemorySaver
mcp_client.py        MultiServerMCPClient setup, tool discovery, transport-failure handling
mcp_servers/
  market_data.py     MCP server (Finnhub, Twelve Data, yfinance)
  fred.py            MCP server (FRED)
mcp_config.json      three server manifests (command/args/env per server)
tests/               pytest; see "Verification" below
Dockerfile           multi-stage, python:3.13-slim, non-root appuser
deploy.sh            gcloud builds submit + gcloud run deploy
.env.example         placeholder keys only — never real values
README.md            setup / run / deploy / cost disclosure / three architecture diagrams
PROCESS_LOG.md       chronological dev log in the author's own voice
```

## Architecture (what the graph looks like)

```
START → agent ──(tool_calls?)──► tools ──► agent  ...  ──(no tool_calls)──► END
```

- `agent` node: `llm.bind_tools(tools).invoke(state["messages"])`
- `tools` node: `ToolNode(tools, handle_tool_errors=True)`
- edge: `tools_condition` (or an equivalent four-line function)
- state: `MessagesState`
- compile with `checkpointer=MemorySaver()`

The loop-back from `tools` to `agent` is load-bearing. Do not flatten it into a single
tool call followed by a hard-coded answer.

## Hard rules

Do not violate them to "make tests pass."

1. **Real MCP, always.** Tool discovery and invocation go through `MultiServerMCPClient`
   (`tools/list`, `tools/call`). Never stub, mock, or hard-code tool *results* in
   application code. Mocks are allowed only inside `tests/`.
2. **No secrets in the repo.** API keys come from environment variables. `.env` is
   gitignored. `.env.example` has placeholders. Deployment passes keys via
   `--set-env-vars` from shell variables, never literals in `deploy.sh`.
3. **Framework memory only.** Memory is `MemorySaver` keyed by `thread_id`. Different
   `session_id` values never share history.
4. **Semantic tool selection.** The LLM chooses tools from their schemas. No keyword
   routing, no regex on the query to pick a tool, no forced tool calls. Questions that
   need no tool ("what's a P/E ratio?") must be answered from model knowledge.
5. **Never crash on MCP failure.** The three required failure modes must each produce a
   graceful user-facing answer and a 200 response, not a 500:
   - transport: a server in `mcp_config.json` won't start or disconnects mid-call
   - tool error: valid call, server-side failure (bad ticker, upstream 4xx/5xx)
   - malformed response: upstream returns something unparseable or off-schema
     (Finnhub returns HTTP 200 with `{"Information": ...}` when rate-limited —
     treat that as malformed and surface it cleanly)
   When a server fails discovery, its `description` from `mcp_config.json` is appended to
   the system message so the model can say the data is unavailable instead of offering to
   fetch it.
6. **Validate inside our servers.** `market_data` and `fred` check upstream payloads
   before returning; they raise a clear tool error rather than passing garbage to the LLM.
7. **Read `PORT` from the environment.** Never hard-code 8080 in `main.py`.
8. **No dead code, no debug prints.** Remove unused imports and leftover `print()` before
   committing. Use `logging`.

## Allowed (do freely)
- Read any file
- Run tests
- Git operations: status, diff, log, branch, commit

### Ask first
- Any `rm` or `delete` command → use `trash` instead
- Installing new dependencies
- Any git push or PR creation
- Environment variable changes

### Never (hard block)
- `rm -rf` anything
- Expose secrets in code or commits
- Push to main branch directly without my approval
- Run commands with `sudo`



## Conventions

- Python 3.13, type hints on public functions, `async` end-to-end in the request path.
- Tool docstrings are the LLM's only description of a tool — write them for the model:
  what it returns, valid argument values, when to use it vs. a sibling tool.
- JSON Schemas for our tools come from the MCP SDK's type inference. Keep argument types
  simple (`str`, `list[str]`, `Literal[...]`) so the LLM gets shapes right.
- Keep each MCP server a single file, runnable standalone with
  `python mcp_servers/<name>.py` (stdio).
- Dependencies pinned in `requirements.txt`. No `pip install --upgrade` in the Dockerfile.
- Commit messages: imperative mood, one line, say *why* when non-obvious.

## Verification

Minimum before any deploy:

- `pytest tests/` passes. Tests cover: graph loops back on tool calls; `thread_id`
  isolation; each of the three failure modes yields a graceful response; our two servers
  reject malformed upstream payloads.
- Run the canonical query set and eyeball tool-call traces (log at INFO which tool was
  called with which args):

  | Query | Expected behavior |
  |---|---|
  | "Why did NVDA drop today?" | `get_quote` → `get_price_history` → tavily search |
  | "Compare TSLA and RIVN over the last month" | `compare_performance` |
  | "What about six months instead?" | reuses prior tickers from memory |
  | "What does RIVN do, and how big is it?" | `get_company_profile` |
  | "Is that about the stock or about rates?" | adds a `fred` call |
  | "What's a P/E ratio?" | **no** tool call |
  | "What did I ask you first?" | real answer from memory |
  | "Quote for ZZZZQ" | tool error, graceful message |

- Probe transport failure by pointing one manifest at a nonexistent command and
  confirming the app still starts and the other servers still work.

## Documentation obligations

- `README.md`: setup, run, deploy, cost disclosure, attribution for the Tavily server,
  rationale for the `fred` server (the "additional MCP server" rubric item), and three
  diagrams (system architecture, tool-invocation loop with loop-back arrow, deployment
  topology with env-var flow). Diagrams must match this codebase, not framework docs.


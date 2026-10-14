# Market Watcher

CSCI 599 Assignment 1. A market-watcher agent: ask finance questions in natural language
("why did NVDA drop today?", "compare TSLA and RIVN over the last month", "what about six
months instead?") and a LangGraph agent reasons over the query, picks tools from three MCP
servers, invokes them over the real MCP protocol, and synthesizes an answer. Conversation
history is kept per `session_id`.

**Live URL:** https://csci599-a1-dvw5tijylq-uw.a.run.app
(`/docs` for Swagger UI, `/health` for server status, `POST /chat` for the agent.)

```bash
curl -X POST https://csci599-a1-dvw5tijylq-uw.a.run.app/chat \
  -H 'Content-Type: application/json' \
  -d '{"query": "Why did NVDA drop today?", "session_id": "demo1"}'
```

Response:

```json
{
  "response": "...",
  "tool_calls": [
    {"server": "market_data", "tool": "get_quote", "args": {"symbol": "NVDA"}, "status": "success"},
    {"server": "tavily", "tool": "tavily_search", "args": {"query": "..."}, "status": "success"}
  ]
}
```

`tool_calls` lists every MCP invocation the agent made for that turn, in order, with its
outcome. It is built from the graph's messages, so it reflects what actually went over the
wire. `response` and the request shape (`query`, `session_id`) follow the assignment
contract; `tool_calls` is additive.

## Setup

Requires Python 3.13 and API keys for OpenAI, Finnhub, Twelve Data, FRED, and Tavily. All
five data providers have free tiers that cover this assignment's traffic.

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then fill in real keys; .env is gitignored
```

| Variable | Used by |
|---|---|
| `OPENAI_API_KEY` | `agent.py` via `langchain-openai` |
| `OPENAI_MODEL` | optional, default `gpt-5-mini` |
| `FINNHUB_API_KEY`, `TWELVEDATA_API_KEY` | `market_data` server (passed through `mcp_config.json`) |
| `FRED_API_KEY` | `fred` server |
| `TAVILY_API_KEY` | bearer header for the hosted Tavily MCP server |
| `MCP_SERVERS_CONFIG` | path to the server manifests, default `mcp_config.json` |
| `PORT` | HTTP port, default `8080` |

Keys never appear in code or in `deploy.sh`; they are read from the environment at every layer.

## Run

```bash
set -a; source .env; set +a
python main.py                # binds 0.0.0.0:$PORT
```

The app starts even if an MCP server is down. `GET /health` shows which servers came up and
which tools were discovered.

Tests (131, no network needed except two discovery tests that spawn the stdio servers locally):

```bash
pytest tests/
```

Canonical query set as a Postman collection, runnable against local or Cloud Run:

```bash
postman collection run postman/collections/market-watcher.postman_collection.json \
  -e postman/environments/cloud-run.postman_environment.json --timeout-request 300000
```

Each MCP server also runs standalone over stdio for debugging with any MCP client:
`python mcp_servers/market_data.py`, `python mcp_servers/fred.py`.

## Deploy

```bash
set -a; source .env; set +a      # keys become shell variables
./deploy.sh                      # gcloud builds submit + gcloud run deploy
```

`deploy.sh` builds the image with Cloud Build, pushes it to `gcr.io/<project>/csci599-a1`
(served by Artifact Registry), and deploys Cloud Run service `csci599-a1` in `us-west1` with
`--memory 1Gi --cpu 1 --timeout 300 --min-instances 0 --max-instances 1`. Keys are passed
with `--set-env-vars` from the shell variables, so the script contains no literals. It prints
the service URL when done.

The Dockerfile is multi-stage on `python:3.13-slim`, installs pinned requirements in a
builder stage, and runs as a non-root `appuser`. No Node: the only external server (Tavily)
is reached over HTTP, not spawned via `npx`.

## Cost disclosure

| Component | Pricing model | Cost for this assignment |
|---|---|---|
| OpenAI `gpt-5-mini` | per token | The only metered cost. A canonical 8-query session uses on the order of 30-60k tokens, mostly tool results in the prompt. Total spend for development and grading runs was low single-digit dollars. Check your usage dashboard for exact figures. |
| Finnhub | free tier, rate-limited per minute | $0. Rate-limit responses are HTTP 200 with an `Information` body; the server treats these as malformed and reports them cleanly. |
| Twelve Data | free tier, daily credit cap | $0. When the cap is hit, `get_price_history` falls back to `yfinance`. |
| FRED | free with API key | $0 |
| Tavily | free tier, monthly credit cap | $0 at this volume. Each `tavily_search` consumes one or more credits. |
| Cloud Run | per vCPU-second and request, free tier applies | $0 at this volume. `--min-instances 0` means nothing is billed while idle; the trade-off is a cold start (roughly 10 s) on the first request after idle, during which the two stdio servers spawn and Tavily discovery completes. |
| Cloud Build + Artifact Registry | per build-minute, per GB stored | Within free tier for a handful of builds. |

A 5-minute in-process TTL cache in both of our servers keeps repeated questions in one
session from re-hitting upstream APIs.

## MCP servers

Three servers, declared in `mcp_config.json`. Two are ours (stdio, FastMCP from the `mcp`
Python SDK 1.x); one is external (streamable HTTP).

| Server | Transport | Tools | Purpose |
|---|---|---|---|
| `market_data` (ours, `mcp_servers/market_data.py`) | stdio | `get_quote`, `get_price_history`, `compare_performance`, `get_company_profile`, `get_company_news` | Stock data. Finnhub for quotes, profiles, and company news; Twelve Data for price history with `yfinance` as fallback. `period ∈ {1w, 1m, 3m, 6m, 1y}`. |
| `fred` (ours, `mcp_servers/fred.py`) | stdio | `get_series`, `search_series`, `get_macro_snapshot` | Macroeconomic backdrop from the St. Louis Fed FRED API: rates, inflation, unemployment, VIX. |
| `tavily` (external, hosted) | streamable HTTP | `tavily_search`, `tavily_extract` | Web and news search for the "why" behind a move. |

Discovery and invocation go through `langchain-mcp-adapters`' `MultiServerMCPClient`
(`tools/list` at startup, `tools/call` per invocation). Nothing in application code stubs or
hard-codes a tool result. The LLM selects tools from their JSON schemas alone; there is no
keyword routing, and questions that need no tool ("what's a P/E ratio?") produce no tool call.

Both of our servers validate every upstream payload before returning. Missing fields,
non-numeric prices, unparseable dates, and non-JSON bodies raise a clear `ToolError` rather
than passing garbage to the model.

### Tavily attribution

The third server is the official hosted Tavily MCP server at `https://mcp.tavily.com/mcp/`,
operated by Tavily (https://tavily.com, https://github.com/tavily-ai/tavily-mcp). We do not
modify it. We connect over streamable HTTP with a bearer token built from `TAVILY_API_KEY`,
and expose only `tavily_search` and `tavily_extract` to the agent via the per-server `tools`
allowlist in `mcp_config.json`.

### Why the `fred` server

`market_data` answers "what happened to this stock". It cannot answer "is that about the
stock or about rates?", which is the follow-up a real market watcher asks. FRED is the
canonical free source for US macro series, and wrapping it as a separate MCP server keeps the
split clean: one server per data domain, each with its own key, cache, and failure handling.
This is the additional server beyond the required two. It is exercised in the canonical
query set by the "stock or rates?" turn, which the model answers with a `get_macro_snapshot`
call alongside the earlier equity data.

## Failure handling

The three required failure modes each produce a graceful 200 response, never a 500.

| Mode | Mechanism | Test |
|---|---|---|
| Transport: server won't start | Discovery runs per server with a 30 s timeout. A failed server is skipped, `/health` reports it `down`, and its `description` from the manifest is appended to the system prompt so the model says the data is unavailable instead of offering to fetch it. | `test_server_unavailable_at_startup`, `test_dead_http_server_is_skipped_quickly` |
| Transport: disconnect mid-call | The SDK raises; `ToolNode(handle_tool_errors=True)` converts it to an error `ToolMessage`; the model explains and answers with what it has. The next session reconnects. | `test_server_dies_mid_call_and_next_session_self_heals` |
| Tool error | Our servers raise `ToolError`; the SDK returns it as an `isError` result; the same `ToolNode` path surfaces it to the model. | `test_tool_error_for_invalid_symbol`, `test_real_mcp_tool_error_becomes_graceful_graph_step`, plus per-server unit tests |
| Malformed response | Upstream to our servers: payload validation raises `ToolError` (including Finnhub's HTTP 200 rate-limit body). Server to agent: an off-schema MCP result is rejected by the adapter and becomes an error `ToolMessage`. | `test_quote_rate_limit_information_body_is_malformed`, `test_malformed_responses`, `test_off_schema_result_becomes_a_tool_error` |

Scope note: the mid-call disconnect and off-schema tests use purpose-built stdio test servers
in `tests/servers/`. For the hosted Tavily server we test the unreachable-at-startup case
directly; a mid-call HTTP failure takes the same `ToolNode` error path but is not exercised
by a dedicated test, since we cannot make the hosted server fail on demand.

## Memory

Memory is LangGraph's `MemorySaver` checkpointer, with `thread_id` set to the request's
`session_id`. The full message list (user turns, assistant turns, tool calls, tool results
from all three servers) is checkpointed after every node, so "what about six months instead?"
sees the earlier `compare_performance` call and reuses its tickers. Different `session_id`
values never share history.

The checkpointer is in-process. Memory lasts for the lifetime of the Cloud Run instance,
which the assignment specifies as sufficient; `--max-instances 1` keeps all requests on one
process. A redeploy or scale-to-zero clears it.

## Architecture diagrams

### System architecture

```
                 {query, session_id}
  User / curl / Postman ──────────────────────────────►  FastAPI  POST /chat, GET /health  (main.py)
                        ◄──────────────────────────────  │
                 {response, tool_calls}                  │ ainvoke(messages, thread_id=session_id)
                                                         ▼
  ┌───────────────────────────────── LangGraph StateGraph (agent.py) ─────────────────────────────────┐
  │                                                                                                   │
  │   agent node  ◄──── chat completion + tool schemas ────►  OpenAI gpt-5-mini (ChatOpenAI)          │
  │   llm.bind_tools(tools)                                                                           │
  │        │ tool_calls?                ▲ ToolMessages                                                │
  │        ▼                            │                                                             │
  │   tools node  ToolNode(handle_tool_errors=True)                                                   │
  │        │                                                                                          │
  │   checkpoint after every node  ◄──►  MemorySaver  (thread_id = session_id)                        │
  └────────│──────────────────────────────────────────────────────────────────────────────────────────┘
           │ tools/call
           ▼
  MCP client wrapper: MultiServerMCPClient (mcp_client.py)
  tools/list at startup, tools/call per invocation
           │
           ├── stdio ──────────────►  market_data server (mcp_servers/market_data.py) ──►  Finnhub, Twelve Data, yfinance
           │
           ├── stdio ──────────────►  fred server (mcp_servers/fred.py) ───────────────►  FRED API (St. Louis Fed)
           │
           └── streamable HTTP ────►  Tavily MCP server (hosted, mcp.tavily.com/mcp/)
               Bearer TAVILY_API_KEY   tools exposed: tavily_search, tavily_extract
```

Everything inside the box and the two stdio servers run in one Cloud Run container. OpenAI,
Tavily, and the data providers are external. Each server's tool results come back as
`ToolMessage`s in the same thread, so the model sees all three sources in one history.

### Tool-invocation loop

```
  User query + session_id
          │
          ▼
  START: load thread from MemorySaver, append HumanMessage
          │
          ▼
  agent node (LLM reasoning)  ◄──────────────────────────────────┐
  system prompt + history + tool schemas  →  AIMessage            │
          │                                                       │
          ▼                                                       │
  tools_condition: does the AIMessage have tool_calls?            │
          │                                                       │
    yes   │   no                                                  │  LOOP-BACK
   ┌──────┴──────┐                                                │  ToolMessage appended
   ▼             ▼                                                │  (success or error);
  tools node    END: checkpoint saved under thread_id             │  the LLM reasons again
  ToolNode →      │                                               │  and may call another
  MCP tools/call  ▼                                               │  tool or answer
   │          response + tool_calls trace                         │
   ▼                                                              │
  MCP server executes (market_data / fred / tavily)               │
  returns result or isError  ─────────────────────────────────────┘

  The loop runs until the LLM emits an answer with no tool_calls, or recursion_limit=25.
```

After each tool result the graph returns to the agent node. For "why did NVDA drop today?"
that is typically `get_quote` → `get_price_history` → `tavily_search` → final answer, three
trips around the loop. Hitting the recursion limit yields a graceful message, not an error.

### Deployment topology

```
  LOCAL DEVELOPMENT (macOS)                         GOOGLE CLOUD PROJECT
  ─────────────────────────                         ────────────────────

  .env (gitignored)                                 Cloud Build
    OPENAI_API_KEY                   ./deploy.sh      gcloud builds submit --tag
    FINNHUB_API_KEY       ┌─────────────────────────►   builds Dockerfile (multi-stage,
    TWELVEDATA_API_KEY    │                              python:3.13-slim, non-root)
    FRED_API_KEY          │                                       │ Docker image
    TAVILY_API_KEY        │                                       ▼
       │                  │                               Artifact Registry
       │ set -a;          │                               gcr.io/<project>/csci599-a1
       │ source .env      │                                       │ gcloud run deploy --image
       ▼                  │                                       ▼
  shell environment ──────┤                               Cloud Run service csci599-a1
       │                  │  --set-env-vars                 us-west1, 1Gi, 1 vCPU,
       │                  │  OPENAI_API_KEY=$OPENAI_API_KEY, timeout 300, max-instances 1,
       │                  └─ FINNHUB_API_KEY=$FINNHUB_... ►  PORT=8080, non-root appuser
       ▼                     (shell variables, no                  │
  python main.py              literals in deploy.sh)               ▼
  uvicorn on :8080                                        Live URL
  pytest tests/                                           https://csci599-a1-dvw5tijylq-uw.a.run.app
       │                                                          │
       └──────────────────────┬───────────────────────────────────┘
                              ▼
            Same backends from both environments:
            OpenAI API · Finnhub · Twelve Data · FRED · Tavily MCP (hosted)
```

Environment-variable flow: locally, `.env` is sourced into the shell and read by
`python main.py` and by the stdio servers it spawns. For deployment the same shell variables
are interpolated by `deploy.sh` into `--set-env-vars`, so Cloud Run injects them into the
container at runtime. `mcp_config.json` references them as `${FINNHUB_API_KEY}` and so on,
and `mcp_client.py` substitutes the values when building each server's connection. No key is
baked into the image. There is no separate local LLM: development and deployment both use
the OpenAI API, so behaviour is identical in both places.


## Repository layout

```
main.py              FastAPI app: /chat, /health, / → /docs; reads PORT
agent.py             LangGraph graph, system prompt, MemorySaver, tool-call trace
mcp_client.py        manifest loading, env substitution, MultiServerMCPClient discovery
mcp_servers/
  market_data.py     Finnhub / Twelve Data / yfinance MCP server
  fred.py            FRED MCP server
mcp_config.json      three server manifests
tests/               pytest (graph, servers, failure modes, HTTP contract)
postman/             canonical query collection + local and Cloud Run environments
Dockerfile           multi-stage, non-root
deploy.sh            Cloud Build + Cloud Run deploy
.env.example         placeholder keys
PROCESS_LOG.md       development log
```

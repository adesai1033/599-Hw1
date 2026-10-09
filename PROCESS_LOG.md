# Process Log

## 2026-10-07: Scaffold
- Scaffolded the repo from AGENTS.md: FastAPI/agent/MCP-client stubs, `mcp_config.json` with three servers, Dockerfile, `deploy.sh`, `.env.example`, `.gitignore`, empty test files. Pushed to `main`.
- Set up a Python 3.13 `.venv` (the system Python is 3.14) and installed `mcp`, `httpx`, `yfinance`, `pytest`, `pytest-asyncio`.

## 2026-10-08: `market_data` server
- Built `mcp_servers/market_data.py` with five tools: `get_quote`, `get_price_history`, `compare_performance`, `get_company_profile`, `get_company_news`. Finnhub for quotes, profiles and news; Twelve Data for history with a `yfinance` fallback.
- The installed `mcp` is 2.x, where `FastMCP` is now `MCPServer`, so I used that.
- 46 mocked tests pass, and the MCP Inspector lists the five tools. Checked the `yfinance` fallback with a live NVDA call.
- Updated AGENTS.md to match this design and added `TWELVEDATA_API_KEY` to `.env.example`.
- Fixed two scaffold bugs: `mcp_config.json` wasn't passing the Twelve Data key, and `/chat` used `message`/`answer` instead of the PDF's `query`/`response`.

## 2026-10-09: `fred` server
- Built `mcp_servers/fred.py` with `get_series`, `search_series` and `get_macro_snapshot`. It copies the scaffolding from `market_data.py` so each server stays standalone. Cache TTL is 1 hour.
- Added `tests/test_fred.py`. The Inspector lists the three tools.
- The snapshot now checks `FRED_API_KEY` before fetching, so a missing key reports "not configured" instead of "temporarily unavailable". Upstream failures now log at WARNING.
- Full suite: 82 tests pass.

## 2026-10-09: Agent graph (Stage 3, `market_data` only)
- Built `mcp_client.py` (config loading with `${VAR}` substitution, per-server tool discovery), `agent.py` (explicit `StateGraph`: agent -> tools -> agent loop, `MemorySaver`, `ask()`), and `scripts/repl.py`. Tavily and FRED are not wired in yet.
- **`mcp` 2.x -> 1.x:** installing `langchain-mcp-adapters` pulled `mcp` back to 1.30. Adapters 0.3.1 doesn't import against `mcp` 2.x either, so I ported both servers back to `FastMCP` and pinned `mcp==1.30.0`. This reverses my earlier 2.x choice.
- **Stateless MCP sessions:** each tool call spawns a fresh stdio subprocess. A server that dies mid-conversation heals on the next call. The cost is subprocess startup per call and losing the servers' in-process caches.
- **Live bug:** Finnhub returns `d` and `dp` as `null` (not `0`) for an unknown ticker, so my numeric check fired first and `ZZZZQ` reported "Malformed" instead of "No quote found". Fixed by checking not-found first, with a regression test.
- Added 16 graph tests, plus a `bind_tools` assertion after a review pointed out the suite would pass even if tools were never bound. I confirmed it by removing `bind_tools` and watching the test fail. Deleted the dead `tests/test_servers.py` stub. 99 tests pass.
- **First live tool-selection trace** (`gpt-5-mini`, real `market_data` server, one session):
```
quote for NVDA                    -> tool_call name=get_quote args={"symbol": "NVDA"}
what's a P/E ratio?               -> (no tool_call)
compare TSLA and RIVN ... month   -> tool_call name=compare_performance args={"symbols": ["TSLA", "RIVN"], "period": "1m"}
what about six months instead?    -> tool_call name=compare_performance args={"symbols": ["TSLA", "RIVN"], "period": "6m"}
quote for ZZZZQ                   -> tool_result name=get_quote status=error chars=153
```
  Memory worked: the six-month follow-up reused both tickers. The `ZZZZQ` run happened before the null-field fix above, so it quoted "Malformed response".

## 2026-10-09: FRED and Tavily wired in (Stage 4)
- Switched Tavily from the npm stdio server to the hosted **streamable HTTP** endpoint with a bearer header, so the image no longer needs Node. Added a per-server `tools` allowlist in `mcp_config.json` (a static capability choice, not query routing) so the agent only sees `tavily_search` and `tavily_extract`. Added three paragraphs to the system prompt on choosing between news tools, when to use macro data, and naming web sources.
- **Tavily tool names:** the hosted server uses underscores (`tavily_search`), not the hyphens from the npm server. With hyphens the allowlist matched nothing and the first live run silently had no web search. The allowlist warning made it easy to spot.
- Added a `network` pytest marker; 105 tests pass (104 offline).
- **Live ten-query run, all three servers, 10 tools:** all ten queries matched the selection table on the second run. Highlights: `compare_performance` reused TSLA/RIVN for the six-month follow-up, "stock or rates?" called `get_macro_snapshot`, "10-year yield this year" called `get_series(DGS10, "1y")`, "big market news" called `tavily_search` and cited sources, and the P/E and "what did I ask first" queries made no tool call.
- **Variance:** the first run reused an earlier macro snapshot for the "rates" question and asked a clarifying question for the yield query. Tool selection is not fully deterministic.
- **Failure probes:** an invalid Tavily key is rejected at the handshake and the other servers keep working. With `fred` pointed at a missing command, startup logs one ERROR and 7 tools remain. In both cases the model invented no numbers, but it never said the data was unavailable and offered to fetch things it had no tool for, because it doesn't know a server is down.

## 2026-10-09: Telling the model what's down (Stage 4b)
- Each server in `mcp_config.json` now has a `description`. When discovery fails, `discover_tools` returns `{server: description}` and the system message gets one appended sentence listing what's unavailable. Before this, the model offered to fetch data it had no tool for.
- First wording ("say plainly that the data source is unavailable") was ignored: the model filled the gap with web search and never mentioned the outage. Rewording to "begin your answer by saying plainly that this data is unavailable" worked in the live probe with `fred` broken. Prompt wording matters more than adding the information.

## 2026-10-09: HTTP layer and failure-mode tests (Stage 5)
- `main.py`: `create_app(config_path, llm)` factory, `POST /chat` with the PDF contract (`query`/`session_id` in, `response` out), `GET /health`, `PORT` default 8080. A missing `OPENAI_API_KEY` fails startup loudly; MCP failures never do.
- `/chat` always returns 200 for runtime trouble: a timeout or any other exception gives a graceful sentence, and the traceback goes to the log. Bad request bodies stay 422.
- Added two deliberately misbehaving test servers (one that exits mid-call and prints garbage, one that returns an off-schema result). The three rubric failure modes (transport, tool error, malformed response) are now proven through the real HTTP stack with a scripted LLM. 125 tests pass.
- `build_agent` now also returns the tool names so `/health` can list them.
- The adapter's error text for the off-schema case differed from what I expected (`ToolInvocationError ... Input should be a valid list`), so I asserted on the real wording.
- **Live smoke test** (real keys): `/health` shows 3 servers up and 10 tools; the six-month follow-up reused TSLA and RIVN from session memory; `ZZZZQ` gave a graceful sentence with HTTP 200; a missing `session_id` gave 422.

## Prompting: what worked vs. what didn't
**Worked**
- Staged prompts, one file each, with AGENTS.md pasted first. The model stayed in scope and `market_data.py` served as the template for `fred.py`.
- Exact output keys, verbatim tool docstrings, and a numbered "done when" list that mapped almost one-to-one onto tests.
- A "run `pip show` first, use what's installed" instruction. It caught the `mcp` 2.x rename before any code was written.
- Asking for a list of deviations at the end, which surfaced every judgment call.
- A separate review pass, which caught real bugs: the missing Twelve Data key in `mcp_config.json` and the wrong `/chat` field names.

**Didn't work**
- My prompts assumed the wrong library API (`FastMCP`) and a different design from AGENTS.md (four tools vs. five, Finnhub-only history). I had to reconcile them mid-task and update AGENTS.md afterwards.
- The specs contradicted themselves in places: dropping bad closes vs. calling them malformed, and "exactly one WARNING" vs. "log every failure". These got resolved by guesswork until I changed the code.
- Soft limits like "~350 lines" were exceeded (415 and 329) without trouble, so they weren't real constraints.
- The scaffold prompt never pointed at the assignment PDF, so the `/chat` contract was wrong until a later review.
- Bare "push" left the commit message unspecified. It's better to say the message up front.

## Still to do
- Live-test FRED and Twelve Data on their own (FRED history ran through the agent, not standalone).
- Dockerfile (Python-only, non-root), `deploy.sh` run, README with diagrams, cost disclosure and Tavily attribution.
- Deploy to Cloud Run and re-run the smoke test against the public URL.

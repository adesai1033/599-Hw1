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
- Live-test FRED and Twelve Data with real keys (not done; the `.env` keys are set).
- Stage 4: wire in FRED and Tavily, then re-run the canonical queries ("stock or rates?" should add a FRED call).
- `main.py` with the `/chat` endpoint, the failure-mode tests (`tests/test_failures.py`), README, deploy.

"""market_data MCP server: tools, validation, fallback, cache. No test touches the network."""
import json
import logging
import types
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_servers import market_data as md

pytestmark = pytest.mark.asyncio

QUOTE = {"c": 133.374, "d": -2.14, "dp": -1.579, "h": 136.02, "l": 132.8, "o": 135.1, "pc": 135.51, "t": 1790000000}
HISTORY_KEYS = {
    "symbol", "period", "source", "start_date", "end_date", "points", "point_count",
    "first_close", "last_close", "period_return_pct", "period_high", "period_low",
}


class Upstream:
    """Routes mocked HTTP by the last URL path segment and records every request."""

    def __init__(self) -> None:
        self.routes = {}
        self.requests = []

    def on(self, name, json=None, status=200, exc=None, fn=None):
        def respond(request):
            if exc:
                raise exc
            return fn(request) if fn else httpx.Response(status, json=json)
        self.routes[name] = respond

    def handle(self, request):
        self.requests.append(request)
        return self.routes[request.url.path.rsplit("/", 1)[-1]](request)


class FakeYFinance:
    """State shared with the patched yfinance.Ticker: canned rows, an optional error, a call count."""

    def __init__(self) -> None:
        self.rows = []
        self.error = None
        self.calls = 0


class Frame:
    def __init__(self, rows):
        self.empty = not rows
        self._rows = rows

    def __getitem__(self, column):
        return types.SimpleNamespace(items=lambda: [(datetime.fromisoformat(d), c) for d, c in self._rows])


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fh-secret")
    monkeypatch.setenv("TWELVEDATA_API_KEY", "td-secret")
    monkeypatch.setattr(md, "_cache", {})
    monkeypatch.setattr(md, "_warned_missing_twelvedata_key", False)


@pytest.fixture
def up(monkeypatch):
    upstream = Upstream()
    monkeypatch.setattr(md, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)))
    return upstream


@pytest.fixture
def yf(monkeypatch):
    fake = FakeYFinance()

    class Ticker:
        def __init__(self, symbol):
            pass

        def history(self, **kwargs):
            fake.calls += 1
            if fake.error:
                raise fake.error
            return Frame(fake.rows)

    monkeypatch.setattr(md.yfinance, "Ticker", Ticker)
    return fake


def td_body(closes, first_day="2026-09-01"):
    """Twelve Data body, newest first, one row per calendar day."""
    start = date.fromisoformat(first_day)
    rows = [{"datetime": (start + timedelta(days=i)).isoformat(), "close": str(c)} for i, c in enumerate(closes)]
    return {"meta": {}, "values": rows[::-1], "status": "ok"}


def weekdays(count, first=date(2025, 10, 13)):
    days, day = [], first
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "market_data"]


async def test_quote_shape_and_rounding(up):
    up.on("quote", QUOTE)
    result = await md.get_quote("NVDA")
    assert result == {
        "symbol": "NVDA", "price": 133.37, "change": -2.14, "change_pct": -1.58, "open": 135.1,
        "high": 136.02, "low": 132.8, "previous_close": 135.51,
        "as_of": datetime.fromtimestamp(1790000000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    assert up.requests[0].url.params["token"] == "fh-secret"


async def test_quote_without_timestamp_is_estimated(up):
    up.on("quote", {**QUOTE, "t": 0})
    result = await md.get_quote("NVDA")
    assert result["as_of_estimated"] is True and result["as_of"].endswith("Z")


async def test_quote_normalizes_symbol(up):
    up.on("quote", QUOTE)
    assert await md.get_quote("nvda ") == await md.get_quote("NVDA")
    assert len(up.requests) == 1
    assert up.requests[0].url.params["symbol"] == "NVDA"


async def test_quote_unknown_symbol(up):
    up.on("quote", {k: 0 for k in QUOTE})
    with pytest.raises(ToolError, match="No quote found"):
        await md.get_quote("ZZZZQ")


async def test_quote_unknown_symbol_with_null_change_fields(up):
    up.on("quote", {"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0})
    with pytest.raises(ToolError, match="No quote found"):
        await md.get_quote("ZZZZQ")


@pytest.mark.parametrize("bad", ["N V DA", "", "TOOLONGX", "NV1", "BRK.TOOLONG"])
async def test_invalid_symbol_makes_no_request(up, bad):
    with pytest.raises(ToolError, match="Invalid symbol"):
        await md.get_quote(bad)
    assert up.requests == []


async def test_quote_missing_field_is_malformed(up):
    up.on("quote", {k: v for k, v in QUOTE.items() if k != "pc"})
    with pytest.raises(ToolError, match="Malformed response"):
        await md.get_quote("NVDA")


async def test_quote_rate_limit_information_body_is_malformed(up):
    up.on("quote", {"Information": "rate limited"})
    with pytest.raises(ToolError, match="Malformed response"):
        await md.get_quote("NVDA")


@pytest.mark.parametrize(
    "status, message",
    [(429, "rate limit"), (503, "temporarily unavailable"), (401, "access denied")],
)
async def test_finnhub_http_errors(up, status, message):
    up.on("quote", {}, status=status)
    with pytest.raises(ToolError, match=message):
        await md.get_quote("NVDA")


async def test_finnhub_connection_error(up):
    up.on("quote", exc=httpx.ConnectError("boom"))
    with pytest.raises(ToolError, match="temporarily unavailable"):
        await md.get_quote("NVDA")


async def test_history_computed_fields(up):
    up.on("time_series", td_body([100, 110, 105, 120]))
    result = await md.get_price_history("NVDA", "1m")
    assert set(result) == HISTORY_KEYS
    assert result["source"] == "twelvedata"
    assert [p["close"] for p in result["points"]] == [100, 110, 105, 120]
    assert [p["date"] for p in result["points"]] == sorted(p["date"] for p in result["points"])
    assert result["period_return_pct"] == round((120 - 100) / 100 * 100, 2)
    assert result["period_high"] == {"date": "2026-09-04", "close": 120}
    assert result["period_low"] == {"date": "2026-09-01", "close": 100}
    assert result["point_count"] == 4
    assert up.requests[0].url.params["apikey"] == "td-secret"


async def test_history_1y_is_weekly(up):
    days = weekdays(250)
    up.on("time_series", {"values": [{"datetime": d.isoformat(), "close": "100.5"} for d in reversed(days)], "status": "ok"})
    result = await md.get_price_history("NVDA", "1y")
    assert len(result["points"]) <= 53 and result["point_count"] == len(result["points"])
    for point in result["points"]:
        day = date.fromisoformat(point["date"])
        assert [d for d in days if d > day and d.isocalendar()[:2] == day.isocalendar()[:2]] == []


@pytest.mark.parametrize(
    "fail",
    [
        {"json": {}, "status": 429},
        {"json": {"code": 429, "message": "limit", "status": "error"}},
        {"json": {}, "status": 500},
        {"exc": httpx.ReadTimeout("slow")},
    ],
)
async def test_history_falls_back_to_yfinance(up, yf, caplog, fail):
    caplog.set_level(logging.DEBUG, logger="market_data")
    up.on("time_series", **fail)
    yf.rows = [("2026-09-01", 100.0), ("2026-09-02", 110.0)]
    result = await md.get_price_history("NVDA", "1m")
    assert result["source"] == "yfinance" and set(result) == HISTORY_KEYS
    assert result["period_return_pct"] == 10.0
    assert len(warnings(caplog)) == 1
    assert "td-secret" not in caplog.text


@pytest.mark.parametrize("fail", [{"json": {}, "status": 404}, {"json": {"code": 400, "status": "error"}}])
async def test_history_provider_rejection_does_not_fall_back(up, yf, fail):
    up.on("time_series", **fail)
    with pytest.raises(ToolError, match="rejected the request"):
        await md.get_price_history("NVDA", "1m")
    assert yf.calls == 0


async def test_history_malformed_close(up):
    up.on("time_series", {"values": [{"datetime": "2026-10-01", "close": "abc"}], "status": "ok"})
    with pytest.raises(ToolError, match="Malformed response"):
        await md.get_price_history("NVDA", "1m")


async def test_history_malformed_date(up):
    up.on("time_series", {"values": [{"datetime": "yesterday", "close": "1"}], "status": "ok"})
    with pytest.raises(ToolError, match="Malformed response"):
        await md.get_price_history("NVDA", "1m")


async def test_history_missing_values_is_malformed(up):
    up.on("time_series", {"meta": {}, "status": "ok"})
    with pytest.raises(ToolError, match="Malformed response"):
        await md.get_price_history("NVDA", "1m")


async def test_history_empty_values_is_not_found(up, yf):
    up.on("time_series", {"values": [], "status": "ok"})
    with pytest.raises(ToolError, match="No price history found"):
        await md.get_price_history("ZZZZQ", "1m")
    assert yf.calls == 0


async def test_history_single_point_is_insufficient(up):
    up.on("time_series", td_body([100]))
    with pytest.raises(ToolError, match="got 1 points; need at least 2"):
        await md.get_price_history("NVDA", "1m")


async def test_missing_twelvedata_key_goes_straight_to_yfinance(up, yf, monkeypatch, caplog):
    monkeypatch.delenv("TWELVEDATA_API_KEY")
    yf.rows = [("2026-09-01", 100.0), ("2026-09-02", 90.0)]
    first = await md.get_price_history("NVDA", "1m")
    second = await md.get_price_history("AAPL", "1m")
    assert first["source"] == second["source"] == "yfinance"
    assert up.requests == []
    assert len(warnings(caplog)) == 1


async def test_both_history_sources_failing(up, yf):
    up.on("time_series", {}, status=429)
    yf.error = RuntimeError("yahoo down")
    with pytest.raises(ToolError, match="temporarily unavailable"):
        await md.get_price_history("NVDA", "1m")


async def test_history_yfinance_empty_frame_is_not_found(up, yf):
    up.on("time_series", {}, status=429)
    with pytest.raises(ToolError, match="No price history found"):
        await md.get_price_history("NVDA", "1m")


def by_symbol(bodies):
    return lambda request: httpx.Response(200, json=bodies[request.url.params["symbol"]])


async def test_compare_ranks_and_dedupes(up):
    up.on("time_series", fn=by_symbol({
        "TSLA": td_body([250.10, 262.40]), "RIVN": td_body([12.80, 11.95]),
    }))
    result = await md.compare_performance(["TSLA", "rivn", "TSLA"], "1m")
    assert [r["symbol"] for r in result["results"]] == ["TSLA", "RIVN"]
    assert result["ranking"] == ["TSLA", "RIVN"]
    assert result["best"] == {"symbol": "TSLA", "period_return_pct": 4.92}
    assert result["worst"] == {"symbol": "RIVN", "period_return_pct": -6.64}
    assert result["spread_pct"] == 11.56
    assert "points" not in json.dumps(result)
    assert len(up.requests) == 2


async def test_compare_ranking_descending_when_caller_order_differs(up):
    up.on("time_series", fn=by_symbol({"AAA": td_body([10, 9]), "BBB": td_body([10, 12])}))
    result = await md.compare_performance(["AAA", "BBB"], "1m")
    assert result["ranking"] == ["BBB", "AAA"]
    assert [r["symbol"] for r in result["results"]] == ["AAA", "BBB"]


async def test_compare_symbol_count_limits(up):
    with pytest.raises(ToolError, match="at least 2"):
        await md.compare_performance(["TSLA", "tsla"], "1m")
    with pytest.raises(ToolError, match="at most 5"):
        await md.compare_performance(["A", "B", "C", "D", "E", "F"], "1m")
    with pytest.raises(ToolError, match="Invalid symbol"):
        await md.compare_performance(["TSLA", "1234"], "1m")
    assert up.requests == []


async def test_compare_fails_whole_when_one_symbol_fails(up):
    up.on("time_series", fn=by_symbol({"TSLA": td_body([1, 2]), "ZZZZQ": {"values": [], "status": "ok"}}))
    with pytest.raises(ToolError, match="ZZZZQ"):
        await md.compare_performance(["TSLA", "ZZZZQ"], "1m")


async def test_compare_reuses_history_cache(up):
    up.on("time_series", fn=by_symbol({"TSLA": td_body([1, 2]), "RIVN": td_body([1, 2])}))
    await md.get_price_history("TSLA", "1m")
    await md.compare_performance(["TSLA", "RIVN"], "1m")
    assert len(up.requests) == 2


PROFILE = {"name": "Rivian Automotive Inc", "exchange": "NASDAQ NMS - GLOBAL MARKET", "finnhubIndustry": "Automobiles",
           "marketCapitalization": 13420, "ipo": "2021-11-10", "weburl": "https://rivian.com", "country": "US",
           "logo": "x", "shareOutstanding": 1}


async def test_profile(up):
    up.on("profile2", PROFILE)
    assert await md.get_company_profile("RIVN") == {
        "symbol": "RIVN", "name": "Rivian Automotive Inc", "exchange": "NASDAQ NMS - GLOBAL MARKET",
        "industry": "Automobiles", "market_cap_usd": 13_420_000_000, "market_cap_label": "$13.4B",
        "ipo_date": "2021-11-10", "country": "US", "website": "https://rivian.com",
    }


async def test_profile_optional_fields_become_null(up):
    up.on("profile2", {"name": "Acme", "marketCapitalization": 2_500_000})
    result = await md.get_company_profile("ACME")
    assert result["market_cap_label"] == "$2.5T"
    assert result["ipo_date"] is None and result["website"] is None and result["industry"] is None


async def test_profile_unknown_and_malformed(up):
    up.on("profile2", {})
    with pytest.raises(ToolError, match="No company profile found"):
        await md.get_company_profile("ZZZZQ")
    up.on("profile2", {"name": "Acme"})
    with pytest.raises(ToolError, match="Malformed response"):
        await md.get_company_profile("ACME")


def article(i):
    return {"headline": f"h{i}", "source": "Reuters", "datetime": 1790000000 + i, "url": f"https://x/{i}",
            "summary": "s", "image": "i", "id": i, "related": "NVDA"}


async def test_news_limits_and_orders(up):
    up.on("company-news", [article(i) for i in range(15)] + [{"headline": "no url"}])
    result = await md.get_company_news("NVDA", 7)
    assert result["article_count"] == 10
    assert [a["headline"] for a in result["articles"]] == [f"h{i}" for i in range(14, 4, -1)]
    assert set(result["articles"][0]) == {"headline", "source", "published_at", "url"}
    assert up.requests[0].url.params["from"] == (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()


async def test_news_empty_is_not_an_error(up):
    up.on("company-news", [])
    result = await md.get_company_news("NVDA")
    assert result["article_count"] == 0 and result["articles"] == []


async def test_news_rejects_bad_days_and_bad_body(up):
    for days in (0, 31):
        with pytest.raises(ToolError, match="days must be between 1 and 30"):
            await md.get_company_news("NVDA", days)
    assert up.requests == []
    up.on("company-news", {"Information": "limit"})
    with pytest.raises(ToolError, match="Malformed response"):
        await md.get_company_news("NVDA")


async def test_results_are_cached_and_errors_are_not(up):
    up.on("quote", QUOTE)
    await md.get_quote("NVDA")
    await md.get_quote("NVDA")
    assert len(up.requests) == 1
    up.on("quote", {k: 0 for k in QUOTE})
    with pytest.raises(ToolError):
        await md.get_quote("ZZZZQ")
    up.on("quote", QUOTE)
    await md.get_quote("ZZZZQ")
    assert len(up.requests) == 3


async def test_cache_expires(up, monkeypatch):
    up.on("quote", QUOTE)
    now = [1000.0]
    monkeypatch.setattr(md, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    await md.get_quote("NVDA")
    now[0] += md.CACHE_TTL_SECONDS - 1
    await md.get_quote("NVDA")
    assert len(up.requests) == 1
    now[0] += 2
    await md.get_quote("NVDA")
    assert len(up.requests) == 2


async def test_missing_finnhub_key(up, monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY")
    with pytest.raises(ToolError, match="not configured") as excinfo:
        await md.get_quote("NVDA")
    assert "FINNHUB" not in str(excinfo.value)
    assert up.requests == []


async def test_server_registers_exactly_five_tools():
    tools = {t.name: t for t in await md.mcp.list_tools()}
    assert set(tools) == {"get_quote", "get_price_history", "compare_performance", "get_company_profile", "get_company_news"}
    for name in ("get_price_history", "compare_performance"):
        assert tools[name].inputSchema["properties"]["period"]["enum"] == ["1w", "1m", "3m", "6m", "1y"]

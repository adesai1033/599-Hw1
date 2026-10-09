"""fred MCP server: tools, validation, error classification, cache. No test touches the network."""
import logging
import types
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_servers import fred

pytestmark = pytest.mark.asyncio

SERIES_KEYS = {
    "series_id", "title", "units", "frequency", "seasonal_adjustment", "last_updated", "period",
    "start_date", "end_date", "sampling", "observations", "observation_count", "earliest", "latest",
    "change", "change_pct", "period_high", "period_low",
}
META = {"seriess": [{
    "id": "DGS10", "title": "10-Year Treasury", "units": "Percent", "frequency": "Daily",
    "seasonal_adjustment": "Not Seasonally Adjusted", "last_updated": "2026-10-07 15:21:03-05",
}]}
SNAPSHOT_IDS = ["DFF", "DGS2", "DGS10", "T10Y2Y", "CPIAUCSL", "UNRATE", "VIXCLS"]


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


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "fred-secret")
    monkeypatch.setattr(fred, "_cache", {})


@pytest.fixture
def up(monkeypatch):
    upstream = Upstream()
    monkeypatch.setattr(fred, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)))
    return upstream


def obs(*pairs):
    return {"observations": [{"date": d, "value": v} for d, v in pairs]}


def daily(count, first=date(2019, 1, 1), value="1.5"):
    return [((first + timedelta(days=i)).isoformat(), value) for i in range(count)]


def warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "fred"]


def series_routes(up, observations, meta=META):
    up.on("series", meta)
    up.on("observations", observations)


async def test_get_series_computes_fields(up):
    days = daily(40, date(2026, 8, 1))
    values = [f"{4 + 0.01 * i:.2f}" for i in range(40)]
    values[15], values[25] = "5.00", "3.50"
    for i in (5, 10, 20):
        values[i] = "."
    series_routes(up, obs(*zip([d for d, _ in days], values)))
    result = await fred.get_series("DGS10", "1y")
    assert set(result) == SERIES_KEYS
    assert result["observation_count"] == 37 and result["sampling"] == "as_reported"
    dates = [o["date"] for o in result["observations"]]
    assert dates == sorted(dates)
    assert days[5][0] not in dates and days[10][0] not in dates and days[20][0] not in dates
    assert result["earliest"] == {"date": days[0][0], "value": 4.0}
    assert result["latest"] == {"date": days[39][0], "value": 4.39}
    assert result["change"] == 0.39 and result["change_pct"] == 9.75
    assert result["period_high"] == {"date": days[15][0], "value": 5.0}
    assert result["period_low"] == {"date": days[25][0], "value": 3.5}
    assert result["title"] == "10-Year Treasury" and result["units"] == "Percent"
    assert len(up.requests) == 2
    for request in up.requests:
        assert request.url.params["api_key"] == "fred-secret" and request.url.params["file_type"] == "json"
    observation_request = up.requests[1].url.params
    assert observation_request["sort_order"] == "asc"
    assert observation_request["observation_start"] == (datetime.now(timezone.utc).date() - timedelta(days=365)).isoformat()


async def test_get_series_normalizes_id_and_caches(up):
    series_routes(up, obs(("2026-09-01", "4.0"), ("2026-09-02", "4.1")))
    first = await fred.get_series("DGS10", "1y")
    assert await fred.get_series(" dgs10 ", "1y") == first
    assert len(up.requests) == 2
    assert up.requests[0].url.params["series_id"] == "DGS10"


@pytest.mark.parametrize("bad", ["DGS 10", "", "dgs-10", "A" * 41])
async def test_invalid_series_id_makes_no_request(up, bad):
    with pytest.raises(ToolError, match="Invalid series id"):
        await fred.get_series(bad, "1y")
    assert up.requests == []


async def test_unknown_series(up):
    up.on("series", {"error_code": 400, "error_message": "Bad Request.  The series does not exist."}, status=400)
    with pytest.raises(ToolError, match="No FRED series found with id NOPE"):
        await fred.get_series("NOPE", "1y")
    assert [r.url.path for r in up.requests] == ["/fred/series"]


async def test_bad_api_key_is_rejected_without_echoing_detail(up):
    up.on("series", {"error_code": 400, "error_message": "Bad Request.  The value for variable api_key is not registered."}, status=400)
    with pytest.raises(ToolError, match="rejected the request") as excinfo:
        await fred.get_series("DGS10", "1y")
    assert "api_key" not in str(excinfo.value) and "fred-secret" not in str(excinfo.value)


@pytest.mark.parametrize(
    "fail, message",
    [
        ({"json": {}, "status": 429}, "rate limit"),
        ({"json": {}, "status": 503}, "temporarily unavailable"),
        ({"exc": httpx.ConnectError("boom")}, "temporarily unavailable"),
    ],
)
async def test_transient_errors(up, fail, message):
    up.on("series", **fail)
    with pytest.raises(ToolError, match=message):
        await fred.get_series("DGS10", "1y")


@pytest.mark.parametrize(
    "meta, observations",
    [
        ({"seriess": []}, obs(("2026-09-01", "1"))),
        (META, {"count": 0}),
        (META, obs(("2026-09-01", "1"), ("2026-09-02", "abc"))),
        (META, obs(("last week", "1"), ("2026-09-02", "2"))),
        (META, obs(("2026-09-01", "1"), ("2026-09-02", "nan"))),
    ],
)
async def test_malformed_responses(up, meta, observations):
    series_routes(up, observations, meta)
    with pytest.raises(ToolError, match="Malformed response from fred for DGS10"):
        await fred.get_series("DGS10", "1y")


async def test_no_valid_observations(up):
    series_routes(up, obs(("2026-09-01", "."), ("2026-09-02", ".")))
    with pytest.raises(ToolError, match="No observations found for DGS10 over 1y"):
        await fred.get_series("DGS10", "1y")


async def test_single_observation_is_insufficient(up):
    series_routes(up, obs(("2026-09-01", "."), ("2026-09-02", "4.1")))
    with pytest.raises(ToolError, match="got 1 observations; need at least 2"):
        await fred.get_series("DGS10", "1y")


async def test_change_pct_is_null_when_earliest_is_zero(up):
    series_routes(up, obs(("2026-09-01", "0"), ("2026-09-02", "1.5")))
    result = await fred.get_series("DGS10", "1y")
    assert result["change"] == 1.5 and result["change_pct"] is None


async def test_sampling_monthly(up):
    rows = daily(2500)
    series_routes(up, obs(*rows))
    result = await fred.get_series("DGS10", "10y")
    assert result["sampling"] == "monthly" and len(result["observations"]) <= 260
    expected = list({d[:7]: d for d, _ in rows}.values())
    assert [o["date"] for o in result["observations"]] == expected


async def test_sampling_weekly(up):
    rows = daily(200)
    series_routes(up, obs(*rows))
    result = await fred.get_series("DGS10", "1y")
    assert result["sampling"] == "weekly"
    week = lambda d: date.fromisoformat(d).isocalendar()[:2]
    expected = list({week(d): d for d, _ in rows}.values())
    assert [o["date"] for o in result["observations"]] == expected


async def test_sampling_as_reported(up):
    series_routes(up, obs(*daily(60)))
    result = await fred.get_series("DGS10", "1y")
    assert result["sampling"] == "as_reported" and result["observation_count"] == 60


async def test_search_series(up):
    up.on("search", {"count": 3, "seriess": [
        {"id": "DGS10", "title": "10-Year", "units": "Percent", "frequency": "Daily", "notes": "long", "popularity": 99,
         "units_short": "%", "observation_start": "1962-01-02", "observation_end": "2026-10-07"},
        {"title": "no id"},
        {"id": "GS10", "title": "10-Year Monthly"},
    ]})
    result = await fred.search_series("10 year treasury")
    params = up.requests[0].url.params
    assert params["search_text"] == "10 year treasury" and params["order_by"] == "popularity"
    assert params["sort_order"] == "desc" and params["limit"] == "5"
    assert set(result) == {"query", "result_count", "results"} and result["result_count"] == 2
    assert set(result["results"][0]) == {
        "series_id", "title", "units", "frequency", "seasonal_adjustment",
        "observation_start", "observation_end", "last_updated",
    }
    assert result["results"][1]["units"] is None
    assert "notes" not in str(result)


async def test_search_series_input_rules(up):
    with pytest.raises(ToolError, match="must not be empty"):
        await fred.search_series("   ")
    with pytest.raises(ToolError, match="200 characters"):
        await fred.search_series("x" * 201)
    for limit in (0, 11, True):
        with pytest.raises(ToolError, match="between 1 and 10"):
            await fred.search_series("cpi", limit)
    assert up.requests == []


async def test_search_series_empty_and_malformed(up):
    up.on("search", {"count": 0, "seriess": []})
    result = await fred.search_series("zzzz")
    assert result["result_count"] == 0 and result["results"] == []
    up.on("search", {"count": 0})
    with pytest.raises(ToolError, match='Malformed response from fred for "cpi"'):
        await fred.search_series("cpi")


def snapshot_routes(up, overrides=None):
    overrides = overrides or {}

    def respond(request):
        series_id = request.url.params["series_id"]
        if series_id in overrides:
            return overrides[series_id](request)
        rows = [("2026-10-07", "4.5"), ("2026-10-06", "4.25")]
        if series_id == "VIXCLS":
            rows.insert(0, ("2026-10-08", "."))
        return httpx.Response(200, json=obs(*rows))

    up.on("observations", fn=respond)


async def test_macro_snapshot(up):
    snapshot_routes(up)
    result = await fred.get_macro_snapshot()
    assert result["unavailable_count"] == 0 and result["as_of"] == datetime.now(timezone.utc).date().isoformat()
    assert [i["series_id"] for i in result["indicators"]] == SNAPSHOT_IDS
    for indicator in result["indicators"]:
        assert (indicator["value"], indicator["date"]) == (4.5, "2026-10-07")
        assert (indicator["previous_value"], indicator["previous_date"]) == (4.25, "2026-10-06")
    assert len(up.requests) == 7
    start = (datetime.now(timezone.utc).date() - timedelta(days=400)).isoformat()
    for request in up.requests:
        params = request.url.params
        assert params["sort_order"] == "desc" and params["limit"] == "10" and params["observation_start"] == start
        assert ("units" in params) == (params["series_id"] == "CPIAUCSL")
        if "units" in params:
            assert params["units"] == "pc1"


async def test_macro_snapshot_partial_failure_is_not_cached(up, caplog):
    caplog.set_level(logging.DEBUG, logger="fred")
    snapshot_routes(up, {"VIXCLS": lambda request: httpx.Response(500, json={})})
    result = await fred.get_macro_snapshot()
    assert result["unavailable_count"] == 1
    assert result["indicators"][6] == {
        "key": "vix", "series_id": "VIXCLS", "label": "CBOE Volatility Index (VIX)", "error": "unavailable",
    }
    assert all("error" not in i for i in result["indicators"][:6])
    messages = [r.getMessage() for r in warnings(caplog)]
    assert len(messages) == 2
    assert "failed for VIXCLS" in messages[0] and "snapshot indicator VIXCLS unavailable" in messages[1]
    await fred.get_macro_snapshot()
    assert len(up.requests) == 14


async def test_macro_snapshot_all_failing(up):
    up.on("observations", {}, status=500)
    with pytest.raises(ToolError, match="temporarily unavailable"):
        await fred.get_macro_snapshot()


async def test_macro_snapshot_indicator_with_only_missing_values(up):
    snapshot_routes(up, {"VIXCLS": lambda request: httpx.Response(200, json=obs(("2026-10-07", "."), ("2026-10-06", ".")))})
    result = await fred.get_macro_snapshot()
    assert result["unavailable_count"] == 1 and result["indicators"][6]["error"] == "unavailable"


async def test_macro_snapshot_successful_result_is_cached(up):
    snapshot_routes(up)
    await fred.get_macro_snapshot()
    await fred.get_macro_snapshot()
    assert len(up.requests) == 7


async def test_errors_are_not_cached(up):
    up.on("series", META)
    up.on("observations", {}, status=503)
    with pytest.raises(ToolError):
        await fred.get_series("DGS10", "1y")
    up.on("observations", obs(("2026-09-01", "4.0"), ("2026-09-02", "4.1")))
    await fred.get_series("DGS10", "1y")
    assert len(up.requests) == 4


async def test_cache_expires(up, monkeypatch):
    series_routes(up, obs(("2026-09-01", "4.0"), ("2026-09-02", "4.1")))
    now = [1000.0]
    monkeypatch.setattr(fred, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    await fred.get_series("DGS10", "1y")
    now[0] += fred.CACHE_TTL_SECONDS - 1
    await fred.get_series("DGS10", "1y")
    assert len(up.requests) == 2
    now[0] += 2
    await fred.get_series("DGS10", "1y")
    assert len(up.requests) == 4


async def test_missing_api_key(up, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY")
    with pytest.raises(ToolError, match="not configured") as excinfo:
        await fred.get_series("DGS10", "1y")
    assert "FRED" not in str(excinfo.value)
    assert up.requests == []


async def test_macro_snapshot_missing_api_key(up, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY")
    with pytest.raises(ToolError, match="not configured"):
        await fred.get_macro_snapshot()
    assert up.requests == []


async def test_api_key_never_logged(up, caplog):
    caplog.set_level(logging.DEBUG, logger="fred")
    series_routes(up, obs(("2026-09-01", "4.0"), ("2026-09-02", "4.1")))
    await fred.get_series("DGS10", "1y")
    assert caplog.records and "fred-secret" not in caplog.text


async def test_server_registers_exactly_three_tools():
    tools = {t.name: t for t in await fred.mcp.list_tools()}
    assert set(tools) == {"get_series", "search_series", "get_macro_snapshot"}
    assert tools["get_series"].inputSchema["properties"]["period"]["enum"] == ["1m", "3m", "6m", "1y", "5y", "10y"]
    assert tools["get_macro_snapshot"].inputSchema.get("required", []) == []

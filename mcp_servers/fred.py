"""MCP server for FRED economic data. Run standalone over stdio: python mcp_servers/fred.py

The HTTP and cache scaffolding is deliberately copied from market_data.py, not imported:
each server runs as its own subprocess and must stay standalone.
"""
import asyncio
import logging
import math
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger("fred")
# httpx logs full request URLs at INFO, and our URLs carry the API key in the query string.
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer("fred")

FRED_URL = "https://api.stlouisfed.org/fred"
CACHE_TTL_SECONDS = 3600
MAX_POINTS = 260
SERIES_ID_PATTERN = r"[A-Z0-9_]{1,40}"
PERIOD_DAYS = {"1m": 30, "3m": 90, "6m": 180, "1y": 365, "5y": 1826, "10y": 3652}
Period = Literal["1m", "3m", "6m", "1y", "5y", "10y"]
# (key, series id, extra request params, label, units)
INDICATORS = (
    ("fed_funds", "DFF", {}, "Effective federal funds rate", "Percent"),
    ("treasury_2y", "DGS2", {}, "2-year Treasury yield", "Percent"),
    ("treasury_10y", "DGS10", {}, "10-year Treasury yield", "Percent"),
    ("yield_curve_10y2y", "T10Y2Y", {}, "10y–2y Treasury spread", "Percentage points"),
    ("cpi_inflation_yoy", "CPIAUCSL", {"units": "pc1"}, "CPI inflation, year over year", "Percent"),
    ("unemployment", "UNRATE", {}, "Unemployment rate", "Percent"),
    ("vix", "VIXCLS", {}, "CBOE Volatility Index (VIX)", "Index"),
)

_client: httpx.AsyncClient | None = None
_cache: dict[tuple, tuple[float, Any]] = {}


class _Upstream(Exception):
    """Upstream failure. kind: 'transient' (429/5xx/timeout/connection), 'rejected'
    (other 4xx, with FRED's error_message in detail) or 'malformed' (body is not JSON)."""

    def __init__(self, kind: str, status: int | None = None, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.detail = detail


def _malformed(label: str) -> ToolError:
    return ToolError(f"Malformed response from fred for {label}.")


def _normalize(raw: str) -> str:
    series_id = raw.strip().upper() if isinstance(raw, str) else ""
    if not re.fullmatch(SERIES_ID_PATTERN, series_id):
        raise ToolError(f"Invalid series id '{raw}'. Expected a FRED series ID like DGS10 or CPIAUCSL.")
    return series_id


def _cache_get(key: tuple) -> Any:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        logger.debug("cache hit %s", key)
        return hit[1]
    return None


def _cache_put(key: tuple, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


async def _cached(key: tuple, fetch: Callable[[], Awaitable[Any]]) -> Any:
    hit = _cache_get(key)
    if hit is not None:
        return hit
    result = await fetch()
    _cache_put(key, result)
    return result


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=8.0)
    return _client


async def _get_json(url: str, params: dict[str, str], key: str) -> Any:
    logger.debug("GET %s params=%s", url, params)
    try:
        response = await _get_client().get(url, params={**params, "api_key": key, "file_type": "json"})
    except httpx.TransportError:
        raise _Upstream("transient") from None
    status = response.status_code
    if status == 429 or status >= 500:
        raise _Upstream("transient", status)
    if status >= 400:
        try:
            detail = response.json().get("error_message")
        except (ValueError, AttributeError):
            detail = None
        raise _Upstream("rejected", status, detail if isinstance(detail, str) else "")
    try:
        return response.json()
    except ValueError:
        raise _Upstream("malformed") from None


async def _fetch_fred(path: str, params: dict[str, str]) -> Any:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise ToolError("Economic data provider is not configured (missing API key).")
    label = params.get("series_id") or f'"{params["search_text"]}"'
    try:
        return await _get_json(f"{FRED_URL}{path}", params, key)
    except _Upstream as exc:
        logger.debug("fred %s failed for %s: %s", path, label, exc.kind)
        if exc.kind == "malformed":
            raise _malformed(label) from None
        if exc.kind == "rejected":
            if exc.status == 400 and "does not exist" in exc.detail.lower():
                raise ToolError(f"No FRED series found with id {label}.") from None
            raise ToolError(f"Economic data provider rejected the request for {label}.") from None
        if exc.status == 429:
            raise ToolError("Economic data provider rate limit reached. Try again in a minute.") from None
        raise ToolError("Economic data provider is temporarily unavailable.") from None


def _parse_observations(body: Any, label: str) -> list[tuple[str, float]]:
    """Valid (date, value) rows in the order FRED returned them; '.' (missing) rows are dropped."""
    items = body.get("observations") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise _malformed(label)
    rows = []
    for item in items:
        try:
            day = date.fromisoformat(item["date"]).isoformat()
            raw = item["value"]
        except (KeyError, TypeError, ValueError):
            raise _malformed(label) from None
        if raw == ".":
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise _malformed(label) from None
        if not math.isfinite(number):
            raise _malformed(label)
        rows.append((day, number))
    return rows


def _downsample(rows: list[tuple[str, float]]) -> tuple[list[tuple[str, float]], str]:
    if len(rows) > MAX_POINTS:
        bucket, sampling = (lambda day: day[:7]), "monthly"
    elif len(rows) > 130:
        bucket, sampling = (lambda day: date.fromisoformat(day).isocalendar()[:2]), "weekly"
    else:
        return rows, "as_reported"
    return list({bucket(day): (day, value) for day, value in rows}.values()), sampling


async def _build_series(series_id: str, period: str) -> dict[str, Any]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=PERIOD_DAYS[period])
    body = await _fetch_fred("/series", {"series_id": series_id})
    seriess = body.get("seriess") if isinstance(body, dict) else None
    meta = seriess[0] if isinstance(seriess, list) and seriess else None
    if not isinstance(meta, dict) or not meta.get("title"):
        raise _malformed(series_id)
    body = await _fetch_fred("/series/observations", {
        "series_id": series_id,
        "observation_start": start.isoformat(),
        "observation_end": end.isoformat(),
        "sort_order": "asc",
    })
    rows = sorted(_parse_observations(body, series_id))
    if not rows:
        raise ToolError(
            f"No observations found for {series_id} over {period}. "
            "The series may be discontinued or updated less often than the window."
        )
    if len(rows) < 2:
        raise ToolError(
            f"Insufficient data for {series_id} over {period} (got {len(rows)} observations; need at least 2)."
        )
    rows, sampling = _downsample(rows)
    truncated = len(rows) > MAX_POINTS
    observations = [{"date": day, "value": round(value, 3)} for day, value in rows[-MAX_POINTS:]]
    earliest, latest = observations[0], observations[-1]
    change = round(latest["value"] - earliest["value"], 3)
    result = {
        "series_id": series_id,
        "title": meta["title"],
        "units": meta.get("units") or None,
        "frequency": meta.get("frequency") or None,
        "seasonal_adjustment": meta.get("seasonal_adjustment") or None,
        "last_updated": meta.get("last_updated") or None,
        "period": period,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "sampling": sampling,
        "observations": observations,
        "observation_count": len(observations),
        "earliest": earliest,
        "latest": latest,
        "change": change,
        "change_pct": round(change / abs(earliest["value"]) * 100, 2) if earliest["value"] else None,
        "period_high": max(observations, key=lambda o: o["value"]),
        "period_low": min(observations, key=lambda o: o["value"]),
    }
    if truncated:
        result["truncated"] = True
    return result


async def _build_search(query: str, limit: int) -> dict[str, Any]:
    label = f'"{query}"'
    body = await _fetch_fred("/series/search", {
        "search_text": query, "order_by": "popularity", "sort_order": "desc", "limit": str(limit),
    })
    seriess = body.get("seriess") if isinstance(body, dict) else None
    if not isinstance(seriess, list):
        raise _malformed(label)
    results = [
        {
            "series_id": item["id"],
            "title": item["title"],
            "units": item.get("units") or None,
            "frequency": item.get("frequency") or None,
            "seasonal_adjustment": item.get("seasonal_adjustment") or None,
            "observation_start": item.get("observation_start") or None,
            "observation_end": item.get("observation_end") or None,
            "last_updated": item.get("last_updated") or None,
        }
        for item in seriess
        if isinstance(item, dict) and item.get("id") and item.get("title")
    ]
    return {"query": query, "result_count": len(results), "results": results}


async def _read_indicator(series_id: str, extra: dict[str, str], start: str) -> list[tuple[str, float]]:
    params = {
        "series_id": series_id, "sort_order": "desc", "limit": "10", "observation_start": start, **extra,
    }
    rows = _parse_observations(await _fetch_fred("/series/observations", params), series_id)
    if not rows:
        raise ToolError(f"No recent observations for {series_id}.")
    return rows[:2]


async def _build_snapshot() -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=400)).isoformat()
    outcomes = await asyncio.gather(
        *(_read_indicator(series_id, extra, start) for _, series_id, extra, _, _ in INDICATORS),
        return_exceptions=True,
    )
    indicators = []
    for (key, series_id, _, label, units), outcome in zip(INDICATORS, outcomes):
        entry: dict[str, Any] = {"key": key, "series_id": series_id, "label": label}
        if isinstance(outcome, ToolError):
            logger.warning("snapshot indicator %s unavailable: %s", series_id, outcome)
            entry["error"] = "unavailable"
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            (day, value), previous = outcome[0], (outcome[1] if len(outcome) > 1 else None)
            entry.update(
                units=units,
                value=round(value, 3),
                date=day,
                previous_value=round(previous[1], 3) if previous else None,
                previous_date=previous[0] if previous else None,
            )
        indicators.append(entry)
    unavailable = sum("error" in entry for entry in indicators)
    if unavailable == len(INDICATORS):
        raise ToolError("Economic data provider is temporarily unavailable.")
    return {"as_of": today.isoformat(), "unavailable_count": unavailable, "indicators": indicators}


@mcp.tool()
async def get_series(series_id: str, period: Period) -> dict[str, Any]:
    """Historical values of one FRED economic data series over a fixed window, with the latest value, the change over the period, and the period high and low already computed. Common IDs: DGS10 (10-year Treasury yield), DGS2 (2-year yield), DFF (fed funds rate), CPIAUCSL (CPI index), UNRATE (unemployment rate), VIXCLS (VIX). Valid periods: 1m, 3m, 6m, 1y, 5y, 10y. For series in percent, `change` is in percentage points. Requires an exact series ID; if you only know the concept, call `search_series` first. For a one-call read on the current rate, inflation, and volatility backdrop, use `get_macro_snapshot` instead."""
    series_id = _normalize(series_id)
    if period not in PERIOD_DAYS:
        raise ToolError(f"Invalid period '{period}'. Valid periods: 1m, 3m, 6m, 1y, 5y, 10y.")
    return await _cached(("get_series", series_id, period), lambda: _build_series(series_id, period))


@mcp.tool()
async def search_series(query: str, limit: int = 5) -> dict[str, Any]:
    """Find FRED economic data series by keyword and return the most popular matches with their series IDs, titles, units, frequency, and date range. Use this when you need a series ID for `get_series` and do not already know it. Returns at most `limit` results (1–10, default 5). Does not return data values."""
    query = query.strip()
    if not query:
        raise ToolError("query must not be empty.")
    if len(query) > 200:
        raise ToolError("query must be 200 characters or fewer.")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        raise ToolError("limit must be between 1 and 10.")
    return await _cached(("search_series", query.lower(), limit), lambda: _build_search(query, limit))


@mcp.tool()
async def get_macro_snapshot() -> dict[str, Any]:
    """Latest readings of seven core US macro indicators in one call: effective fed funds rate, 2-year and 10-year Treasury yields, the 10y–2y spread, CPI inflation (year over year), the unemployment rate, and the VIX. Each comes with its date and the prior reading so direction is visible. Use this to judge whether a stock's move is about the company or about the broader rate, inflation, or risk backdrop. For the history of any one series, or for any series not listed here, use `get_series`."""
    key = ("get_macro_snapshot",)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    snapshot = await _build_snapshot()
    if snapshot["unavailable_count"] == 0:
        _cache_put(key, snapshot)
    return snapshot


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run()

"""MCP server for US-equity market data: Finnhub (quotes, profiles, news), Twelve Data
with a yfinance fallback (price history). Run standalone over stdio:
python mcp_servers/market_data.py
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
import yfinance

# mcp 2.x renamed FastMCP to MCPServer and moved it (and ToolError) to mcp.server.mcpserver.
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger("market_data")
# httpx logs full request URLs at INFO, and our URLs carry API keys in the query string.
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer("market_data")

FINNHUB_URL = "https://finnhub.io/api/v1"
TWELVEDATA_URL = "https://api.twelvedata.com"
CACHE_TTL_SECONDS = 300
MAX_POINTS = 260
SYMBOL_PATTERN = r"[A-Z]{1,5}(\.[A-Z]{1,2})?"
PERIOD_DAYS = {"1w": 7, "1m": 30, "3m": 90, "6m": 180, "1y": 365}
QUOTE_FIELDS = ("c", "d", "dp", "h", "l", "o", "pc")
Period = Literal["1w", "1m", "3m", "6m", "1y"]

_client: httpx.AsyncClient | None = None
_cache: dict[tuple, tuple[float, Any]] = {}
_warned_missing_twelvedata_key = False


class _Upstream(Exception):
    """Upstream failure. kind: 'transient' (429/5xx/timeout/connection), 'rejected'
    (other 4xx) or 'malformed' (body is not JSON)."""

    def __init__(self, kind: str, status: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status = status


def _malformed(source: str, symbol: str) -> ToolError:
    return ToolError(f"Malformed response from {source} for {symbol}.")


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _to_close(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _normalize(raw: str) -> str:
    symbol = raw.strip().upper() if isinstance(raw, str) else ""
    if not re.fullmatch(SYMBOL_PATTERN, symbol):
        raise ToolError(f"Invalid symbol '{raw}'. Expected a 1–5 letter US ticker like NVDA.")
    return symbol


async def _cached(key: tuple, fetch: Callable[[], Awaitable[Any]]) -> Any:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        logger.debug("cache hit %s", key)
        return hit[1]
    result = await fetch()
    _cache[key] = (time.monotonic(), result)
    return result


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=8.0)
    return _client


async def _get_json(url: str, params: dict[str, str], key_param: str, key: str) -> Any:
    logger.debug("GET %s params=%s", url, params)
    try:
        response = await _get_client().get(url, params={**params, key_param: key})
    except httpx.TransportError:
        raise _Upstream("transient") from None
    status = response.status_code
    if status == 429 or status >= 500:
        raise _Upstream("transient", status)
    if status >= 400:
        raise _Upstream("rejected", status)
    try:
        return response.json()
    except ValueError:
        raise _Upstream("malformed") from None


async def _fetch_finnhub(path: str, params: dict[str, str]) -> Any:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        raise ToolError("Market data provider is not configured (missing API key).")
    symbol = params["symbol"]
    try:
        return await _get_json(f"{FINNHUB_URL}{path}", params, "token", key)
    except _Upstream as exc:
        logger.warning("finnhub %s failed for %s: %s", path, symbol, exc.kind)
        if exc.kind == "malformed":
            raise _malformed("finnhub", symbol) from None
        if exc.kind == "rejected":
            suffix = " (access denied)." if exc.status in (401, 403) else "."
            raise ToolError(f"Market data provider rejected the request for {symbol}{suffix}") from None
        if exc.status == 429:
            raise ToolError("Market data provider rate limit reached. Try again in a minute.") from None
        raise ToolError("Market data provider is temporarily unavailable.") from None


async def _fetch_twelvedata_history(
    symbol: str, start: date, end: date, key: str
) -> list[tuple[str, float]]:
    params = {
        "symbol": symbol,
        "interval": "1day",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "adjust": "all",
    }
    rejected = ToolError(f"Price data provider rejected the request for {symbol}.")
    try:
        data = await _get_json(f"{TWELVEDATA_URL}/time_series", params, "apikey", key)
    except _Upstream as exc:
        if exc.kind == "transient":
            raise
        if exc.kind == "rejected":
            raise rejected from None
        raise _malformed("twelvedata", symbol) from None
    if not isinstance(data, dict):
        raise _malformed("twelvedata", symbol)
    if data.get("status") == "error" or "code" in data:
        code = data.get("code")
        if not isinstance(code, int):
            raise _malformed("twelvedata", symbol)
        if code == 429 or code >= 500:
            raise _Upstream("transient", code)
        raise rejected
    values = data.get("values")
    if not isinstance(values, list):
        raise _malformed("twelvedata", symbol)
    rows = []
    for item in values:
        try:
            day = date.fromisoformat(item["datetime"]).isoformat()
        except (KeyError, TypeError, ValueError):
            raise _malformed("twelvedata", symbol) from None
        close = _to_close(item.get("close"))
        if close is not None:
            rows.append((day, close))
    if values and not rows:
        raise _malformed("twelvedata", symbol)
    return sorted(rows)


def _yfinance_sync(symbol: str, start: date, end: date) -> list[tuple[str, Any]]:
    frame = yfinance.Ticker(symbol.replace(".", "-")).history(
        start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), auto_adjust=True
    )
    if frame is None or frame.empty:
        return []
    return [(index.date().isoformat(), close) for index, close in frame["Close"].items()]


async def _fetch_yfinance_history(symbol: str, start: date, end: date) -> list[tuple[str, float]]:
    raw = await asyncio.to_thread(_yfinance_sync, symbol, start, end)
    rows = [(day, close) for day, value in raw if (close := _to_close(value)) is not None]
    return sorted(rows)


def _summarize(
    symbol: str, period: str, source: str, start: date, end: date, rows: list[tuple[str, float]]
) -> dict[str, Any]:
    if not rows:
        raise ToolError(
            f"No price history found for {symbol}. It may be delisted or not a US-listed ticker."
        )
    if period == "1y":
        weekly = {date.fromisoformat(day).isocalendar()[:2]: (day, close) for day, close in rows}
        rows = list(weekly.values())
    truncated = len(rows) > MAX_POINTS
    points = [{"date": day, "close": round(close, 2)} for day, close in rows[-MAX_POINTS:]]
    if len(points) < 2:
        raise ToolError(
            f"Insufficient data for {symbol} over {period} (got {len(points)} points; need at least 2)."
        )
    first, last = points[0]["close"], points[-1]["close"]
    result = {
        "symbol": symbol,
        "period": period,
        "source": source,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "points": points,
        "point_count": len(points),
        "first_close": first,
        "last_close": last,
        "period_return_pct": round((last - first) / first * 100, 2),
        "period_high": max(points, key=lambda p: p["close"]),
        "period_low": min(points, key=lambda p: p["close"]),
    }
    if truncated:
        result["truncated"] = True
    return result


async def _build_history(symbol: str, period: str) -> dict[str, Any]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=PERIOD_DAYS[period])
    key = os.environ.get("TWELVEDATA_API_KEY")
    source, rows = "twelvedata", None
    if key:
        try:
            rows = await _fetch_twelvedata_history(symbol, start, end, key)
        except _Upstream as exc:
            logger.warning(
                "twelvedata unavailable for %s %s (status=%s); falling back to yfinance",
                symbol, period, exc.status,
            )
    else:
        global _warned_missing_twelvedata_key
        if not _warned_missing_twelvedata_key:
            _warned_missing_twelvedata_key = True
            logger.warning("twelvedata is not configured; using yfinance for price history")
    if rows is None:
        source = "yfinance"
        try:
            rows = await _fetch_yfinance_history(symbol, start, end)
        except Exception as exc:
            logger.warning("yfinance failed for %s %s: %s", symbol, period, type(exc).__name__)
            raise ToolError(f"Price data is temporarily unavailable for {symbol}.") from None
    return _summarize(symbol, period, source, start, end, rows)


async def _history(symbol: str, period: str) -> dict[str, Any]:
    if period not in PERIOD_DAYS:
        raise ToolError(f"Invalid period '{period}'. Valid periods: 1w, 1m, 3m, 6m, 1y.")
    return await _cached(("get_price_history", symbol, period), lambda: _build_history(symbol, period))


async def _build_quote(symbol: str) -> dict[str, Any]:
    data = await _fetch_finnhub("/quote", {"symbol": symbol})
    if not isinstance(data, dict) or not all(_is_num(data.get(f)) for f in QUOTE_FIELDS):
        raise _malformed("finnhub", symbol)
    if data["c"] == 0 and data["pc"] == 0:
        raise ToolError(f"No quote found for {symbol}. It may be delisted or not a US-listed ticker.")
    quote: dict[str, Any] = {
        "symbol": symbol,
        "price": round(data["c"], 2),
        "change": round(data["d"], 2),
        "change_pct": round(data["dp"], 2),
        "open": round(data["o"], 2),
        "high": round(data["h"], 2),
        "low": round(data["l"], 2),
        "previous_close": round(data["pc"], 2),
    }
    timestamp = data.get("t")
    try:
        if _is_num(timestamp) and timestamp > 0:
            moment = datetime.fromtimestamp(timestamp, timezone.utc)
        else:
            moment = datetime.now(timezone.utc)
            quote["as_of_estimated"] = True
    except (OverflowError, OSError, ValueError):
        raise _malformed("finnhub", symbol) from None
    quote["as_of"] = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return quote


def _market_cap_label(usd: int) -> str:
    for divisor, suffix in ((10**12, "T"), (10**9, "B"), (10**6, "M")):
        if usd >= divisor:
            return f"${usd / divisor:.1f}{suffix}"
    return f"${usd:,}"


async def _build_profile(symbol: str) -> dict[str, Any]:
    data = await _fetch_finnhub("/stock/profile2", {"symbol": symbol})
    if not isinstance(data, dict):
        raise _malformed("finnhub", symbol)
    name = data.get("name")
    if not data or not name:
        raise ToolError(f"No company profile found for {symbol}.")
    millions = data.get("marketCapitalization")
    if not isinstance(name, str) or not _is_num(millions):
        raise _malformed("finnhub", symbol)
    market_cap = int(round(millions * 1_000_000))
    return {
        "symbol": symbol,
        "name": name,
        "exchange": data.get("exchange") or None,
        "industry": data.get("finnhubIndustry") or None,
        "market_cap_usd": market_cap,
        "market_cap_label": _market_cap_label(market_cap),
        "ipo_date": data.get("ipo") or None,
        "country": data.get("country") or None,
        "website": data.get("weburl") or None,
    }


async def _build_news(symbol: str, days: int) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)
    data = await _fetch_finnhub(
        "/company-news",
        {"symbol": symbol, "from": start.isoformat(), "to": today.isoformat()},
    )
    if not isinstance(data, list):
        raise _malformed("finnhub", symbol)
    items = [
        item for item in data
        if isinstance(item, dict) and item.get("headline") and item.get("url")
    ]
    items.sort(key=lambda i: i["datetime"] if _is_num(i.get("datetime")) else 0, reverse=True)
    articles = []
    for item in items[:10]:
        stamp = item.get("datetime")
        published = (
            datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if _is_num(stamp) and stamp > 0 else None
        )
        articles.append({
            "headline": item["headline"],
            "source": item.get("source") or None,
            "published_at": published,
            "url": item["url"],
        })
    return {
        "symbol": symbol,
        "from_date": start.isoformat(),
        "to_date": today.isoformat(),
        "article_count": len(articles),
        "articles": articles,
    }


@mcp.tool()
async def get_quote(symbol: str) -> dict[str, Any]:
    """Current price and today's change for one US stock ticker. Returns price, dollar and percent change, open, high, low, previous close, and the quote timestamp. Use this for "what is X trading at" or "how is X doing today." For performance over a longer window use `get_price_history`; for comparing several tickers use `compare_performance`."""
    symbol = _normalize(symbol)
    return await _cached(("get_quote", symbol), lambda: _build_quote(symbol))


@mcp.tool()
async def get_price_history(symbol: str, period: Period) -> dict[str, Any]:
    """Daily adjusted closing prices for one US stock over a fixed window, with the period's total return, high, and low already computed. Valid periods: 1w, 1m, 3m, 6m, 1y. Use for "how has X done over the last month / six months." For comparing several tickers over the same window, call `compare_performance` once instead of calling this repeatedly. For today's price only, use `get_quote`."""
    return await _history(_normalize(symbol), period)


@mcp.tool()
async def compare_performance(symbols: list[str], period: Period) -> dict[str, Any]:
    """Compare the total return of 2–5 US stock tickers over the same window and rank them. Valid periods: 1w, 1m, 3m, 6m, 1y. Returns each ticker's start and end price and percent return, plus a ranking, best, worst, and the spread between them. Use this whenever a question compares two or more tickers; do not call `get_price_history` once per ticker and compute the difference yourself."""
    unique = list(dict.fromkeys(_normalize(s) for s in symbols))
    if len(unique) < 2:
        raise ToolError("compare_performance needs at least 2 distinct symbols.")
    if len(unique) > 5:
        raise ToolError("compare_performance accepts at most 5 symbols.")
    histories = await asyncio.gather(*(_history(s, period) for s in unique), return_exceptions=True)
    for history in histories:
        if isinstance(history, BaseException):
            raise history
    results = [
        {k: h[k] for k in ("symbol", "first_close", "last_close", "period_return_pct", "source")}
        for h in histories
    ]
    ranked = sorted(results, key=lambda r: r["period_return_pct"], reverse=True)
    best, worst = ranked[0], ranked[-1]
    return {
        "period": period,
        "start_date": histories[0]["start_date"],
        "end_date": histories[0]["end_date"],
        "results": results,
        "ranking": [r["symbol"] for r in ranked],
        "best": {"symbol": best["symbol"], "period_return_pct": best["period_return_pct"]},
        "worst": {"symbol": worst["symbol"], "period_return_pct": worst["period_return_pct"]},
        "spread_pct": round(best["period_return_pct"] - worst["period_return_pct"], 2),
    }


@mcp.tool()
async def get_company_profile(symbol: str) -> dict[str, Any]:
    """Basic facts about the company behind a US stock ticker: name, exchange, industry, market capitalization, IPO date, and website. Use this to understand what a company does or how large it is, for example when judging whether a price move is unusual. This tool does not return prices or news."""
    symbol = _normalize(symbol)
    return await _cached(("get_company_profile", symbol), lambda: _build_profile(symbol))


@mcp.tool()
async def get_company_news(symbol: str, days: int = 7) -> dict[str, Any]:
    """Recent news headlines about one specific US stock ticker from financial news feeds, up to 10 articles from the last `days` days (1–30, default 7). Returns headline, source, publish time, and URL only; it does not return article text. Use this when the question is about news for a particular company. For broader questions — market-wide events, macroeconomics, analysis, or anything not tied to a single ticker — use the web search tool instead."""
    symbol = _normalize(symbol)
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 30:
        raise ToolError("days must be between 1 and 30.")
    return await _cached(("get_company_news", symbol, days), lambda: _build_news(symbol, days))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run()

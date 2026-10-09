"""Load MCP server manifests and discover their tools through MultiServerMCPClient."""
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger("mcp_client")

DISCOVERY_TIMEOUT_SECONDS = 30
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_config(path: str | Path, include: set[str] | None = None) -> dict[str, dict]:
    """Read the manifest, expand ${VAR} placeholders from os.environ, and pin `python` to this interpreter."""
    servers: dict[str, dict] = json.loads(Path(path).read_text())
    if include is not None:
        unknown = include - servers.keys()
        if unknown:
            raise ValueError(f"Unknown MCP server(s) in {path}: {sorted(unknown)}")
        servers = {name: conn for name, conn in servers.items() if name in include}
    warned: set[str] = set()

    def expand(value: str) -> str:
        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                if name not in warned:
                    warned.add(name)
                    logger.warning("environment variable %s is not set; substituting an empty string", name)
                return ""
            return os.environ[name]

        return _PLACEHOLDER.sub(substitute, value)

    config = {}
    for name, connection in servers.items():
        connection = dict(connection)
        for field in ("env", "headers"):
            if field in connection:
                connection[field] = {key: expand(value) for key, value in connection[field].items()}
        if "url" in connection:
            connection["url"] = expand(connection["url"])
        if connection.get("command") == "python":
            connection["command"] = sys.executable
        config[name] = connection
    return config


async def _discover_one(name: str, connection: dict) -> list[BaseTool] | None:
    # Stateless sessions on purpose: each tool call spawns a fresh stdio subprocess, so a
    # server that dies mid-conversation self-heals on the next call. The cost is subprocess
    # startup per call and no in-process cache survival across calls, acceptable at this traffic.
    client = MultiServerMCPClient({name: connection})
    try:
        tools = await asyncio.wait_for(client.get_tools(), timeout=DISCOVERY_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.error("MCP server %s failed during discovery: %s: %s", name, type(exc).__name__, exc)
        return None
    logger.info("discovered %d tools from %s: %s", len(tools), name, [tool.name for tool in tools])
    return tools


async def discover_tools(config: dict[str, dict]) -> tuple[list[BaseTool], list[str]]:
    """Return (tools, failed server names). One dead server never blocks the others."""
    results = await asyncio.gather(*(_discover_one(name, conn) for name, conn in config.items()))
    tools: list[BaseTool] = []
    failed: list[str] = []
    owner: dict[str, str] = {}
    for name, server_tools in zip(config, results):
        if server_tools is None:
            failed.append(name)
            continue
        for tool in server_tools:
            if tool.name in owner:
                raise RuntimeError(
                    f"Tool name '{tool.name}' is exposed by both '{owner[tool.name]}' and '{name}'"
                )
            owner[tool.name] = name
        tools.extend(server_tools)
    return tools, failed

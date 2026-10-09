"""Test-only MCP server: tools that break the transport on purpose."""
import os
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("flaky")


@mcp.tool()
def die() -> str:
    """Kill the server process mid-call (transport closes during an operation)."""
    os._exit(1)


@mcp.tool()
def garbage_then_ok() -> str:
    """Write a non-JSON-RPC line to stdout, then return normally."""
    sys.stdout.write("this is not json-rpc\n")
    sys.stdout.flush()
    return "ok after garbage"


if __name__ == "__main__":
    mcp.run()

"""FastMCP server wrapping the FRED API. Run standalone: python mcp_servers/fred.py"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fred")

if __name__ == "__main__":
    mcp.run(transport="stdio")

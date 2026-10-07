"""FastMCP server wrapping Finnhub. Run standalone: python mcp_servers/market_data.py"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("market_data")

if __name__ == "__main__":
    mcp.run(transport="stdio")

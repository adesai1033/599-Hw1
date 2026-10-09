"""Dev REPL: python scripts/repl.py --servers market_data [--session dev1] [--log DEBUG]. Run from the repo root."""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import ask, build_agent  # noqa: E402


async def main(servers: set[str], session: str) -> None:
    graph, failed = await build_agent(os.environ.get("MCP_SERVERS_CONFIG", "mcp_config.json"), servers)
    if failed:
        print("servers that failed to start:", ", ".join(failed))
    while True:
        try:
            line = (await asyncio.to_thread(input, "> ")).strip()
        except EOFError:
            break
        if line:
            print(await ask(graph, line, session))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--servers", default="market_data", help="comma-separated server names")
    parser.add_argument("--session", default="dev")
    parser.add_argument("--log", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log.upper(), stream=sys.stderr)
    asyncio.run(main(set(args.servers.split(",")), args.session))

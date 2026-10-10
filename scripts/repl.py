"""Dev REPL: python scripts/repl.py --servers market_data [--session dev1] [--log DEBUG]. Run from the repo root."""
import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import ask, build_agent  # noqa: E402


async def main(servers: set[str], session: str) -> None:
    graph, unavailable, _, owner = await build_agent(os.environ.get("MCP_SERVERS_CONFIG", "mcp_config.json"), servers)
    if unavailable:
        print("servers that failed to start:", ", ".join(f"{name} ({text})" for name, text in unavailable.items()))
    while True:
        try:
            line = (await asyncio.to_thread(input, "> ")).strip()
        except EOFError:
            break
        if line:
            answer, trace = await ask(graph, line, session, owner=owner)
            print(answer)
            for call in trace:
                print(f"  ↳ {call['server']}.{call['tool']}({json.dumps(call['args'])}) → {call['status']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--servers", default="market_data", help="comma-separated server names")
    parser.add_argument("--session", default="dev")
    parser.add_argument("--log", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log.upper(), stream=sys.stderr)
    asyncio.run(main(set(args.servers.split(",")), args.session))

"""Test-only MCP server speaking raw JSON-RPC over stdio; tools/call returns an off-schema result."""
import json
import sys


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method, msg_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": msg["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "offschema", "version": "0"},
        }})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [{
            "name": "offschema",
            "description": "Returns a result that does not match the CallToolResult schema.",
            "inputSchema": {"type": "object", "properties": {}},
        }]}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"bogus": True, "content": "not-a-list"}})
    elif msg_id is not None:
        send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}})

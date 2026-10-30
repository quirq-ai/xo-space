"""A model-free MCP server used by installed-CLI integration tests."""
import json
import sys


def main():
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {"protocolVersion": request["params"]["protocolVersion"],
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "test-comms", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [] if "--empty" in sys.argv else [
                {"name": "reply", "description": "Test reply", "inputSchema": {"type": "object"}}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                              "error": {"code": -32601, "message": "not served by fixture"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()

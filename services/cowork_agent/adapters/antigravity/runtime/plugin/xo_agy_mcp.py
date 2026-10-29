"""XO's agy MCP relay: agy starts it as a stdio MCP server, one per name XO
mounts (``xo_agy_mcp.py <server>``), and it relays every JSON-RPC message to
the session's runtime over its socket. The runtime makes the HTTP call to
the real server (XO's comms, Composio) with the session's token, so the
token never reaches agy, its env or its shell.

Outside an XO session (no ``XO_AGY_SOCKET``), or for a name the session
doesn't mount, it serves an empty tool list.

Stdlib only and no XO imports, like the hook.
"""

import json
import os
import socket
import sys
import threading

PROTOCOL = 1
SOCKET_ENV = "XO_AGY_SOCKET"


def write(msg, lock):
    with lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def inert(server):
    """No session: answer agy's start-up so it shows nothing."""
    lock = threading.Lock()
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict) or msg.get("id") is None:
            continue
        method = msg.get("method")
        if method == "initialize":
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                      "serverInfo": {"name": server, "version": str(PROTOCOL)}}
            write({"jsonrpc": "2.0", "id": msg["id"], "result": result}, lock)
        elif method == "tools/list":
            write({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": []}}, lock)
        else:
            write({"jsonrpc": "2.0", "id": msg["id"],
                   "error": {"code": -32601, "message": "XO serves nothing outside its own sessions"}}, lock)


def relay(server, path):
    lock = threading.Lock()
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(path)
    conn.sendall(json.dumps({"v": PROTOCOL, "type": "mcp_hello", "server": server, "pid": os.getpid()}).encode()
                 + b"\n")
    reader = conn.makefile("rb")

    def answers():
        for line in reader:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and isinstance(msg.get("message"), dict):
                write(msg["message"], lock)
        os._exit(0)                      # the session ended: so does this server

    threading.Thread(target=answers, daemon=True).start()
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            conn.sendall(json.dumps({"v": PROTOCOL, "type": "mcp", "server": server, "message": msg}).encode()
                         + b"\n")


def main():
    server = sys.argv[1] if len(sys.argv) > 1 else ""
    path = os.environ.get(SOCKET_ENV)
    if not path:
        inert(server)
        return
    try:
        relay(server, path)
    except OSError:
        inert(server)


if __name__ == "__main__":
    main()

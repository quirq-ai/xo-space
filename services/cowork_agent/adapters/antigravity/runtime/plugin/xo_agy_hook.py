"""XO's agy hook: agy runs it for its lifecycle events (PreToolUse,
SessionStart, PreInvocation, Stop), with the event's JSON on
stdin, and prints the answer.

Inside a session XO runs (``XO_AGY_SOCKET`` is set), it hands the event to
that session's runtime over its socket and prints what the runtime answers;
the runtime decides every tool call the way claude's modes would. Anything
going wrong refuses the tool call (fails closed): no socket, no answer, a
malformed one. Outside an XO session it gives agy no opinion, so the person's
own agy behaves as if this plugin weren't there.

Stdlib only and no XO imports: agy runs it with the system's ``python3``,
wherever XO is installed. It never prints an empty answer: agy reads that as
"no opinion" and, headless, refuses the call and ends the turn.
"""

import json
import os
import socket
import sys

PROTOCOL = 1
SOCKET_ENV = "XO_AGY_SOCKET"
LIMIT = 16 * 1024 * 1024


def refusal(event, why):
    if event == "PreToolUse":
        return {"decision": "deny", "reason": f"XO's permission check didn't answer ({why}), so this action is "
                                              "refused. Tell the person if it keeps happening."}
    return {}


def ask(path, event, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(path)
        conn.sendall(json.dumps({"v": PROTOCOL, "type": "hook", "event": event, "payload": payload}).encode()
                     + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > LIMIT:
                raise ValueError("answer too long")
    answer = json.loads(buf.decode() or "null")
    if not isinstance(answer, dict):
        raise ValueError("not a JSON object")
    if event == "PreToolUse" and not isinstance(answer.get("decision"), str):
        raise ValueError("no decision")
    return answer


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = sys.stdin.read()
    path = os.environ.get(SOCKET_ENV)
    if not path:
        out = {"decision": "allow"} if event == "PreToolUse" else {}
    else:
        try:
            payload = json.loads(raw or "{}")
            out = ask(path, event, payload if isinstance(payload, dict) else {})
        except Exception as exc:  # noqa: BLE001 - any failure refuses, never allows
            out = refusal(event, type(exc).__name__)
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()

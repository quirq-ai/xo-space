import json
import socket
import sys
from pathlib import Path


def decide(folder):
    raw = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        return False
    event = json.loads(raw)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.connect(str(folder / "policy.sock"))
        channel.sendall(json.dumps(event).encode() + b"\n")
        with channel.makefile("rb") as reader:
            return reader.readline(16) == b"allow\n"


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    if sys.argv[2] == "ready":
        (folder / "ready").touch(mode=0o600)
        print("{}")
    else:
        try:
            allowed = decide(folder)
        except (ValueError, OSError):
            allowed = False
        if allowed:
            print(json.dumps({"permission": "allow", "hookSpecificOutput":
                              {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
        else:
            print(json.dumps({"permission": "deny", "decision": "block", "reason": "Storm denied the tool call"}))
            sys.exit(2)

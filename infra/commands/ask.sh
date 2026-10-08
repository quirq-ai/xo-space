#!/bin/sh
: Ask the active agent and stream its answer, for example qq ask "summarise this repo"
# Continue a conversation: QQ_SESSION=<id printed at the end> qq ask "..."
# Work inside a project folder: QQ_PROJECT=<project> qq ask "..."
[ $# -gt 0 ] || { echo 'usage: qq ask "QUESTION"   (QQ_SESSION=ID continues, QQ_PROJECT=NAME works in a project)' >&2; exit 2; }
url=$(. infra/qq-lib.sh; qq_url)
root=$(. infra/qq-lib.sh; _qq_pointer projects_root || pwd)
exec python3 - "$url" "$root" "$*" <<'PY'
import json, os, sys, urllib.error, urllib.request
url, root, text = sys.argv[1], sys.argv[2], sys.argv[3]

def post(path, body):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

body = {"text": text}
if os.environ.get("QQ_SESSION"):
    body["session_id"] = os.environ["QQ_SESSION"]
if os.environ.get("QQ_PROJECT"):
    body["workspace"] = os.path.join(root, os.environ["QQ_PROJECT"])
try:
    started = post("/api/chat/prompt", body)
except urllib.error.HTTPError as e:
    sys.exit(f"qq ask: HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
except OSError:
    sys.exit(f"qq ask: no answer from {url}, is the server running")
stream_id, session = started.get("stream_id"), started.get("session_id")
if not stream_id:
    sys.exit(f"qq ask: unexpected reply: {json.dumps(started)[:300]}")

event, failed = None, False
try:
    with urllib.request.urlopen(url + f"/api/chat/stream/{stream_id}", timeout=600) as r:
        for raw in r:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip() or "{}")
                if event == "text-delta":
                    print(data.get("text", ""), end="", flush=True)
                elif event == "model-loading" and data.get("label"):
                    print(f"\n[{data['label']}]", file=sys.stderr, flush=True)
                elif event in ("agent-error", "error"):
                    print(f"\nqq ask: {data.get('error_message', 'error')}", file=sys.stderr)
                    failed = True
                elif event == "done":
                    session = data.get("session_id") or session
                    break
except KeyboardInterrupt:
    try:
        post("/api/chat/abort", {"stream_id": stream_id})
    except OSError:
        pass
    print("\nqq ask: stopped following (the agent may still finish on the server)", file=sys.stderr)
    sys.exit(130)
print()
if session:
    print(f"\n(session {session}; continue with QQ_SESSION={session} qq ask \"...\")", file=sys.stderr)
sys.exit(1 if failed else 0)
PY

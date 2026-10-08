#!/bin/sh
: Show inbox items, open by default, or qq inbox done or qq inbox all
url=$(. infra/qq-lib.sh; qq_url)
body=$(curl -fs "$url/api/inbox?status=${1:-open}&limit=50") || { echo "qq inbox: no answer from $url, is the server running, status is open, done or all" >&2; exit 1; }
python3 - "$body" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
counts = d.get("counts") or {}
print("inbox: " + ", ".join(f"{v} {k}" for k, v in counts.items()) if counts else "inbox")
for it in d.get("items", []):
    print(f"  {str(it.get('ts', ''))[:16]:<16}  {it.get('status', ''):<5} {it.get('kind', ''):<22} {it.get('title', '')}")
PY

#!/bin/sh
: List the projects in this Space
url=$(. infra/qq-lib.sh; qq_url)
body=$(curl -fs "$url/api/xo-projects") || { echo "qq projects: no answer from $url, is the server running" >&2; exit 1; }
python3 - "$body" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
items = d.get("items", [])
print(f"{d.get('total', len(items))} project(s)")
for p in items:
    note = "  (not scaffolded)" if p.get("unscaffolded") else ""
    print(f"  {p.get('id', ''):<30} {p.get('display_name') or ''}{note}")
PY

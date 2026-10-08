#!/bin/sh
: List the Spaces a project is shared with, for example qq members my-project
. infra/qq-lib.sh
[ -n "${1:-}" ] || { echo "usage: qq members PROJECT" >&2; exit 2; }
body=$(qq_call GET "/api/xo-projects/$(qq_path_part "$1")/members") || exit 1
python3 - "$body" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
own = d.get("own_workspace_id")
print(f"{d.get('repo')}: {len(d.get('members') or [])} member(s)")
for m in d.get("members") or []:
    you = "  (this Space)" if m.get("workspace_id") == own else ""
    print(f"  {m.get('workspace_id')}  {m.get('role')}, {m.get('status')}{you}")
PY

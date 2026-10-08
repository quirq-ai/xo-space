#!/bin/sh
: Show the shared branch of a project and what is new, for example qq commits my-project
. infra/qq-lib.sh
[ -n "${1:-}" ] || { echo "usage: qq commits PROJECT [COUNT]" >&2; exit 2; }
body=$(qq_call GET "/api/xo-projects/$(qq_path_part "$1")/commits?limit=${2:-10}") || exit 1
python3 - "$body" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
behind = d.get("behind")
state = "nothing fetched yet" if behind is None else "in sync" if behind == 0 else f"{behind} new, run: qq apply {d.get('project_id')}"
print(f"{d.get('project_id')} on {d.get('branch')}: {state}")
for i, c in enumerate(d.get("commits") or []):
    mark = "new" if isinstance(behind, int) and i < behind else "   "
    print(f"  {mark} {c.get('hash', '')[:7]}  {c.get('date', '')[:10]}  {c.get('author', ''):<16} {c.get('subject', '')}")
PY

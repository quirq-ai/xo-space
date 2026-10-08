#!/bin/sh
: Fast-forward a project to the commits fetched from its sharers, for example qq apply my-project
. infra/qq-lib.sh
[ -n "${1:-}" ] || { echo "usage: qq apply PROJECT" >&2; exit 2; }
body=$(qq_call POST "/api/xo-projects/$(qq_path_part "$1")/apply") || exit 1
python3 - "$body" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
n = d.get("applied") or 0
print(f"qq apply: {d.get('project_id')} " + (f"moved forward {n} commit(s), now at {str(d.get('head'))[:7]}" if n else f"was already up to date at {str(d.get('head'))[:7]}"))
PY

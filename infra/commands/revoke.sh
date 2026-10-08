#!/bin/sh
: Stop sharing a project with a Space, for example qq revoke my-project SPACE_ID
. infra/qq-lib.sh
[ -n "${1:-}" ] && [ -n "${2:-}" ] || { echo "usage: qq revoke PROJECT SPACE_ID" >&2; exit 2; }
data=$(python3 -c 'import json, sys; print(json.dumps({"workspace_id": sys.argv[1]}))' "$2")
qq_call POST "/api/xo-projects/$(qq_path_part "$1")/revoke" "$data" >/dev/null || exit 1
echo "qq revoke: $2 no longer receives $1. Their local copy stays."

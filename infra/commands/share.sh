#!/bin/sh
: Share a project with another Space, for example qq share my-project SPACE_ID
. infra/qq-lib.sh
[ -n "${1:-}" ] && [ -n "${2:-}" ] || { echo "usage: qq share PROJECT SPACE_ID" >&2; exit 2; }
data=$(python3 -c 'import json, sys; print(json.dumps({"workspace_id": sys.argv[1]}))' "$2")
qq_call POST "/api/xo-projects/$(qq_path_part "$1")/share" "$data" >/dev/null || exit 1
echo "qq share: $1 is now shared with $2. Their Space picks it up on its next poll, about a minute."

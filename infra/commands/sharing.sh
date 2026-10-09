#!/bin/sh
: Show the project sharing relay status, qq sharing check to make it poll now, qq sharing tick to run one tick here
. infra/qq-lib.sh
case "${1:-}" in
    "") ;;
    check) qq_call POST /api/project-sharing/check >/dev/null && echo "qq sharing: asked the relay to poll now"; exit ;;
    tick)
        # One relay tick in this process: what the watcher's "sharing tick" job runs every minute.
        shift
        [ -x .qq/venv/bin/python ] || { echo "qq sharing tick: no .qq/venv yet, run qq deps first" >&2; exit 1; }
        QUIRQ_STATE_ROOT="$(qq_state)" XO_PROJECTS_ROOT="${XO_PROJECTS_ROOT:-$(_qq_pointer projects_root || pwd)}"             exec .qq/venv/bin/python -m services.cowork_agent.project_sharing.tick "$@" ;;
    *) echo "usage: qq sharing [check | tick [--json]]" >&2; exit 2 ;;
esac
body=$(qq_call GET /api/project-sharing/status) || exit 1
python3 - "$body" <<'PY'
import json, sys
s = json.loads(sys.argv[1])
state = s.get("cadence") or "unknown"
if s.get("reason"):
    state += f" ({s['reason']})"
print(f"relay: {state}, watching branch {s.get('watch_branch') or 'main'}")
print(f"this Space: {s.get('own_workspace_id') or 'no XO_SPACE_ID set'}")
if s.get("last_poll_at"):
    print(f"last poll: {s['last_poll_at'][:19]} ({'ok' if s.get('last_poll_ok') else 'failed'})")
repos = s.get("repos") or {}
print(f"{len(repos)} repo(s) known to the relay")
for repo, r in sorted(repos.items()):
    others = r.get("members")
    who = "not shared" if not r.get("shared") else f"shared, {others - 1} other Space(s)" if isinstance(others, int) else "shared"
    where = r.get("project") or ("available, not cloned" if r.get("available") else "-")
    extra = []
    if r.get("fetched"):
        extra.append(f"{r['fetched']} fetched")
    if r.get("pending_github"):
        extra.append("needs GitHub access")
    if r.get("last_error"):
        extra.append(f"error: {r['last_error']}")
    if (r.get("clone") or {}).get("state"):
        extra.append(f"clone: {r['clone']['state']}")
    print(f"  {repo}\n      {where}, {who}" + (", " + ", ".join(extra) if extra else ""))
PY

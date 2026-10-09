#!/bin/sh
: Check whether a newer xo-space is on its branch, without changing anything
QUIRQ_STATE_ROOT="$(. infra/qq-lib.sh; qq_state)" exec python3 - "$@" <<'PY'
import json, sys
from services.cowork_agent.self_update import check_update_status
as_json = "--json" in sys.argv[1:]
try:
    s = check_update_status()
except Exception as e:
    if as_json:
        print(json.dumps({"code": "update_status_failed", "message": f"Could not determine the checkout's version state: {e}"}))
        raise SystemExit(1)
    raise SystemExit(f"qq update-check: {e}")
if as_json:
    print(json.dumps(s))
    raise SystemExit(0)
if not s.get("supported"):
    raise SystemExit(f"qq update-check: {s['message']}")
cur, lat = s.get("current") or {}, s.get("latest")
print(f"on {s['branch']} at {cur.get('sha', '?')[:7]} ({cur.get('date', '?')}) {cur.get('subject', '')}")
if s.get("message"):
    print(s["message"])
if lat is None:
    raise SystemExit(1)
if s["up_to_date"]:
    print("up to date")
else:
    print(f"{s['behind']} new commit(s) on {s['remote']}/{s['branch']}, newest {lat['sha'][:7]} ({lat['date']}) {lat['subject']}")
    print("run: qq update")
if s.get("ahead"):
    print(f"note: {s['ahead']} local commit(s) are not on the remote, so qq update will refuse")
if s.get("dirty"):
    print("note: the checkout has local changes, so qq update will refuse")
PY

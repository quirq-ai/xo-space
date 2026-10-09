#!/bin/sh
: Fast-forward this checkout to the newest commit on its branch, like the Setup tab Update button
QUIRQ_STATE_ROOT="$(. infra/qq-lib.sh; qq_state)" exec python3 - "$@" <<'PY'
import json, sys
from services.cowork_agent.self_update import UpdateError, apply_update
as_json = "--json" in sys.argv[1:]
try:
    r = apply_update()
except UpdateError as e:
    if as_json:
        print(json.dumps({"code": "update_failed", "message": str(e)}))
        raise SystemExit(1)
    raise SystemExit(f"qq update: {e}")
refused = not r.get("updated") and r.get("reason") != "up_to_date"
if as_json:
    # The same object the Setup tab gets; a refusal (dirty tree, diverged, ...) still exits 1.
    print(json.dumps(r))
    raise SystemExit(1 if refused else 0)
print(r["message"])
if r.get("updated"):
    if r.get("requirements_changed"):
        print("next: qq deps, then restart")
    print("restart: qq stop, then qq start")
elif refused:
    raise SystemExit(1)
PY

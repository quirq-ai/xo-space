#!/bin/sh
: Fast-forward this checkout to the newest commit on its branch, like the Setup tab Update button
QUIRQ_STATE_ROOT="$(. infra/qq-lib.sh; qq_state)" exec python3 - <<'PY'
from services.cowork_agent.self_update import UpdateError, apply_update
try:
    r = apply_update()
except UpdateError as e:
    raise SystemExit(f"qq update: {e}")
print(r["message"])
if r.get("updated"):
    if r.get("requirements_changed"):
        print("next: qq deps, then restart")
    print("restart: qq stop, then qq start")
elif r.get("reason") != "up_to_date":
    raise SystemExit(1)
PY

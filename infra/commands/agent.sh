#!/bin/sh
: Show the active agent runtime and every runtime this Space supports, read only
. infra/qq-lib.sh
body=$(qq_call GET /api/runtime-config) || exit 1
python3 - "$body" <<'PY'
import json, sys
s = json.loads(sys.argv[1])
applied, configured = s.get("applied") or {}, s.get("configured") or {}
print(f"active agent: {applied.get('agent_name')}")
if configured.get("agent_name") and configured.get("agent_name") != applied.get("agent_name"):
    print(f"  saved but not running yet: {configured['agent_name']} (restart: qq stop, then qq start)")
if s.get("restart_required"):
    print(f"  restart pending: {', '.join(map(str, s.get('restart_reasons') or [])) or 'settings changed'}")
print(f"watcher: {'on' if applied.get('watcher_enabled') else 'off'}, every {applied.get('watcher_interval_seconds')}s, "
      f"sources {applied.get('watcher_source_mode')}")
print("runtimes:")
for a in s.get("agents") or []:
    avail = a.get("binary_available")
    program = (f"{a.get('binary')} " + ("installed" if avail else "not installed")) if a.get("binary") else "no local program"
    health = "" if a.get("health_ok") is None else ", healthy" if a.get("health_ok") else ", NOT healthy"
    files = a.get("session_files")
    files = len(files) if isinstance(files, list) else files
    secrets = a.get("secrets") or []
    keys = f", keys {sum(1 for k in secrets if k.get('configured'))}/{len(secrets)} set" if secrets else ""
    mark = "*" if a.get("active") else " "
    print(f"  {mark} {a.get('name', ''):<12} {program}{health}, {files or 0} session file(s){keys}")
PY

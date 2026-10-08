#!/bin/sh
# Stop this checkout's server only, through its localhost-only HTTP route (kills nothing else).
curl -fsS -X POST "$(. infra/qq-lib.sh; qq_url)/space/server/stop" && echo || { echo "qq stop: no xo-space server answered (qq info shows where qq looks)" >&2; exit 1; }

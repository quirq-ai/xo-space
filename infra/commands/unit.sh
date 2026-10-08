#!/bin/sh
: Run chosen tests with pytest, for example qq unit tests/test_swarm_api.py -k poll
[ -x .qq/venv/bin/python ] || { echo "qq unit: no test venv yet, run qq test tests once to create it" >&2; exit 1; }
exec .qq/venv/bin/python -m pytest -q -p no:cacheprovider "$@"

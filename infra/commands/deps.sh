#!/bin/sh
: Reinstall the Python dependencies into venv with uv, as install.sh does
[ -x venv/bin/python ] || { echo "qq deps: no venv yet, run qq start once to create it" >&2; exit 1; }
uv=$(command -v uv || echo "$HOME/.local/bin/uv")
exec "$uv" pip install --python venv/bin/python --requirement requirements.txt

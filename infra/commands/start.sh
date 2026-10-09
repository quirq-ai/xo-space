#!/bin/sh
: Start xo-space in the foreground on .qq/venv, the repo pinned Python
# qq build server makes or updates .qq/venv (pinned toolchain + requirements.txt);
# install.sh then does everything else as usual and runs server.py from that venv.
qq build server || exit 1
QUIRQ_VENV_DIR="$(pwd -P)/.qq/venv" exec bash ./install.sh

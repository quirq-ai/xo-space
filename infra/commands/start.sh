#!/bin/sh
# Start xo-space in the foreground on the repo's pinned Python (Ctrl-C or `qq stop` to stop).
QUIRQ_PYTHON_VERSION="${QUIRQ_PYTHON_VERSION:-3.14}" exec bash ./install.sh

#!/bin/sh
: Show what uninstall would remove, and only remove it with --yes
if [ $# -eq 0 ]; then
    echo "qq uninstall: dry run, nothing is removed. To really uninstall: qq uninstall --yes"
    exec bash ./uninstall.sh --dry-run
fi
exec bash ./uninstall.sh "$@"

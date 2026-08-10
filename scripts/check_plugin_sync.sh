#!/usr/bin/env bash
# The Claude Code plugin (plugin/) and the Codex plugin (.agents/plugins/quirq/)
# share discovery logic that must not drift. Run from the repo root; exits
# non-zero with a diff when the copies disagree.
set -u

cd "$(dirname "$0")/.." || exit 1

fail=0
compare() {
    if ! diff -u "$1" "$2"; then
        printf '\nOut of sync: %s vs %s\n' "$1" "$2" >&2
        fail=1
    fi
}

compare plugin/scripts/discover.sh .agents/plugins/quirq/skills/quirq/scripts/discover.sh

if [ "$fail" -eq 0 ]; then
    printf 'Plugin bundles in sync.\n'
fi
exit "$fail"

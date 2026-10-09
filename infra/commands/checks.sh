#!/bin/sh
: Run every CI check except the test suite, keep going, and summarise
[ -x .qq/venv/bin/python ] || { echo "qq checks: no .qq/venv yet, run qq deps first" >&2; exit 1; }
failed=""

step() {
    name=$1; shift
    echo "== $name"
    if "$@"; then echo "   ok"; else echo "   FAILED"; failed="$failed
   - $name"; fi
}

no_env_files() {
    # The same rule and allow-list as CI (and tests/test_checkout_hygiene.py).
    found=$(git ls-files | grep -E '(^|/)(\.env(\..*)?|[^/]*\.env)$' \
        | grep -vxF -e .env.example \
            -e tests/fixtures/quirq-state/secrets/secrets.env \
            -e tests/fixtures/quirq-state/settings/roots.env \
            -e tests/fixtures/quirq-state/settings/runtime.env || true)
    [ -z "$found" ] || { echo "   env files must not be committed:"; echo "$found"; return 1; }
}

ui_modules_parse() {
    # Through stdin as a module: `node --check file.js` passes a module with a syntax error.
    bad=$(find space_ui/js -name '*.js' | sort | while IFS= read -r f; do
        node --input-type=module --check < "$f" >/dev/null 2>&1 || echo "$f"
    done)
    [ -z "$bad" ] || { echo "   does not parse:"; echo "$bad"; return 1; }
}

step "no committed .env files" no_env_files
step "byte-compile" .qq/venv/bin/python -m compileall -q server.py config routers services utils scripts tests
step "route parity, every agent runtime" .qq/venv/bin/python scripts/check_route_parity.py
step "install.sh harness" bash tests/install_sh_harness.sh
step "uninstall.sh harness" bash tests/uninstall_sh_harness.sh
step "plugin bundles in sync" bash scripts/check_plugin_sync.sh
step "plugin scripts parse" sh -c 'bash -n plugins/quirq/scripts/space.sh && bash -n plugin/scripts/discover.sh'
step "Space UI modules parse" ui_modules_parse

if [ -z "$failed" ]; then
    echo "qq checks: all passed. The test suite is separate: qq test tests"
    exit 0
fi
echo "qq checks: failed:$failed"
exit 1

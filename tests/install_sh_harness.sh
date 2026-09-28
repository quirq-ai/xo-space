#!/usr/bin/env bash
# tests/install_sh_harness.sh — exercises install.sh's resolve_repo_dir,
# fetch_repo (clones and release updates) and print_restart_hint in isolation.
#
# Sources everything except the final `main "$@"`, against temp directories
# and a local file:// git remote named xo-space, so REPO_NAME resolves the way
# it does for the real one. No network, no uv, no venv, no server is started.
# Run directly (bash tests/install_sh_harness.sh) or via tests/test_install_sh.py.
# INSTALL_SH=<path> points it at another copy of the script.
set -u
SRC="${INSTALL_SH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/install.sh}"
W="$(mktemp -d)"
pass=0; fail=0
ok()   { pass=$((pass+1)); printf 'PASS  %s\n' "$1"; }
bad()  { fail=$((fail+1)); printf 'FAIL  %s\n      got: %s\n' "$1" "$2"; }
check(){ [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2  (want: $3)"; }

# ---- functions-only copy -------------------------------------------------
last="$(tail -n 1 "$SRC" | tr -d '\r' | sed 's/[[:space:]]*$//')"
[ "$last" = 'main "$@"' ] || { echo "unexpected last line:"; tail -n 1 "$SRC" | od -c | head -3; exit 1; }
tr -d '\r' < "$SRC" | sed '$d' > "$W/lib.sh"

# ---- fake upstream on main -----------------------------------------------
G="git -c user.name=t -c user.email=t@t"
mkdir -p "$W/origin/xo-space" && cd "$W/origin/xo-space"
git init -q -b main . && echo a > server.py && echo b > requirements.txt
$G add . && $G commit -qm v1
export QUIRQ_SOURCE_REPO="file://$W/origin/xo-space"

# ---- 1. mode detection -----------------------------------------------------
# helper: run resolve_repo_dir from $1 as if piped (sourcing lib.sh from $W,
# which has no server.py beside it, so the BASH_SOURCE probe fails as it does
# for stdin)
detect(){ ( cd "$1" && unset QUIRQ_APP_DIR && [ -z "${2:-}" ] || export QUIRQ_APP_DIR="$2"
            cd "$1"; source "$W/lib.sh" 2>/dev/null; resolve_repo_dir >/dev/null
            printf '%s|%s|%s' "$REPO_DIR" "$LAUNCH_DIR" "$MANAGED_CHECKOUT" ); }

mkdir -p "$W/ws"
check "piped from an empty workspace -> managed ./xo-space" \
      "$(detect "$W/ws")" "$W/ws/xo-space|$W/ws|1"

git clone -q -b main "file://$W/origin/xo-space" "$W/ws/xo-space"
check "piped from INSIDE the checkout -> that checkout, parent is workspace" \
      "$(detect "$W/ws/xo-space")" "$W/ws/xo-space|$W/ws|1"

mkdir -p "$W/elsewhere"
check "QUIRQ_APP_DIR wins over the inside-checkout probe" \
      "$(detect "$W/ws/xo-space" "$W/elsewhere/app")" "$W/elsewhere/app|$W/ws/xo-space|1"

# in-place: the script file itself sits beside server.py (own clone, so the
# copied script does not dirty the one the update test uses)
git clone -q -b main "file://$W/origin/xo-space" "$W/clone"
cp "$W/lib.sh" "$W/clone/install.sh"
inplace="$( cd "$W/clone" && source ./install.sh 2>/dev/null; resolve_repo_dir; printf '%s|%s|%s' "$REPO_DIR" "$LAUNCH_DIR" "$MANAGED_CHECKOUT" )"
check "./install.sh from a clone -> in-place, no update" \
      "$inplace" "$W/clone|$W/clone|0"

# ---- 2. fetch_repo ---------------------------------------------------------
cd "$W/origin/xo-space" && echo c >> server.py && $G commit -qam v2
UP="$(git -C "$W/origin/xo-space" rev-parse HEAD)"
fetch(){ ( cd "$W"; source "$W/lib.sh" 2>/dev/null; REPO_DIR="$1"; MANAGED_CHECKOUT=1; fetch_repo ); }

out="$(fetch "$W/ws/xo-space" 2>&1)"
check "clean checkout on main -> updated to upstream HEAD" \
      "$(git -C "$W/ws/xo-space" rev-parse HEAD)" "$UP"
case "$out" in *"Updating it"*) ok "…and said so";; *) bad "…and said so" "$out";; esac

git clone -q -b main "file://$W/origin/xo-space" "$W/dirty" && echo edit >> "$W/dirty/server.py"
before="$(git -C "$W/dirty" rev-parse HEAD)"
cd "$W/origin/xo-space" && echo d >> server.py && $G commit -qam v3
out="$(fetch "$W/dirty" 2>&1)"
check "dirty checkout -> untouched" "$(git -C "$W/dirty" rev-parse HEAD)" "$before"
check "dirty checkout -> edit preserved" "$(tail -n1 "$W/dirty/server.py")" "edit"
case "$out" in *"local changes"*) ok "…and said so";; *) bad "…and said so" "$out";; esac

git clone -q -b main "file://$W/origin/xo-space" "$W/devclone" && git -C "$W/devclone" checkout -q -b development
before="$(git -C "$W/devclone" rev-parse HEAD)"
cd "$W/origin/xo-space" && echo e >> server.py && $G commit -qam v4
out="$(fetch "$W/devclone" 2>&1)"
check "clean clone on another branch -> untouched" "$(git -C "$W/devclone" rev-parse HEAD)" "$before"
check "…still on its branch" "$(git -C "$W/devclone" rev-parse --abbrev-ref HEAD)" "development"
case "$out" in *"on branch development, not main"*"QUIRQ_SOURCE_REF=development"*) ok "…and named the override";; *) bad "…and named the override" "$out";; esac

# upstream grows a development branch one commit ahead of main
cd "$W/origin/xo-space" && git checkout -q -b development && echo f >> server.py && $G commit -qam dev1 && git checkout -q main
out="$(cd "$W"; source "$W/lib.sh" 2>/dev/null; SOURCE_REF=development; REPO_DIR="$W/devclone"; MANAGED_CHECKOUT=1; fetch_repo 2>&1)"
check "QUIRQ_SOURCE_REF=development -> that clone DOES update, to origin/development" \
      "$(git -C "$W/devclone" rev-parse HEAD)" "$(git -C "$W/origin/xo-space" rev-parse development)"

mkdir -p "$W/fresh"
out="$(fetch "$W/fresh/xo-space" 2>&1)"
check "no checkout yet -> cloned" "$(git -C "$W/fresh/xo-space" rev-parse HEAD 2>/dev/null)" "$(git -C "$W/origin/xo-space" rev-parse HEAD)"

# ---- 2b. generated .env: Composio callback (#176) ------------------------
cb(){ ( cd "$W"; source "$W/lib.sh" 2>/dev/null; PORT="$1"; export_connector_defaults; printf '%s' "$COMPOSIO_CALLBACK_URL" ); }
check "callback default follows the resolved port" \
      "$(unset COMPOSIO_CALLBACK_URL; cb 8080)" "http://127.0.0.1:8080/api/connectors/composio/callback"
check "an explicit callback wins over the default" \
      "$(COMPOSIO_CALLBACK_URL=https://space.example.com/api/connectors/composio/callback cb 5002)" \
      "https://space.example.com/api/connectors/composio/callback"
mkdir -p "$W/envrepo"
( cd "$W"; source "$W/lib.sh" 2>/dev/null; REPO_DIR="$W/envrepo"
  HOST=127.0.0.1 PORT=8080 STAGE=local UVICORN_RELOAD=false AGENT_NAME=claude_code
  QUIRQ_SKIP_BOOT_INSTALL=1 XO_PROJECTS_ROOT=/x AI_WORKSPACE_ROOT=/x QUIRQ_STATE_ROOT=/q
  QUIRQ_WATCHER_SOURCE_MODE=all; unset COMPOSIO_CALLBACK_URL
  export_connector_defaults; write_env_file ) >/dev/null
check "first-run .env records the callback" \
      "$(grep '^COMPOSIO_CALLBACK_URL=' "$W/envrepo/.env")" \
      "COMPOSIO_CALLBACK_URL=http://127.0.0.1:8080/api/connectors/composio/callback"
sed -i 's#^COMPOSIO_CALLBACK_URL=.*#COMPOSIO_CALLBACK_URL=https://edited.example.com/cb#' "$W/envrepo/.env"
check "a callback edited in .env is read back on the next run" \
      "$(cd "$W"; source "$W/lib.sh" 2>/dev/null; REPO_DIR="$W/envrepo"; unset COMPOSIO_CALLBACK_URL
         load_env_file; PORT=5002; export_connector_defaults; printf '%s' "$COMPOSIO_CALLBACK_URL")" \
      "https://edited.example.com/cb"

# ---- 2c. releases: install the newest tag, update only forward -------------
# Its own upstream, so the no-tags cases above keep testing the fallback.
R="$W/rel/xo-space"
mkdir -p "$R" && cd "$R"
git init -q -b main . && echo a > server.py && echo b > requirements.txt
$G add . && $G commit -qm r1 && $G tag -a v1.9.0 -m v1.9.0
echo 2 >> server.py && $G commit -qam r2 && $G tag -a v1.10.0 -m v1.10.0
V110="$(git rev-parse HEAD)"
echo 3 >> server.py && $G commit -qam r3 && $G tag -a v2.0.0-rc1 -m rc   # pre-release: never installed
echo 4 >> server.py && $G commit -qam "main tip"
rel(){ ( cd "$W"; source "$W/lib.sh" 2>/dev/null; SOURCE_REPO="file://$R"; SOURCE_REF="${2-}"
         REPO_DIR="$1"; MANAGED_CHECKOUT=1; fetch_repo ) 2>&1; }
at(){ git -C "$1" rev-parse HEAD; }
on(){ git -C "$1" rev-parse --abbrev-ref HEAD; }

mkdir -p "$W/relws"
out="$(rel "$W/relws/xo-space")"
check "fresh install -> newest release (v1.10.0: not v1.9.0, the rc, or the main tip)" "$(at "$W/relws/xo-space")" "$V110"
check "…as a detached tag checkout" "$(on "$W/relws/xo-space")" "HEAD"
case "$out" in *"Downloading Quirq v1.10.0"*) ok "…and named the release";; *) bad "…and named the release" "$out";; esac

out="$(rel "$W/relws/xo-space")"
check "re-run with no newer release -> unchanged" "$(at "$W/relws/xo-space")" "$V110"
case "$out" in *"on the latest release, v1.10.0"*) ok "…and said so";; *) bad "…and said so" "$out";; esac

# an old installer's clone: depth 1 on the main tip, ahead of every release
git clone -q --depth 1 -b main "file://$R" "$W/legacy/xo-space"
LEGACY="$(at "$W/legacy/xo-space")"
out="$(rel "$W/legacy/xo-space")"
check "main tip ahead of the latest release -> never moved back" "$(at "$W/legacy/xo-space")" "$LEGACY"
case "$out" in *"not behind the latest release (v1.10.0)"*) ok "…and said so";; *) bad "…and said so" "$out";; esac

# a full clone on main, sitting at v1.10.0
git clone -q -b main "file://$R" "$W/onmain/xo-space" && git -C "$W/onmain/xo-space" reset -q --hard "$V110"

cd "$R" && echo 5 >> server.py && $G commit -qam r5 && $G tag -a v1.11.0 -m v1.11.0
V111="$(git rev-parse HEAD)"
echo 6 >> server.py && $G commit -qam "past v1.11.0"
out="$(rel "$W/relws/xo-space")"
check "tag install + newer release -> moves to it, not to the main tip" "$(at "$W/relws/xo-space")" "$V111"
check "…still a detached tag checkout" "$(on "$W/relws/xo-space")" "HEAD"
case "$out" in *"Updating it to release v1.11.0"*) ok "…and said so";; *) bad "…and said so" "$out";; esac
rel "$W/onmain/xo-space" >/dev/null
check "main behind a newer release -> fast-forwards to the release, not the tip" "$(at "$W/onmain/xo-space")" "$V111"
check "…and stays on main" "$(on "$W/onmain/xo-space")" "main"

# the shallow main clone is behind once a release passes it
cd "$R" && echo 7 >> server.py && $G commit -qam r7 && $G tag -a v1.12.0 -m v1.12.0
V112="$(git rev-parse HEAD)"
rel "$W/legacy/xo-space" >/dev/null
check "shallow main clone -> moves forward once a newer release exists" "$(at "$W/legacy/xo-space")" "$V112"

mkdir -p "$W/tipws"
rel "$W/tipws/xo-space" main >/dev/null
check "QUIRQ_SOURCE_REF=main -> the main tip, as before" "$(at "$W/tipws/xo-space")" "$(git -C "$R" rev-parse main)"

# ---- 2d. the dependency stamp cowork-api.sh compares on start (#184) -------
mkdir -p "$W/deps/venv/bin" && echo fastapi > "$W/deps/requirements.txt"
printf '#!/bin/sh\n' > "$W/deps/venv/bin/python" && chmod +x "$W/deps/venv/bin/python"
( cd "$W"; source "$W/lib.sh" 2>/dev/null; uv(){ :; }; REPO_DIR="$W/deps"
  VENV_DIR="$W/deps/venv"; VENV_PYTHON="$W/deps/venv/bin/python"; sync_dependencies ) >/dev/null
check "sync_dependencies stamps the venv with the requirements.txt hash" \
      "$(cat "$W/deps/venv/.requirements.sha256" 2>/dev/null)" \
      "$(sha256sum "$W/deps/requirements.txt" | cut -d' ' -f1)"

# ---- 3. banner -------------------------------------------------------------
hint(){ ( cd "$W"; source "$W/lib.sh" 2>/dev/null; MANAGED_CHECKOUT="$1"; REPO_DIR="$2"; LAUNCH_DIR="$3"; SOURCE_REF="${4-}"; print_restart_hint ); }
m="$(hint 1 "$W/ws/xo-space" "$W/ws")"
case "$m" in *"cd $W/ws && $W/ws/xo-space/install.sh"*"curl -fsSL https://quirq.ai/install | sh"*) ok "managed banner: start-again + one-liner";; *) bad "managed banner" "$m";; esac
case "$m" in *QUIRQ_SOURCE_REF*) bad "managed banner on releases: no ref prefix" "$m";; *) ok "managed banner on releases: no ref prefix";; esac
m="$(hint 1 "$W/ws/xo-space" "$W/ws" main)"
case "$m" in *"| QUIRQ_SOURCE_REF=main sh"*) ok "managed banner on an explicit main: keeps the prefix";; *) bad "managed banner on an explicit main: keeps the prefix" "$m";; esac
m="$(hint 1 "$W/ws/xo-space" "$W/ws" development)"
# the ref must sit on `sh` (the bootstrap reads it), never on `curl`
case "$m" in *"| QUIRQ_SOURCE_REF=development sh"*) ok "managed banner on dev ref: prefix on sh";; *) bad "managed banner on dev ref: prefix on sh" "$m";; esac
case "$m" in *"QUIRQ_SOURCE_REF=development curl"*) bad "managed banner on dev ref: prefix must not be on curl" "$m";; *) ok "managed banner on dev ref: prefix not on curl";; esac
i="$(hint 0 "$W/ws/xo-space" "$W/ws/xo-space")"
case "$i" in *"cd $W/ws/xo-space && ./install.sh"*"Setup tab → Update"*) ok "in-place banner";; *) bad "in-place banner" "$i";; esac
case "$i" in *"git pull"*) bad "in-place banner: no git pull (it would skip releases and dependencies)" "$i";; *) ok "in-place banner: no git pull";; esac

# ---- 4. strictness: set -u / -e under bash 5, and shellcheck if present -----
bash -n "$W/lib.sh" && ok "bash -n (LF-normalised copy)" || bad "bash -n" "syntax error"
# the banner must never be the thing that runs the server
case "$(sed -n '/^print_restart_hint()/,/^}/p' "$W/lib.sh")" in *exec*) bad "print_restart_hint contains exec" "exec inside the hint function";; *) ok "print_restart_hint has no exec";; esac
case "$(sed -n '/^start_server()/,/^}/p' "$W/lib.sh")" in *'exec "$VENV_PYTHON" server.py'*) ok "start_server still owns the exec";; *) bad "start_server lost the exec" "";; esac
if command -v shellcheck >/dev/null; then shellcheck -S warning "$SRC" && ok "shellcheck (warning+)" || bad "shellcheck" "see above"; else echo "skip  shellcheck not installed"; fi
bash --version | head -1

printf '\n%d passed, %d failed  (sandbox: %s)\n' "$pass" "$fail" "$W"
rm -rf "$W"
[ "$fail" -eq 0 ]

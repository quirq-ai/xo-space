#!/usr/bin/env bash
# tests/install_sh_harness.sh — exercises install.sh's resolve_repo_dir,
# fetch_repo and print_restart_hint in isolation.
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

# ---- 3. banner -------------------------------------------------------------
hint(){ ( cd "$W"; source "$W/lib.sh" 2>/dev/null; MANAGED_CHECKOUT="$1"; REPO_DIR="$2"; LAUNCH_DIR="$3"; SOURCE_REF="${4:-main}"; print_restart_hint ); }
m="$(hint 1 "$W/ws/xo-space" "$W/ws")"
case "$m" in *"cd $W/ws && $W/ws/xo-space/install.sh"*"curl -fsSL https://quirq.ai/install | sh"*) ok "managed banner: start-again + one-liner";; *) bad "managed banner" "$m";; esac
case "$m" in *QUIRQ_SOURCE_REF*) bad "managed banner on main: no ref prefix" "$m";; *) ok "managed banner on main: no ref prefix";; esac
m="$(hint 1 "$W/ws/xo-space" "$W/ws" development)"
# the ref must sit on `sh` (the bootstrap reads it), never on `curl`
case "$m" in *"| QUIRQ_SOURCE_REF=development sh"*) ok "managed banner on dev ref: prefix on sh";; *) bad "managed banner on dev ref: prefix on sh" "$m";; esac
case "$m" in *"QUIRQ_SOURCE_REF=development curl"*) bad "managed banner on dev ref: prefix must not be on curl" "$m";; *) ok "managed banner on dev ref: prefix not on curl";; esac
i="$(hint 0 "$W/ws/xo-space" "$W/ws/xo-space")"
case "$i" in *"cd $W/ws/xo-space && ./install.sh"*"git pull --ff-only"*) ok "in-place banner";; *) bad "in-place banner" "$i";; esac

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

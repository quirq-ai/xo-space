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
# Physical, so paths compare equal to the ones install.sh resolves.
W="$(cd "$(mktemp -d)" && pwd -P)"
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
            cd "$1"; source "$W/lib.sh" 2>/dev/null; resolve_repo_dir >/dev/null 2>&1
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

# a shared temp dir (mode 1777) where someone left server.py and
# requirements.txt: the quirq.ai bootstrap saves this script into such a
# directory, and people run the one-liner from /tmp. Neither probe may adopt it.
mkdir -p "$W/sharedtmp" "$W/runfrom" && chmod 1777 "$W/sharedtmp"
echo planted > "$W/sharedtmp/server.py" && : > "$W/sharedtmp/requirements.txt"
cp "$W/lib.sh" "$W/sharedtmp/install.sh"
shared="$( cd "$W/runfrom" && unset QUIRQ_APP_DIR && source "$W/sharedtmp/install.sh" 2>/dev/null; resolve_repo_dir 2>/dev/null; printf '%s|%s|%s' "$REPO_DIR" "$LAUNCH_DIR" "$MANAGED_CHECKOUT" )"
check "script saved into a world-writable dir with planted files -> managed clone, not in-place" \
      "$shared" "$W/runfrom/xo-space|$W/runfrom|1"
check "piped while standing in a world-writable dir with planted files -> managed ./xo-space" \
      "$(detect "$W/sharedtmp")" "$W/sharedtmp/xo-space|$W/sharedtmp|1"
said="$( cd "$W/runfrom" && source "$W/sharedtmp/install.sh" 2>/dev/null; resolve_repo_dir 2>&1 >/dev/null )"
case "$said" in *"Ignoring the server.py in $W/sharedtmp: it is a shared temporary folder"*) ok "…and said why";; *) bad "…and said why" "$said";; esac
# the same files in a directory only this user can write are still a checkout
chmod 0755 "$W/sharedtmp"
check "same dir once it is not world-writable -> in-place again" \
      "$( cd "$W/runfrom" && source "$W/sharedtmp/install.sh" 2>/dev/null; resolve_repo_dir 2>/dev/null; printf '%s|%s' "$REPO_DIR" "$MANAGED_CHECKOUT" )" "$W/sharedtmp|0"

# ---- 1b. trust: physical paths, refusals that stop, identity ----------------
# run(): resolve_repo_dir with the script sourced from $2 (or piped, when $2
# is empty) while standing in $1. Prints REPO_DIR|LAUNCH_DIR|MANAGED on
# success; stderr lands in $W/err either way, and a refusal exits non-zero.
run(){ ( cd "$1" || exit 9; unset QUIRQ_APP_DIR
         # shellcheck disable=SC1090
         source "${2:-$W/lib.sh}" 2>/dev/null
         if [ -n "${3:-}" ]; then eval "$3"; fi
         resolve_repo_dir >/dev/null 2>"$W/err"
         printf '%s|%s|%s' "$REPO_DIR" "$LAUNCH_DIR" "$MANAGED_CHECKOUT" ); }
err(){ cat "$W/err"; }
expect_stop(){ # name, output, status, wanted text in stderr
    if [ "$3" -ne 0 ] && [ -z "$2" ] && case "$(err)" in *"$4"*) true;; *) false;; esac
    then ok "$1"; else bad "$1" "status $3, out '$2', err '$(err)'"; fi; }
commit_installer(){ cp "$W/lib.sh" "$1/install.sh" && $G -C "$1" add install.sh && $G -C "$1" commit -qm local; }

# symlinks resolve to the real checkout, not the link (a link's own mode is
# 0777 on Linux; on macOS /tmp is a link to the shared /private/tmp)
ln -s "$W/clone" "$W/clonelink"
check "./install.sh through a symlinked checkout -> in-place at the real path" \
      "$(run "$W/clonelink" "$W/clonelink/install.sh")" "$W/clone|$W/clonelink|0"
ln -s "$W/ws/xo-space" "$W/wslink"
check "piped from inside a symlinked checkout -> that checkout, real parent is workspace" \
      "$(run "$W/wslink")" "$W/ws/xo-space|$W/ws|1"
mkdir -p "$W/privtmp" && chmod 1777 "$W/privtmp" && ln -s "$W/privtmp" "$W/tmplink"
echo planted > "$W/privtmp/server.py" && : > "$W/privtmp/requirements.txt" && cp "$W/lib.sh" "$W/privtmp/install.sh"
check "script saved into a symlinked shared temp dir (macOS /tmp layout) -> managed clone" \
      "$(run "$W/runfrom" "$W/tmplink/install.sh")" "$W/runfrom/xo-space|$W/runfrom|1"
case "$(err)" in *"Ignoring the server.py in $W/privtmp: it is a shared temporary folder"*) ok "…and named the real dir";; *) bad "…and named the real dir" "$(err)";; esac
run "$W/privtmp" "$W/privtmp/install.sh" >/dev/null
check "script in a shared temp dir, run from it -> said once, not twice" "$(grep -c Ignoring "$W/err")" "1"

# a world-writable clone of the user's own (umask 000, or a drive that keeps
# no permissions) stops: falling back to managed mode would reset it to
# upstream or nest a second clone in it
mkdir -p "$W/ws2" && git clone -q -b main "file://$W/origin/xo-space" "$W/ws2/xo-space"
commit_installer "$W/ws2/xo-space" && chmod 0777 "$W/ws2/xo-space"
local_head="$(git -C "$W/ws2/xo-space" rev-parse HEAD)"
out="$(run "$W/ws2" "$W/ws2/xo-space/install.sh")"; rc=$?
expect_stop "cd ws && ./xo-space/install.sh on a 0777 clone -> stops with chmod" "$out" "$rc" "chmod go-w $W/ws2/xo-space"
out="$(run "$W/ws2/xo-space" "$W/ws2/xo-space/install.sh")"; rc=$?
expect_stop "cd xo-space && ./install.sh on a 0777 clone -> stops" "$out" "$rc" "Other users can write to $W/ws2/xo-space so"
out="$(run "$W/ws2/xo-space")"; rc=$?
expect_stop "piped from inside a 0777 clone -> stops" "$out" "$rc" "chmod go-w"
check "…the clone kept its local commit" "$(git -C "$W/ws2/xo-space" rev-parse HEAD)" "$local_head"
check "…and nothing was nested in it" "$(test -e "$W/ws2/xo-space/xo-space" && echo nested || echo none)" "none"
out="$(run "$W/ws2" "$W/ws2/xo-space/install.sh" 'filesystem_type(){ echo 9p; }')"; rc=$?
expect_stop "0777 clone on a drive without permissions (WSL /mnt/c) -> says clone into a Linux folder" "$out" "$rc" "Clone it into a Linux folder"

# only the leaf used to be checked: a non-sticky world-writable parent lets
# anyone rename the checkout away; a sticky one (like /tmp) does not
mkdir -p "$W/openparent" && git clone -q -b main "file://$W/origin/xo-space" "$W/openparent/xo-space"
commit_installer "$W/openparent/xo-space" && chmod 0777 "$W/openparent"
out="$(run "$W/openparent/xo-space" "$W/openparent/xo-space/install.sh")"; rc=$?
expect_stop "own 0755 clone under a 0777 parent -> stops, names the parent" "$out" "$rc" "$W/openparent, a folder above $W/openparent/xo-space"
chmod 1777 "$W/openparent"
check "…under a 1777 (sticky) parent -> in-place" \
      "$(run "$W/openparent/xo-space" "$W/openparent/xo-space/install.sh")" "$W/openparent/xo-space|$W/openparent/xo-space|0"

# group write: fine when the group is the owner's private group (umask 002),
# refused when it is a shared one
git clone -q -b main "file://$W/origin/xo-space" "$W/gw" && commit_installer "$W/gw" && chmod 0775 "$W/gw"
me_name="$(id -un)"
if [ "$(id -u)" -eq 0 ]; then shared_group=12345
else shared_group="$(id -Gn | tr ' ' '\n' | grep -vx "$me_name" | head -n 1)"; fi
if [ -n "$shared_group" ] && chgrp "$shared_group" "$W/gw" 2>/dev/null; then
    out="$(run "$W/gw" "$W/gw/install.sh")"; rc=$?
    expect_stop "0775 clone, shared group $shared_group -> stops" "$out" "$rc" "chmod go-w $W/gw"
else echo "skip  shared-group case: no group other than $me_name to test with"; fi
if chgrp "$me_name" "$W/gw" 2>/dev/null; then
    check "0775 clone, the owner's private group -> in-place" "$(run "$W/gw" "$W/gw/install.sh")" "$W/gw|$W/gw|0"
else echo "skip  private-group case: no group named $me_name"; fi

# an ACL can grant write without o+w; any ACL on the checkout is refused
git clone -q -b main "file://$W/origin/xo-space" "$W/acl" && commit_installer "$W/acl"
add_acl(){ # setfacl, else the same access ACL written as its raw xattr
    setfacl -m u:12345:rwx "$1" 2>/dev/null || python3 - "$1" 2>/dev/null <<'PY'
import os, struct, sys
entries = [(1, 7, 0xFFFFFFFF), (2, 7, 12345), (4, 5, 0xFFFFFFFF), (0x10, 7, 0xFFFFFFFF), (0x20, 5, 0xFFFFFFFF)]
os.setxattr(sys.argv[1], "system.posix_acl_access",
            struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in entries))
PY
}
if add_acl "$W/acl" && case "$(ls -ld "$W/acl")" in ??????????+*) true;; *) false;; esac; then
    out="$(run "$W/acl" "$W/acl/install.sh")"; rc=$?
    expect_stop "clone with an ACL -> stops, says setfacl -b" "$out" "$rc" "setfacl -b $W/acl"
else echo "skip  ACL case: this filesystem does not support ACLs"; fi

# another user's checkout: root may run that user's install.sh by path, but
# never adopts their directory just because it was standing in it
if [ "$(id -u)" -eq 0 ]; then
    git clone -q -b main "file://$W/origin/xo-space" "$W/other" && commit_installer "$W/other" && chown -R 12345 "$W/other"
    check "root runs another user's ./install.sh by path -> in-place" \
          "$(run "$W/other" "$W/other/install.sh")" "$W/other|$W/other|0"
    out="$(run "$W/other")"; rc=$?
    expect_stop "root pipes the one-liner inside another user's clone -> stops" "$out" "$rc" "$W/other belongs to 12345"
else
    echo "skip  other-owner cases: need root to create another user's directory"
fi

# the launch-dir probe adopts only a clone of SOURCE_REPO: managed mode resets
# the tree, so some other project with a server.py must never qualify
mkdir -p "$W/proj" && echo mine > "$W/proj/server.py" && : > "$W/proj/requirements.txt"
git -C "$W/proj" init -q -b main && git -C "$W/proj" remote add origin https://example.com/someone/else.git
$G -C "$W/proj" add . && $G -C "$W/proj" commit -qm mine
proj_head="$(git -C "$W/proj" rev-parse HEAD)"
check "piped from inside another project with server.py -> not adopted, managed ./xo-space" \
      "$(run "$W/proj")" "$W/proj/xo-space|$W/proj|1"
case "$(err)" in *"Not using $W/proj as the Quirq checkout: it is not a clone of"*) ok "…and said why";; *) bad "…and said why" "$(err)";; esac
check "…its commit untouched" "$(git -C "$W/proj" rev-parse HEAD)" "$proj_head"
ids="$( source "$W/lib.sh" 2>/dev/null
        for u in https://github.com/Quirq-AI/xo-space.git git@github.com:quirq-ai/xo-space ssh://git@github.com/quirq-ai/xo-space/; do repo_identity "$u"; echo; done | sort -u )"
check "https, ssh and .git spellings of one repo are the same clone" "$ids" "github.com/quirq-ai/xo-space"

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

# ---- 3b. port probe: only "in use" says "in use" ---------------------------
# A stand-in interpreter exits with the code under test, the way the real
# probe would (3 = in use; 1 = Python died, e.g. on an ImportError).
port(){ printf '#!/bin/sh\nexit %s\n' "$1" > "$W/fakepy" && chmod +x "$W/fakepy"
        # shellcheck disable=SC2034  # read by ensure_port_available
        ( cd "$W" || exit 9; source "$W/lib.sh" 2>/dev/null; VENV_PYTHON="$W/fakepy"; ensure_port_available 127.0.0.1 5002 ) 2>&1; }
case "$(port 3)" in *"Port 5002 is already in use"*) ok "probe exit 3 -> port in use";; *) bad "probe exit 3 -> port in use" "$(port 3)";; esac
case "$(port 1)" in *"Could not verify that port 5002 is free"*) ok "probe exit 1 (crash) -> could not verify, not 'in use'";; *) bad "probe exit 1 (crash) -> could not verify" "$(port 1)";; esac
case "$(port 0)" in "") ok "probe exit 0 -> silent";; *) bad "probe exit 0 -> silent" "$(port 0)";; esac

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

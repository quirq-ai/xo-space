#!/usr/bin/env bash
# tests/uninstall_sh_harness.sh — exercises uninstall.sh's resolve_repo_dir,
# resolve_roots, remove_path guards and remove_checkout in isolation, plus
# one full --yes run against a fabricated managed install.
#
# Sources everything except the final `main "$@"`, against temp directories.
# lsof and docker are shadowed with no-op fakes so nothing on the real
# machine is inspected, killed, or brought down. No network, no venv.
# Run directly (bash tests/uninstall_sh_harness.sh) or via
# tests/test_uninstall_sh.py. UNINSTALL_SH=<path> points it elsewhere.
set -u
SRC="${UNINSTALL_SH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/uninstall.sh}"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
pass=0; fail=0
ok()   { pass=$((pass+1)); printf 'PASS  %s\n' "$1"; }
bad()  { fail=$((fail+1)); printf 'FAIL  %s\n      got: %s\n' "$1" "$2"; }
check(){ [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2  (want: $3)"; }

# ---- functions-only copy -------------------------------------------------
last="$(tail -n 1 "$SRC" | tr -d '\r' | sed 's/[[:space:]]*$//')"
[ "$last" = 'main "$@"' ] || { echo "unexpected last line of $SRC"; exit 1; }
tr -d '\r' < "$SRC" | sed '$d' > "$W/lib.sh"

# ---- fakes: never touch the real machine ---------------------------------
mkdir -p "$W/bin"
printf '#!/bin/sh\nexit 1\n' > "$W/bin/lsof";   chmod +x "$W/bin/lsof"
printf '#!/bin/sh\nexit 1\n' > "$W/bin/docker"; chmod +x "$W/bin/docker"
export PATH="$W/bin:$PATH"

# ---- a fabricated managed install ----------------------------------------
make_install() {  # $1 = workspace dir
    mkdir -p "$1/xo-space/venv" "$1/.quirq/watcher" "$1/projectA" "$1/.xo"
    echo srv > "$1/xo-space/server.py"
    echo req > "$1/xo-space/requirements.txt"
    echo "PORT=59999" > "$1/xo-space/.env"
    echo keep > "$1/projectA/notes.md"
    echo state > "$1/.quirq/state.json"
}

# ---- 1. resolve_repo_dir ---------------------------------------------------
make_install "$W/ws1"
r1="$( cd "$W/ws1" && source "$W/lib.sh" 2>/dev/null
       resolve_repo_dir; resolve_workspace_dir; printf '%s|%s' "$REPO_DIR" "$WORKSPACE_DIR" )"
check "from the workspace -> ./xo-space checkout, workspace above" \
      "$r1" "$W/ws1/xo-space|$W/ws1"

r2="$( cd "$W/ws1/xo-space" && source "$W/lib.sh" 2>/dev/null
       resolve_repo_dir; resolve_workspace_dir; printf '%s|%s' "$REPO_DIR" "$WORKSPACE_DIR" )"
check "from inside the checkout -> same answer" \
      "$r2" "$W/ws1/xo-space|$W/ws1"

mkdir -p "$W/empty"
r3="$( cd "$W/empty" && source "$W/lib.sh" 2>/dev/null
       resolve_repo_dir; printf '%s' "${REPO_DIR:-none}" )"
check "nothing installed -> no checkout found" "$r3" "none"

# ---- 2. resolve_roots: roots.env beats .env, shell beats both --------------
echo "XO_PROJECTS_ROOT=$W/ws1/elsewhere" > "$W/ws1/.quirq/roots.env"
r4="$( cd "$W/ws1" && source "$W/lib.sh" 2>/dev/null
       unset XO_PROJECTS_ROOT QUIRQ_STATE_ROOT || true
       resolve_repo_dir; resolve_workspace_dir; resolve_roots
       printf '%s|%s' "$PROJECTS_ROOT" "$STATE_ROOT" )"
check "roots.env saved root wins; state root defaults beside it" \
      "$r4" "$W/ws1/elsewhere|$W/ws1/.quirq"

r5="$( cd "$W/ws1" && source "$W/lib.sh" 2>/dev/null
       export XO_PROJECTS_ROOT="$W/shellroot"
       resolve_repo_dir; resolve_workspace_dir; resolve_roots
       printf '%s' "$PROJECTS_ROOT" )"
check "an exported shell root beats roots.env" "$r5" "$W/shellroot"
rm -f "$W/ws1/.quirq/roots.env"

# ---- 3. remove_path guards -------------------------------------------------
g1="$( source "$W/lib.sh" 2>/dev/null; ( remove_path x "/" ) 2>&1 || true )"
case "$g1" in *"refusing to remove /"*) ok "remove_path refuses /";;
              *) bad "remove_path refuses /" "$g1";; esac

mkdir -p "$W/fakehome"
g2="$( source "$W/lib.sh" 2>/dev/null; export HOME="$W/fakehome"
       ( remove_path x "$W/fakehome" ) 2>&1 || true )"
case "$g2" in *"home directory"*) ok "remove_path refuses \$HOME";;
              *) bad "remove_path refuses \$HOME" "$g2";; esac

mkdir -p "$W/relcase/relative/path"
g3="$( cd "$W/relcase" && source "$W/lib.sh" 2>/dev/null
       ( remove_path x "relative/path" ) 2>&1 || true )"
case "$g3" in *"relative path"*) ok "remove_path refuses relative paths";;
              *) bad "remove_path refuses relative paths" "$g3";; esac

echo f > "$W/dryfile"
( source "$W/lib.sh" 2>/dev/null; DRY_RUN=1; remove_path x "$W/dryfile" )
[ -f "$W/dryfile" ] && ok "dry run removes nothing" || bad "dry run removes nothing" "file gone"

# ---- 4. remove_checkout: in-place strips, managed removes ------------------
make_install "$W/ws2"
( cd "$W/ws2/xo-space" && source "$W/lib.sh" 2>/dev/null
  REPO_DIR="$W/ws2/xo-space"; WORKSPACE_DIR="$REPO_DIR"; PROJECTS_ROOT="$REPO_DIR"
  remove_checkout )
inplace_state="$([ -f "$W/ws2/xo-space/server.py" ] && echo kept || echo gone)|$([ -d "$W/ws2/xo-space/venv" ] && echo venv || echo novenv)|$([ -f "$W/ws2/xo-space/.env" ] && echo env || echo noenv)"
check "in-place: checkout kept, venv and .env stripped" "$inplace_state" "kept|novenv|noenv"

make_install "$W/ws3"
( cd "$W/ws3" && source "$W/lib.sh" 2>/dev/null
  REPO_DIR="$W/ws3/xo-space"; WORKSPACE_DIR="$W/ws3"; PROJECTS_ROOT="$W/ws3"
  remove_checkout )
[ ! -e "$W/ws3/xo-space" ] && ok "managed: checkout removed wholesale" \
                           || bad "managed: checkout removed wholesale" "still there"

# managed but dirty: kept (venv still stripped)
make_install "$W/ws4"
( cd "$W/ws4/xo-space" && git init -q . && git add -A >/dev/null 2>&1
  git -c user.name=t -c user.email=t@t commit -qm x >/dev/null 2>&1
  echo dirty >> server.py )
( cd "$W/ws4" && source "$W/lib.sh" 2>/dev/null
  REPO_DIR="$W/ws4/xo-space"; WORKSPACE_DIR="$W/ws4"; PROJECTS_ROOT="$W/ws4"
  remove_checkout )
dirty_state="$([ -f "$W/ws4/xo-space/server.py" ] && echo kept || echo gone)|$([ -d "$W/ws4/xo-space/venv" ] && echo venv || echo novenv)"
check "managed but dirty: checkout kept, venv stripped" "$dirty_state" "kept|novenv"

# ---- 5. one full run against a fabricated install --------------------------
# The script must run from a COPY inside the fabricated checkout: run by its
# real path, its BASH_SOURCE probe would (correctly) resolve the developer's
# own checkout — which is exactly what a real user's invocation looks like,
# and exactly what this harness must never touch.
make_install "$W/ws5"
cp "$SRC" "$W/ws5/xo-space/uninstall.sh"
mkdir -p "$W/home5/.argus" "$W/home5/.xo-cowork"
echo db > "$W/home5/.argus/argus.db"
mkdir -p "$W/tmp5"
full="$( cd "$W/ws5" && HOME="$W/home5" PORT=59999 QUIRQ_DAEMON_TMPDIR="$W/tmp5" bash ./xo-space/uninstall.sh --yes 2>&1 )" || {
    bad "full --yes run exits 0" "$full"; }
state5="$([ -d "$W/ws5/projectA" ] && echo projects || echo lost)"
state5="$state5|$([ -e "$W/ws5/xo-space" ] && echo checkout || echo nocheckout)"
state5="$state5|$([ -e "$W/ws5/.quirq" ] && echo quirq || echo noquirq)"
state5="$state5|$([ -e "$W/ws5/.xo" ] && echo xo || echo noxo)"
state5="$state5|$([ -e "$W/home5/.argus" ] && echo argus || echo noargus)"
check "full run: projects kept; checkout, .quirq, .xo, ~/.argus removed" \
      "$state5" "projects|nocheckout|noquirq|noxo|noargus"

cp "$SRC" "$W/ws5/uninstall-copy.sh"
again="$( cd "$W/ws5" && HOME="$W/home5" QUIRQ_DAEMON_TMPDIR="$W/tmp5" bash ./uninstall-copy.sh --yes >/dev/null 2>&1 && echo ok || echo err )"
check "second run is graceful (idempotent)" "$again" "ok"

# ---- summary ---------------------------------------------------------------
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]

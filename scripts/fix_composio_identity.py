#!/usr/bin/env python3
"""Repoint a Space's Composio stores from a Space-scoped id to an XO account id.

**The problem this fixes.** Composio used to be addressed by ``XO_SPACE_ID``, so every
connection a Space made was filed under that Space's UUID. Addressing it by the XO
account id came later. A Space left over from the old scheme carries the UUID where the
account id belongs::

    identity.json   {"account_id": "f5484ec9-1acd-4f10-a6de-9f9882ff67b3"}   <- a space id
    sessions.json   {"account_id": "f5484ec9-...", "session": "trs_..."}

and it is sticky: ``resolve()`` returns the cached value first, and the Composio project
still holds connections under that UUID, so reading the project back only re-adopts it.
Nothing self-heals. This script breaks that loop.

**What it does**, all of it atomic and all of it reversible by reconnecting:

1. writes the real account id into ``identity.json``;
2. re-stamps ``sessions.json`` with it, drops the session minted for the old id, and
   **keeps the proxy tokens** so agents' MCP URLs survive;
3. clears the ``connected_account_ids`` pins in ``space_scope.json`` that point at
   connections owned by the old id, leaving each toolkit's ``enabled`` flag alone.

Afterwards, connecting any tool in that Space creates the connection under the new
account id on that API key — which is the "new user on that key" this produces. The old
connections are not migrated: Composio has no route to reassign a connected account to a
different ``user_id``, so they stay where they are and are reconnected once.

**Dry run by default.** It prints the plan and changes nothing until ``--apply``.

Usage, from the xo-space checkout in the Space to fix::

    venv/bin/python scripts/fix_composio_identity.py                       # inspect
    venv/bin/python scripts/fix_composio_identity.py --account-id user_XXX --apply
    venv/bin/python scripts/fix_composio_identity.py --apply               # ask XO for it

Standalone: stdlib only for the file surgery, so it runs in a Space whose checkout
predates any of this. The optional lookups (the XO account id, the project's connections)
use the checkout's own modules when they import, and are skipped when they do not.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

#: An XO account id looks like a Clerk id. A Space-scoped leftover is a bare UUID, which
#: is exactly what this script exists to replace, so the two must not be confused.
ACCOUNT_RE = re.compile(r"^user_[A-Za-z0-9]+$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def store_dir() -> Path:
    configured = (os.getenv("COMPOSIO_STORE_DIR", "") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".config" / "composio"


def read_json(path: Path) -> Optional[dict]:
    try:
        with path.open(encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"  ! {path.name} is unreadable ({exc}); leaving it alone.")
        return None
    return doc if isinstance(doc, dict) else None


def write_json(path: Path, doc: dict, *, mode: Optional[int] = None) -> None:
    """Replace in one os.replace, so a reader never sees a half-written store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def looks_stale(value: Any, space_id: Optional[str]) -> bool:
    """Whether a stored account id is really a Space id wearing the wrong hat."""
    if not isinstance(value, str) or not value:
        return False
    if ACCOUNT_RE.match(value):
        return False
    return bool(UUID_RE.match(value)) or (space_id is not None and value == space_id)


def account_from_xo() -> Optional[str]:
    """Ask xo-swarm-api who this backend's credential belongs to. None if it cannot."""
    try:
        sys.path.insert(0, os.getcwd())
        import asyncio

        from services.swarm_api import auth as swarm_auth
    except Exception as exc:  # noqa: BLE001 — an old checkout, or no deps
        print(f"  - could not ask xo-swarm-api from this checkout ({exc}).")
        return None
    try:
        res = asyncio.run(swarm_auth.get_user_id())
    except Exception as exc:  # noqa: BLE001
        print(f"  - the xo-swarm-api lookup failed ({exc}).")
        return None
    if not getattr(res, "ok", False):
        why = "this Space holds no XO credential" if getattr(res, "unauthenticated", False) \
            else getattr(res, "detail", "") or "the swarm did not answer"
        print(f"  - xo-swarm-api did not supply an account id: {why}.")
        return None
    data = res.data if isinstance(res.data, dict) else {}
    return str(data.get("user_id") or "").strip() or None


def project_connections() -> Optional[list[tuple[str, str, str]]]:
    """``(user_id, toolkit, connected_account_id)`` for the key's whole project."""
    try:
        sys.path.insert(0, os.getcwd())
        from services.cowork_agent.connectors.composio import byo_key
        from services.cowork_agent.connectors.composio import client as c

        if not byo_key.configured():
            return None
        page = c._sdk().connected_accounts.list()
        items = c._attr(page, "items", default=page) or []
        return [(str(c._attr(it, "user_id") or "?"),
                 str(c._attr(it, "toolkit", "slug") or "?"),
                 str(c._attr(it, "id") or "?")) for it in items]
    except Exception as exc:  # noqa: BLE001 — informational only
        print(f"  - could not read the Composio project ({exc}).")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--account-id", help="the XO account id to write (user_...). "
                                         "Omitted: ask xo-swarm-api for it.")
    ap.add_argument("--apply", action="store_true",
                    help="write the changes. Without it, nothing is touched.")
    ap.add_argument("--force", action="store_true",
                    help="repoint even when the stored id does not look stale.")
    args = ap.parse_args()

    root = store_dir()
    space_id = (os.getenv("XO_SPACE_ID", "") or "").strip() or None
    identity_path, sessions_path = root / "identity.json", root / "sessions.json"
    scope_path = root / "space_scope.json"

    print(f"store dir      {root}")
    print(f"XO_SPACE_ID    {space_id or '(unset)'}")
    if not root.is_dir():
        print("\nNothing to fix: this Space has no Composio store yet.")
        return 0

    identity, sessions, scope = (read_json(p) for p in (identity_path, sessions_path, scope_path))
    stored = [d.get("account_id") for d in (identity, sessions) if isinstance(d, dict)]
    current = next((s for s in stored if isinstance(s, str) and s), None)
    print(f"identity.json  account_id = {(identity or {}).get('account_id', '(none)')}")
    print(f"sessions.json  account_id = {(sessions or {}).get('account_id', '(none)')}"
          f"   session = {(sessions or {}).get('session', '(none)')}")

    stale = looks_stale(current, space_id)
    print(f"\nverdict        {'STALE — a Space id is sitting where the account id belongs'
                             if stale else 'the stored id already looks like an XO account id'}")
    if not stale and not args.force:
        print("Nothing to do. Re-run with --force to repoint it anyway.")
        return 0

    print("\nresolving the account id to write:")
    account = (args.account_id or "").strip() or account_from_xo()
    if not account:
        print("\nNo account id. Pass --account-id user_XXX (copy it from the "
              "identity.json of a Space that already resolved one).")
        return 2
    if not ACCOUNT_RE.match(account) and not args.force:
        print(f"\n{account!r} is not an XO account id (expected user_...). "
              f"Re-run with --force if that is deliberate.")
        return 2
    print(f"  -> {account}")

    rows = project_connections()
    if rows:
        print("\nconnections in this key's Composio project:")
        for uid, toolkit, cid in rows:
            mark = "  (orphaned by this change — reconnect once)" if uid == current else ""
            print(f"  {uid}  {toolkit}  {cid}{mark}")

    plan = [f"identity.json  account_id -> {account}"]
    if isinstance(sessions, dict):
        plan.append(f"sessions.json  account_id -> {account}, space_id -> "
                    f"{space_id or 'null'}, session -> null (proxy tokens kept)")
    pinned = sum(len(e.get("connected_account_ids") or [])
                 for e in (scope or {}).get("toolkits", {}).values() if isinstance(e, dict))
    if pinned:
        plan.append(f"space_scope.json  clear {pinned} pin(s) owned by the old id "
                    f"(each toolkit stays enabled)")
    print("\nplan:")
    for step in plan:
        print(f"  - {step}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write it.")
        return 0

    # 0644 for the two that are not secrets, 0600 for sessions.json, matching what the
    # app itself writes; mkstemp would otherwise leave everything at 0600.
    write_json(identity_path, {"version": 1, "account_id": account,
                               "resolved_at": __import__("time").time()}, mode=0o644)
    print(f"\nwrote {identity_path}")

    if isinstance(sessions, dict):
        sessions["account_id"] = account
        sessions["space_id"] = space_id
        # Dropped, not kept: it was minted against the old id's connections. The proxy
        # tokens stay, so agents keep the MCP URL they already hold.
        sessions["session"] = None
        write_json(sessions_path, sessions, mode=0o600)
        print(f"wrote {sessions_path}")

    if pinned and isinstance(scope, dict):
        for entry in scope.get("toolkits", {}).values():
            if isinstance(entry, dict):
                entry["connected_account_ids"] = []
        scope["space_id"] = space_id
        write_json(scope_path, scope, mode=0o644)
        print(f"wrote {scope_path}")

    print("\nDone. Restart xo-space, open the Connectors tab and connect a tool: the "
          "connection is created under the new account id on this API key.")
    print("Note: sharing with another Space also needs that Space to hold the SAME "
          "Composio API key — the same account id in a different project is a different "
          "set of connections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

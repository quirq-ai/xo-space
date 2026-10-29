"""XO's agy plugin: written into agy's global customization root
(``~/.gemini/config/plugins/xo/``) before a session starts, and by agy's
``setup.sh`` (storm ch 04 §2; owner decision §0.14: installed with backups).

The plugin is the only thing XO puts into the person's agy setup:

* ``hooks.json``: XO's hook (``xo_agy_hook.py``) for every tool call and the
  turn's lifecycle; it does nothing outside an XO session;
* ``mcp_config.json``: one relay (``xo_agy_mcp.py <name>``) per MCP server a
  session has asked for (XO's comms, Composio); names are only ever added;
* the two scripts and ``plugin.json``.

No allow rule goes into the person's ``settings.json``: the hook answers
each call. The runtime installs the plugin itself, at each session's start,
because ``setup.sh`` only runs in a Space whose agent is agy, and a chat can
run on agy in any Space.

Each write keeps a backup of the files it replaces (under XO's state, the
last few), goes through a temp file and a rename, and is read back and
compared byte for byte. One writer at a time (a lock file); a plugin already
in place is left untouched.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from services.cowork_agent.adapters.antigravity.runtime.env import config_root

PLUGIN_NAME = "xo"
SOURCE = Path(__file__).resolve().parent / "plugin"
SCRIPTS = ("xo_agy_hook.py", "xo_agy_mcp.py")
#: How long agy waits for XO's answer to a tool call: as long as the person
#: needs (held 11 minutes live; agy's default is 30 s).
HOOK_TIMEOUT_S = 7 * 24 * 3600
LIFECYCLE_TIMEOUT_S = 60
LIFECYCLE = ("SessionStart", "PreInvocation", "Stop")
#: The names agy gives a plugin's MCP server: ``<plugin>_<server>``.
AGY_PREFIX = f"{PLUGIN_NAME}_"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
BACKUPS_KEPT = 3


class InstallFailed(RuntimeError):
    pass


def plugin_dir(root: Path | None = None) -> Path:
    return (root or config_root()) / "plugins" / PLUGIN_NAME


def agy_server_name(server: str) -> str:
    return AGY_PREFIX + server


def spec_server_name(agy_name: str) -> str | None:
    """The spec's server for agy's name of one of this plugin's servers."""
    return agy_name[len(AGY_PREFIX):] if agy_name.startswith(AGY_PREFIX) else None


def _python() -> str:
    """The interpreter agy runs the scripts with: the system's ``python3``
    (the scripts are stdlib only), else the one XO runs on."""
    found = shutil.which("python3")
    return found or sys.executable


def files(names: Iterable[str], *, where: Path, python: str) -> dict[str, bytes]:
    """The plugin's files for a set of server names, as bytes."""
    def hook(event: str, timeout: int) -> dict:
        command = f"{shlex.quote(python)} -I {shlex.quote(str(where / 'xo_agy_hook.py'))} {event}"
        return {"type": "command", "command": command, "timeout": timeout}

    hooks = {PLUGIN_NAME: {"PreToolUse": [{"matcher": "*", "hooks": [hook("PreToolUse", HOOK_TIMEOUT_S)]}],
                           **{event: [hook(event, LIFECYCLE_TIMEOUT_S)] for event in LIFECYCLE}}}
    servers = {name: {"command": python, "args": ["-I", str(where / "xo_agy_mcp.py"), name]}
               for name in sorted(set(names))}
    out = {
        "plugin.json": json.dumps({"name": PLUGIN_NAME, "description": "XO Space's link to the agy sessions it "
                                   "runs. It does nothing in any other agy session.",
                                   "author": {"name": "XO Space"}}, indent=2) + "\n",
        "hooks.json": json.dumps(hooks, indent=2) + "\n",
        "mcp_config.json": json.dumps({"mcpServers": servers}, indent=2) + "\n",
    }
    blobs = {k: v.encode() for k, v in out.items()}
    for script in SCRIPTS:
        blobs[script] = (SOURCE / script).read_bytes()
    return blobs


def installed_names(where: Path) -> set[str]:
    try:
        data = json.loads((where / "mcp_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return {n for n in servers if isinstance(n, str) and _NAME.fullmatch(n)} if isinstance(servers, dict) else set()


def _digest(blobs: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name in sorted(blobs):
        h.update(name.encode() + b"\0" + blobs[name] + b"\0")
    return h.hexdigest()


def _backup_root() -> Path:
    from services.storage.paths import quirq_state_dir
    return quirq_state_dir() / "backups" / "agy-plugin"


def ensure(names: Iterable[str] = (), *, root: Path | None = None, backups: Path | None = None) -> str:
    """Make sure the plugin is in place with relays for ``names`` (and every
    name already there); returns the digest of what is installed. Blocking.
    Raises ``InstallFailed`` with what to do."""
    wanted = set(names)
    bad = sorted(n for n in wanted if not _NAME.fullmatch(n))
    if bad:
        raise InstallFailed(f"not an MCP server name agy can take: {', '.join(bad)}")
    where = plugin_dir(root)
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        lock_path = where.parent / f".{PLUGIN_NAME}.lock"
        with open(lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            blobs = files(wanted | installed_names(where), where=where, python=_python())
            current = {name: _read(where / name) for name in blobs}
            if all(current[name] == data for name, data in blobs.items()):
                return _digest(blobs)
            if any(v is not None for v in current.values()):
                _backup(where, backups or _backup_root())
            where.mkdir(exist_ok=True)
            for name, data in blobs.items():
                if current[name] != data:
                    _write(where / name, data, executable=name in SCRIPTS)
            for name, data in blobs.items():
                if _read(where / name) != data:
                    raise InstallFailed(f"agy's XO plugin file {where / name} doesn't read back as written")
            return _digest(blobs)
    except OSError as exc:
        raise InstallFailed(f"can't install XO's agy plugin in {where}: {exc.strerror or exc}") from exc


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write(path: Path, data: bytes, *, executable: bool) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o755 if executable else 0o644)
    os.replace(tmp, path)


def _backup(where: Path, root: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = root / stamp
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(where, target, symlinks=True)
    kept = sorted(p for p in root.iterdir() if p.is_dir())
    for old in kept[:-BACKUPS_KEPT]:
        with contextlib.suppress(OSError):
            shutil.rmtree(old)


def main(argv: list[str]) -> int:
    """``python -m …runtime.install [name …]``: what ``setup.sh`` runs."""
    try:
        digest = ensure(argv)
    except InstallFailed as exc:
        print(f"XO's agy plugin: {exc}", file=sys.stderr)
        return 1
    print(f"XO's agy plugin is in place at {plugin_dir()} ({digest[:12]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

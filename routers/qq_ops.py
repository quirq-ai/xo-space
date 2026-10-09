"""Routes whose work runs as a qq command: both sides of that contract.

Server side, qq_first(): the route runs ``qq <command> --json`` and returns its answer. Only when
qq did not run the operation at all (qq or the checkout's Python is missing: QQNotRun) does the
route run its in-process code instead. When qq started but gave no answer (timed out, crashed:
QQNoAnswer) the operation may be half done, so it is reported as a 502 and never run twice.

Process side, ``python -m routers.qq_ops OP ...``: what those commands run
(infra/commands/{projects,share,revoke,apply,backup,restore}.sh). It loads the server's settings
and calls the same code the route falls back to. With --json it prints one object: the route's
answer (exit 0; a list answer is {"results": [...]}), or {"code", "message", "status", "detail"}
where status and detail are the HTTP error the route would have raised (exit 1).

Values always arrive as --name=VALUE, so a project called "--all" stays a value.
Design: infra/commands/DESIGN.md.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import HTTPException
from starlette.responses import Response

from services import qq_runner

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── Server side ──────────────────────────────────────────────────────────────


async def qq_first(args: list[str], timeout: float, in_process: Callable[[], Awaitable],
                   *, results: bool = False):
    """The route's answer from ``qq <args> --json``, or from ``in_process()`` when qq did not run.

    ``results``: the answer is a list, which the command wraps as {"results": [...]}."""
    try:
        res = await qq_runner.run_qq(args, timeout=timeout)
    except qq_runner.QQNoAnswer as exc:
        raise HTTPException(status_code=502, detail={"code": "qq_no_answer", "message": str(exc)})
    except qq_runner.QQNotRun as exc:
        print(f"⚠️ qq {args[0]} did not run, running it in-process ({exc})")
        return await in_process()
    if res.returncode != 0:
        status = res.data.get("status")
        if isinstance(status, int) and 400 <= status < 600:
            raise HTTPException(status_code=status, detail=res.data.get("detail"))
        raise HTTPException(status_code=502, detail={
            "code": res.data.get("code") or "qq_failed",
            "message": res.data.get("message") or f"qq {args[0]} failed"})
    return res.data.get("results", []) if results else res.data


# ── Process side ─────────────────────────────────────────────────────────────


def _settings_file(configured: str, new: Path, old: Path) -> str:
    path = Path(configured).expanduser() if configured else new
    if path == new and not path.is_file() and old.is_file():
        return str(old)
    return str(path)


def _load_settings() -> None:
    """The settings server.py loads at start-up, in the same order: shell > roots.env > .env,
    then runtime.env and secrets over them. Under the server the environment is inherited and
    this re-reads the same files. TODO(cleanup): one loader shared with server.py."""
    from dotenv import dotenv_values, load_dotenv

    from services.storage.layout import secrets_dir, settings_dir

    dotenv = REPO_ROOT / ".env"
    for key, value in (dotenv_values(dotenv) if dotenv.is_file() else {}).items():
        if (value or "").strip() and not os.environ.get(key, "").strip():
            os.environ.pop(key, None)   # a blank export must not hide the .env value
    shell = frozenset(os.environ)
    load_dotenv(dotenv)

    def state_root() -> Path:
        return Path((os.getenv("QUIRQ_STATE_ROOT", "") or "").strip() or Path.home() / ".quirq").expanduser()

    roots = state_root() / settings_dir().name / "roots.env"
    if not roots.is_file():
        roots = state_root() / "roots.env"
    values = dotenv_values(roots) if roots.is_file() else {}
    for key in ("XO_PROJECTS_ROOT", "QUIRQ_STATE_ROOT"):
        value = (values.get(key) or "").strip()
        if value and key not in shell:
            os.environ[key] = value
    load_dotenv(_settings_file((os.getenv("QUIRQ_RUNTIME_FILE", "") or "").strip(),
                               settings_dir() / "runtime.env", state_root() / "runtime.env"), override=True)
    secrets = (os.getenv("QUIRQ_SECRETS_FILE", "") or "").strip()
    if secrets:
        load_dotenv(_settings_file(secrets, secrets_dir() / "secrets.env", state_root() / "secrets.env"),
                    override=True)


@contextlib.contextmanager
def _backup_lock():
    """One backup or restore at a time across processes. The services' per-project asyncio
    locks only cover one process; every qq run is its own process."""
    from services.storage.layout import locks_dir

    folder = locks_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "xo-projects-sync.lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)   # waits, like the asyncio lock did
        yield


async def _projects_add(a):
    from routers.cowork_agent.bff import project_management as r
    return await r.clone_project_in_process(a.project, a.url)


async def _projects_remove(a):
    from routers.cowork_agent.bff import project_management as r
    if a.confirm is None:
        return await r.removal_status(a.project)   # a dry run: what removing would find
    return await r.remove_project_in_process(a.project, a.confirm)


async def _share(a):
    from routers.cowork_agent.bff import project_sharing as r
    return await r.share_project_in_process(a.project, r.ShareBody(workspace_id=a.space))


async def _revoke(a):
    from routers.cowork_agent.bff import project_sharing as r
    return await r.revoke_project_in_process(a.project, r.ShareBody(workspace_id=a.space))


async def _apply(a):
    from routers.cowork_agent.bff import project_sharing as r
    return await r.apply_project_in_process(a.project)


async def _backup(a):
    from routers.cowork_agent import xo_projects_sync as r
    if a.list:
        return await r.list_projects_in_repo()
    body = r.BackupBody(note=a.note)
    with _backup_lock():
        if a.all:
            return await r.backup_all_projects_in_process(body)
        return await r.backup_project_in_process(a.project, body)


async def _restore(a):
    from routers.cowork_agent import xo_projects_sync as r
    with _backup_lock():
        if a.all:
            pins = dict(p.split("=", 1) for p in a.pin)
            return await r.restore_all_projects_in_process(
                r.RestoreAllBody(snapshot_id_map=pins or None, force=a.force))
        return await r.restore_project_in_process(a.project, r.RestoreBody(snapshot_id=a.snapshot, force=a.force))


def _say(op: str, d: dict) -> str:
    """One human line (or a few) for an answer."""
    if op == "projects-add":
        return f"added {d.get('project_id')}" + (f" ({d['warning']})" if d.get("warning") else "")
    if op == "projects-remove":
        if "removed" in d:
            return f"removed {d.get('project_id')}"
        if not d.get("can_remove"):
            return f"{d.get('project_id')} cannot be removed: " + "; ".join(b["message"] for b in d.get("blockers", []))
        return f"{d.get('project_id')} can be removed. Run again with --yes to delete this Space's copy."
    if op in ("share", "revoke"):
        return f"{op}d {d.get('repo')}" if op == "revoke" else f"shared {d.get('repo')}. Their Space picks it up on its next poll."
    if op == "apply":
        n = d.get("applied") or 0
        head = str(d.get("head"))[:7]
        return f"{d.get('project_id')} moved forward {n} commit(s), now at {head}" if n else f"{d.get('project_id')} was already up to date at {head}"
    rows = d.get("results", [d])
    lines = []
    for r in rows:
        name = r.get("project_id", "?")
        if op == "backup" and "snapshots" in r:   # --list
            snaps = r.get("snapshots") or []
            lines.append(f"{name:<30} {len(snaps)} snapshot(s)" + (f", latest {snaps[-1].get('snapshot_id')}" if snaps else ""))
        elif r.get("error"):
            lines.append(f"{name:<30} failed: {r['error']}")
        else:
            lines.append(f"{name:<30} {'backed up' if op == 'backup' else 'restored'}"
                         + (f" {r['snapshot_id']}" if r.get("snapshot_id") else ""))
    return "\n".join(lines) or "nothing to do"


def _answer(value) -> tuple[dict, int]:
    """(object, status) for whatever the handler returned."""
    status = 200
    if isinstance(value, Response):
        status = value.status_code
        value = json.loads(value.body)
    if isinstance(value, list):
        value = {"results": value}
    return value, status


def _error(exc: HTTPException) -> dict:
    d = exc.detail
    if isinstance(d, dict):
        code = d.get("code") or d.get("error") or "failed"
        message = d.get("message") or d.get("detail") or code
        if d.get("suggestion"):
            message = f"{message} {d['suggestion']}"
    else:
        code, message = "failed", str(d)
    return {"code": code, "message": message, "status": exc.status_code, "detail": d}


OPS = {"projects-add": _projects_add, "projects-remove": _projects_remove, "share": _share,
       "revoke": _revoke, "apply": _apply, "backup": _backup, "restore": _restore}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="qq", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="op", required=True)

    def op(name: str, *values: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true")
        for v in values:   # positional for people, --name=VALUE for the server
            p.add_argument(v, nargs="?", default=None)
            p.add_argument(f"--{v}", dest=f"{v}_opt", default=None)
        return p

    op("projects-add", "project", "url")
    p = op("projects-remove", "project")
    p.add_argument("--confirm", default=None)
    p.add_argument("--yes", action="store_true")
    op("share", "project", "space")
    op("revoke", "project", "space")
    op("apply", "project")
    p = op("backup", "project")
    p.add_argument("--all", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--note", default=None)
    p = op("restore", "project")
    p.add_argument("--all", action="store_true")
    p.add_argument("--snapshot", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--pin", action="append", default=[], metavar="PROJECT=SNAPSHOT")
    return ap


def _usage(a) -> str | None:
    """Fill each value from its positional or --name form; the problem, if any."""
    for key in ("project", "url", "space"):
        if hasattr(a, key):
            setattr(a, key, getattr(a, f"{key}_opt") or getattr(a, key))
    if a.op == "projects-remove" and a.yes and a.confirm is None:
        a.confirm = a.project
    if a.op in ("backup", "restore") and (a.all or getattr(a, "list", False)):
        return "give a project or --all, not both" if a.project else None
    if a.op == "restore" and any("=" not in p for p in a.pin):
        return "--pin takes PROJECT=SNAPSHOT"
    missing = [k for k in ("project", "url", "space") if hasattr(a, k) and not getattr(a, k)]
    return f"missing {', '.join(missing)}" if missing else None


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    problem = _usage(a)
    if problem:
        print(f"qq {a.op}: {problem}", file=sys.stderr)
        return 2
    _load_settings()
    # With --json, stdout carries exactly one object: anything the code prints goes to stderr.
    quiet = contextlib.redirect_stdout(sys.stderr) if a.json else contextlib.nullcontext()
    try:
        with quiet:
            data, _status = _answer(asyncio.run(OPS[a.op](a)))
    except HTTPException as exc:
        err = _error(exc)
        if a.json:
            print(json.dumps(err))
        else:
            print(f"qq {a.op}: {err['message']}", file=sys.stderr)
        return 1
    if a.json:
        # A list answer is final as it is, as the route's 200 was: each entry carries its own error.
        print(json.dumps(data))
        return 0
    print(_say(a.op, data))
    failed = any(r.get("error") for r in data.get("results", []) if isinstance(r, dict))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

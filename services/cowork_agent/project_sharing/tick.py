"""One relay tick as its own process: what `qq sharing tick` runs.

The watcher's command scheduler runs it every minute as the built-in "sharing
tick" job (job.py), and a person or agent can run it by hand. It loads the
server's settings, takes the tick lock, loads the last status, runs
poller.run_tick() and keeps going while there is a backlog (the old loop's
5 s drain ticks) or a nudge arrived meanwhile, then saves the status for the
server and the next tick.

Exit codes follow the qq contract: 0 ran (or parked: nothing to do), 1 the poll
failed, 3 another tick holds the lock.
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

REPO_ROOT = Path(__file__).resolve().parents[3]
MAX_ROUNDS = 12  # a backlog drains in 5 s rounds, at most about a minute per run


def _load_settings() -> None:
    """The settings the server loads at start-up, for a run from a terminal.
    Under the scheduler the server's environment is inherited and this only
    re-reads the same files."""
    from dotenv import load_dotenv

    from utils.runtime_env import quirq_state_dir

    load_dotenv(REPO_ROOT / ".env")
    state = quirq_state_dir()
    runtime = (os.getenv("QUIRQ_RUNTIME_FILE") or "").strip() or str(state / "settings" / "runtime.env")
    load_dotenv(runtime, override=True)
    secrets = (os.getenv("QUIRQ_SECRETS_FILE") or "").strip() or str(state / "secrets" / "secrets.env")
    if Path(secrets).is_file():
        load_dotenv(secrets, override=True)


async def _run() -> int:
    from . import job, log_line, poller

    rounds = 0
    while True:
        job.take_pending_nudge()  # this round covers any nudge made so far
        try:
            delay = await poller.run_tick()
        except Exception as exc:  # noqa: BLE001 — report it; the next run tries again
            log_line(f"⚠️ relay: tick failed: {exc}")
            return rounds + 1
        rounds += 1
        again = delay == poller.DRAIN_INTERVAL or job.take_pending_nudge()
        if not again or rounds >= MAX_ROUNDS:
            return rounds
        await asyncio.sleep(poller.DRAIN_INTERVAL)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qq sharing tick", description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="print one JSON object")
    args = ap.parse_args(argv)
    _load_settings()

    from services.storage.layout import sharing_dir

    from . import clone, status

    def emit(result: dict, code: int) -> int:
        if args.json:
            print(json.dumps(result))
        else:
            print(f"qq sharing tick: {result.get('result')}"
                  + (f" ({result['reason']})" if result.get("reason") else "")
                  + (f", {result['rounds']} round(s), {result['repos']} repo(s)" if "rounds" in result else ""))
        return code

    folder = sharing_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / ".tick.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return emit({"result": "busy", "reason": "another tick is running"}, 3)

        # With --json, stdout carries exactly one JSON object: the relay's own
        # log lines (log_line prints) go to stderr, which the job's log keeps.
        quiet = contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext()
        with quiet:
            status.load()
            try:
                clone.cleanup_stale_temp_dirs()  # under the lock no clone of ours is in flight
            except Exception:  # noqa: BLE001
                pass
            rounds = asyncio.run(_run())
            status.save()

    snap = status.snapshot()
    parked = snap.get("cadence") == "parked"
    failed = not parked and snap.get("last_poll_ok") is False
    result = {"result": "parked" if parked else "failed" if failed else "ok", "reason": snap.get("reason"),
              "rounds": rounds, "repos": len(snap.get("repos") or {}), "last_poll_at": snap.get("last_poll_at")}
    return emit(result, 1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())

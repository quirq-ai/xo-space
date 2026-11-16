"""Opt-in subscription load rehearsal on a new isolated store; never starts a server.

Without --run this performs only a model-free account check. A real run
requires the founder's approval. The account must explicitly report paid
usage disabled, with fresh numeric windows below 80 percent. Storm's normal
admission, memory and paid gates remain active throughout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def subscription_ready(snapshot) -> bool:
    return bool(snapshot.source == "measured" and snapshot.account and snapshot.paid and snapshot.paid.possible is False
                and snapshot.paid.in_use is not True and not snapshot.overage and snapshot.windows
                and all(w.utilization is not None and 0 <= w.utilization < 0.8
                        and w.status != "limited" for w in snapshot.windows))


async def rehearsal(args) -> dict:
    from services.advisory.advisory import Advisory
    from services.cowork_agent.adapters import contract as c
    from services.cowork_agent.storm import engine as storm

    runtime = storm.load_runtime(args.agent)
    if runtime is None or not callable(getattr(runtime, "read_usage", None)):
        raise RuntimeError("This runtime has no model-free usage read; no sessions were started.")
    snapshot = await asyncio.wait_for(runtime.read_usage(), 30)
    if not subscription_ready(snapshot):
        raise RuntimeError("The account did not prove paid usage disabled and every window below 80%; "
                           "no sessions were started.")
    if not args.run:
        return {"preflight": "ready", "model_calls": 0}
    # Do not inherit a provider API key in a subscription rehearsal.
    if any(value and key.endswith("API_KEY") for key, value in os.environ.items()):
        raise RuntimeError("An API key is present in this process environment; no sessions were started.")

    folder = Path(tempfile.mkdtemp(prefix="storm-load-"))
    work = folder / "work"
    work.mkdir()
    saved = {name: os.environ.get(name) for name in ("QUIRQ_STATE_ROOT", "XO_PROJECTS_ROOT", "QUIRQ_SECRETS_FILE")}
    os.environ.update(QUIRQ_STATE_ROOT=str(folder / "state"), XO_PROJECTS_ROOT=str(work),
                      QUIRQ_SECRETS_FILE=str(folder / "no-secrets.env"))
    # agent_env may already have been imported by runtime discovery.
    from services.cowork_agent.registry import agent_env
    original_secrets = agent_env.ENV_FILE
    agent_env.ENV_FILE = folder / "no-secrets.env"
    advisory = Advisory(readers=lambda: {args.agent: runtime})
    advisory.start()
    began = time.monotonic()
    maximum_running = maximum_processes = 0
    engine = None
    try:
        engine = await storm.boot(runtimes=lambda name: runtime if name == args.agent else None,
                                  providers=[args.agent], advisory=advisory)
        if engine is None:
            raise RuntimeError("The isolated engine did not start.")
        sessions = [await engine.create_session(c.SessionSpec(
            cwd=str(work), model=args.model, permissions=c.PermissionPolicy(mode="ask")),
            agent=args.agent, role="plain", preset="plain_chat") for _ in range(args.sessions)]
        await asyncio.gather(*(engine.deliver(sid, storm.Wake(
            id=f"load:{i}", text="Reply exactly READY. Do not use any tools or change any file.",
            priority="now", reason="message", origin="person")) for i, sid in enumerate(sessions)))
        async with asyncio.timeout(120):
            while True:
                running, processes, finished, failed = await engine.store.read(lambda conn: (
                    conn.execute("SELECT count(*) FROM turns WHERE status = 'running'").fetchone()[0],
                    conn.execute("SELECT count(*) FROM procs").fetchone()[0],
                    conn.execute("SELECT count(*) FROM turns WHERE status = 'finished'").fetchone()[0],
                    conn.execute("SELECT count(*) FROM turns WHERE status IN ('failed', 'stopped')").fetchone()[0]))
                maximum_running, maximum_processes = max(maximum_running, running), max(maximum_processes, processes)
                if failed:
                    raise RuntimeError("A load turn failed or was held/stopped; inspect the isolated store.")
                if finished == args.sessions:
                    break
                await asyncio.sleep(0.1)
        return {"sessions": args.sessions, "finished": finished, "peak_running": maximum_running,
                "peak_processes": maximum_processes, "seconds": round(time.monotonic() - began, 3),
                "store": str(folder / "state" / "engine" / "xo.db")}
    finally:
        if engine:
            await storm.shutdown()
        await advisory.stop()
        agent_env.ENV_FILE = original_secrets
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--sessions", type=int, choices=range(5, 11), default=5)
    parser.add_argument("--run", action="store_true", help="Start model turns; requires founder approval")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(rehearsal(args)), sort_keys=True))
        return 0
    except (RuntimeError, TimeoutError) as exc:
        print(str(exc) or "The rehearsal timed out; its isolated sessions were shut down.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

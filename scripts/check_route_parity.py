#!/usr/bin/env python3
"""Route-parity guard for the agent-modular broker."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Runnable as `python scripts/check_route_parity.py`, where sys.path[0] is
# scripts/ — the repo root has to go on the path before `utils` resolves.
sys.path.insert(0, str(REPO))

from utils.commands import run_sync  # noqa: E402

# Emitted inside the subprocess: the app's OpenAPI paths plus whatever this
# agent's own routes.py contributes, so the parent can check one against the
# other without importing the app itself.
_PROBE = """
import contextlib, io, json, warnings
warnings.simplefilter("ignore")
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    import server
    from services.cowork_agent.adapters.loader import try_load_capability
    mod = try_load_capability("routes")
    router = getattr(mod, "router", None) if mod else None
    own = sorted({r.path for r in getattr(router, "routes", [])}) if router is not None else []
print(json.dumps({"paths": sorted(server.app.openapi()["paths"]), "own": own}))
"""


def discover_agents() -> list[str]:
    d = REPO / "config" / "agents"
    return sorted(
        p.name
        for p in d.iterdir()
        if p.is_dir() and (p / "manifest.json").is_file()
    )


def probe(agent: str, roots: Path) -> dict:
    env = {
        **os.environ,
        "AGENT_NAME": agent,
        # Isolated roots: probing must never create or touch a real workspace.
        "XO_PROJECTS_ROOT": str(roots / "projects"),
        "QUIRQ_STATE_ROOT": str(roots / "state"),
        "QUIRQ_SKIP_BOOT_INSTALL": "1",
        "QUIRQ_WATCHER_ENABLED": "false",
        "STARTUP_WARMUP_ENABLED": "false",
        "PYTHONWARNINGS": "ignore",
    }
    out = run_sync(
        [sys.executable, "-c", _PROBE],
        cwd=REPO, env=env, timeout=180, separate_stderr=True,
    )
    if not out.ok:
        raise SystemExit(
            f"import gate FAILED for AGENT_NAME={agent}\n"
            f"{out.stderr.strip()[-2000:]}"
        )
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise SystemExit(
            f"could not parse probe output for {agent}: {exc}\n{out.stdout[-2000:]}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="print the core and per-agent sets")
    args = ap.parse_args()

    agents = discover_agents()
    if not agents:
        print("no agent manifests found", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="route-parity-") as tmp:
        roots = Path(tmp)
        (roots / "projects").mkdir()
        (roots / "state").mkdir()
        data = {a: probe(a, roots) for a in agents}

    sets = {a: set(d["paths"]) for a, d in data.items()}
    own = {a: set(d["own"]) for a, d in data.items()}
    core = set.intersection(*sets.values())

    failures: list[str] = []

    for a in agents:
        expected = core | own[a]
        missing = expected - sets[a]
        extra = sets[a] - expected
        if missing:
            failures.append(
                f"{a}: {len(missing)} path(s) expected but absent:\n    "
                + "\n    ".join(sorted(missing))
            )
        if extra:
            # A path outside core that this agent's routes.py does not declare:
            # either a core route that is not universal, or a leaked agent-
            # owned route.
            failures.append(
                f"{a}: {len(extra)} path(s) present but not in core and not "
                f"declared by adapters/{a}/routes.py:\n    "
                + "\n    ".join(sorted(extra))
            )

    # An agent-owned route must not appear under any other agent.
    for a in agents:
        for b in agents:
            if a == b:
                continue
            leaked = (own[a] - own[b]) & sets[b]
            if leaked:
                failures.append(
                    f"{b}: carries {len(leaked)} route(s) owned by {a}:\n    "
                    + "\n    ".join(sorted(leaked))
                )

    width = max(len(a) for a in agents)
    print(f"core: {len(core)} paths shared by all {len(agents)} agents\n")
    for a in agents:
        n_own = len(own[a])
        tag = "" if n_own else "   (no routes.py)"
        print(f"  {a.ljust(width)}  {len(sets[a]):>4} = {len(core)} core + {n_own:>2} own{tag}")

    if args.list:
        print("\n--- core ---")
        for p in sorted(core):
            print(f"  {p}")
        for a in agents:
            if own[a]:
                print(f"\n--- {a} own ---")
                for p in sorted(own[a]):
                    print(f"  {p}")

    if failures:
        print("\nROUTE PARITY BROKEN\n")
        for f in failures:
            print(f"  {f}\n")
        return 1

    print("\nRoute parity OK — every agent is core + its own routes.py, nothing leaked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

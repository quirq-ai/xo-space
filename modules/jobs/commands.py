"""``python -m quirq jobs <command>``

  list                    every saved job with its state
  run <job_id>            run one job now (the same as Run now)
  runs <job_id> [n]       the newest n run records (default 20)
"""

from __future__ import annotations

from services.errors import ServiceError

from . import service


def list_(args: list[str]) -> dict:
    return {"jobs": service.list_jobs()}


def run(args: list[str]) -> dict:
    if not args:
        raise ServiceError("missing_job_id", "usage: quirq jobs run <job_id>")
    return {"ok": True, "started": True, "job": service.run_now(args[0])}


def runs(args: list[str]) -> dict:
    if not args:
        raise ServiceError("missing_job_id", "usage: quirq jobs runs <job_id> [limit]")
    limit = int(args[1]) if len(args) > 1 else 20
    return {"job_id": args[0], "runs": service.list_runs(args[0], limit),
            "log_path": str(service.log_file(args[0]))}


COMMANDS = {"list": list_, "run": run, "runs": runs}

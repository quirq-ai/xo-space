#!/usr/bin/env python3
"""Real scheduler API with disposable state and a read-only fictional UI upstream.

Run server.py on 5100 first, then this script with the project venv. It forwards
only GETs outside /api/schedules, exposes no process-control writes, and disables
automatic jobs. Commands explicitly submitted to this server do execute locally.
Use only benign test commands with the returned temporary working directory.
"""
import argparse
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5112)
    parser.add_argument("--fixture-port", type=int, default=5100)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    with tempfile.TemporaryDirectory(prefix="space-command-review-") as temporary:
        os.environ.update(QUIRQ_STATE_ROOT=temporary, QUIRQ_COMMAND_LOG="off",
                          XO_SCHEDULER_ENABLED="0", XO_SCHEDULER_MAX_CONCURRENT="1")
        os.chdir(temporary)
        from fastapi import FastAPI, Request
        from fastapi.responses import Response
        import uvicorn
        from modules.jobs.routes import router

        app = FastAPI()
        app.include_router(router)
        from routers.errors import install_service_errors
        install_service_errors(app)

        @app.get("/__fixture__/runtime")
        def runtime():
            return {"cwd": temporary, "python": sys.executable, "automatic_jobs": False}

        @app.get("/space/server/status")
        def status():
            return {"running": True, "restart_mode": "foreground",
                    "instance_id": "isolated-command-review"}

        @app.get("/{path:path}")
        async def fixture(path: str, request: Request):
            target = f"http://127.0.0.1:{args.fixture_port}/{path}"
            if request.url.query:
                target += "?" + request.url.query

            def fetch():
                try:
                    response = urlopen(target, timeout=10)
                except HTTPError as error:
                    response = error
                with response:
                    return Response(response.read(), status_code=response.status,
                                    headers={"Content-Type": response.headers.get("Content-Type", "application/octet-stream"),
                                             "Cache-Control": "no-store"})

            return await asyncio.to_thread(fetch)

        print(f"Isolated command review: http://127.0.0.1:{args.port}/space/ (cwd {temporary})", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning",
                    access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()

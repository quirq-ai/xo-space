"""Space: the local workspace knowledge graph.

Serves the Space UI folder as static files under /space, plus a tiny control
API the UI uses for its server on/off widget and the Setup tab's self-update.

The folder location comes from SPACE_DIR (env), defaulting to space_ui/ in the
repo. The UI's DATA comes from the workspace .xo directory via /xo/*.json
(routers/xo_data.py), not from this mount.
"""

import asyncio
import os
import signal
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# Bundled UI (space_ui/ at the repo root); SPACE_DIR env var overrides, e.g.
# to point at a live xo-atlas checkout during UI development.
DEFAULT_SPACE_DIR = str(Path(__file__).resolve().parent.parent / "space_ui")
SPACE_DIR = Path(os.getenv("SPACE_DIR", DEFAULT_SPACE_DIR)).expanduser()

router = APIRouter(prefix="/space", tags=["space"])
_SERVER_INSTANCE = str(time.time_ns())


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


@router.get("/server/status")
async def space_server_status():
    """Lightweight status for the Space UI widget (also see /health)."""
    from services.cowork_agent.runtime_config import restart_mode

    return {
        "status": "on",
        "instance_id": _SERVER_INSTANCE,
        "restart_mode": restart_mode(),
        "pid": os.getpid(),
        "space_dir": str(SPACE_DIR),
        "space_dir_exists": SPACE_DIR.exists(),
    }


@router.post("/server/stop")
async def space_server_stop(request: Request):
    """Gracefully stop the server. Localhost only; restart via ./cowork-api.sh start."""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="stop is allowed from localhost only")

    async def _terminate_soon():
        await asyncio.sleep(0.4)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.get_running_loop().create_task(_terminate_soon())
    return {"status": "stopping", "restart": "./cowork-api.sh start"}


@router.post("/server/restart")
async def space_server_restart(request: Request):
    """Restart through the install's supervisor; never start a second server."""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="restart is allowed from localhost only")
    from services.cowork_agent.runtime_config import REPO_ROOT, restart_mode
    from utils.commands import spawn_detached

    mode = restart_mode()
    if mode == "foreground":
        raise HTTPException(status_code=409, detail="Ctrl-C and re-run the server from the terminal where you launched it.")
    if mode == "native":
        result = spawn_detached(["./cowork-api.sh", "restart"], cwd=REPO_ROOT)
        if not result.ok:
            raise HTTPException(status_code=503, detail=f"Could not start the restart script: {result.output}")
    else:
        async def _terminate_soon():
            await asyncio.sleep(0.4)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.get_running_loop().create_task(_terminate_soon())
    return {"ok": True, "restarting": True, "mode": mode, "instance_id": _SERVER_INSTANCE}


@router.get("/update/status")
async def space_update_status():
    """Version check for the Setup tab: how far HEAD is behind the remote.

    Fetches the checkout's own remote via git; offline it still reports the
    local version with fetch_ok false."""
    from services.cowork_agent.self_update import check_update_status

    try:
        return await asyncio.to_thread(check_update_status)
    except Exception as exc:
        print(f"⚠️ update status failed ({exc})")
        raise HTTPException(
            status_code=503,
            detail={"code": "update_status_failed",
                    "message": "Could not determine the checkout's version state."},
        )


@router.post("/update/apply")
async def space_update_apply(request: Request):
    """Fast-forward the checkout to the remote branch. Localhost only, like
    /server/stop: it changes the code on disk. The running server keeps the
    old version until restarted."""
    if not _is_local(request):
        raise HTTPException(status_code=403,
                            detail="update is allowed from localhost only")
    from services.cowork_agent.self_update import UpdateError, apply_update

    try:
        return await asyncio.to_thread(apply_update)
    except UpdateError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "update_failed", "message": str(exc)},
        )
    except Exception as exc:
        print(f"⚠️ update apply failed ({exc})")
        raise HTTPException(
            status_code=503,
            detail={"code": "update_failed",
                    "message": "The update could not be applied."},
        )


SPACE_CACHE_TTL = float(os.getenv("SPACE_CACHE_TTL", "30"))

# The graph, dashboard and session-telemetry payloads used to be generated
# here and served from /space/data/. They are files in the workspace .xo
# directory now, served by routers/xo_data.py at /xo/*.json — one location on
# disk, one URL that mirrors it. Only session_prompts stays: it is a
# per-session lookup, not a workspace file.

# Aggregate telemetry never contains prompt text. Session details request one
# transcript lazily through its provider's optional capability.
_session_prompts_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_SESSION_PROMPTS_CACHE_MAX = 32


@router.get("/data/session_prompts.json")
async def session_prompts_data(agent: str, sid: str):
    """Return user prompts for one session, grouped into human turns."""
    from services.cowork_agent.adapters.loader import try_load_capability

    now = time.monotonic()
    hit = _session_prompts_cache.get((agent, sid))
    if hit is not None and now - hit[0] < SPACE_CACHE_TTL:
        return JSONResponse(hit[1], headers={"Cache-Control": "no-store"})

    try:
        module = try_load_capability("session_prompts", agent=agent)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_agent",
                "message": f"Invalid telemetry source {agent!r}.",
            },
        )
    collector = getattr(module, "collect_session_prompts", None) if module else None
    if not callable(collector):
        return JSONResponse(
            {
                "source": {"id": agent},
                "session_id": sid,
                "supported": False,
                "total_prompts": 0,
                "capped": False,
                "prompts": [],
            },
            headers={"Cache-Control": "no-store"},
        )

    try:
        data = await asyncio.to_thread(collector, sid)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_session", "message": str(exc)},
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "session_transcript_not_found",
                "message": "No transcript found for this session.",
            },
        )
    except Exception as exc:
        print(f"⚠️ session prompts failed for {agent}:{sid} ({exc})")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "session_prompts_unavailable",
                "message": "Could not read this session's prompts.",
            },
        )

    if len(_session_prompts_cache) >= _SESSION_PROMPTS_CACHE_MAX:
        oldest = min(
            _session_prompts_cache,
            key=lambda key: _session_prompts_cache[key][0],
        )
        _session_prompts_cache.pop(oldest, None)
    _session_prompts_cache[(agent, sid)] = (now, data)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


def mount_space(app):
    """Mount the Space folder at /space (index.html served at /space/)."""
    if SPACE_DIR.exists():
        app.mount("/space", StaticFiles(directory=str(SPACE_DIR), html=True), name="space")
    else:
        print(f"⚠️ Space folder not found at {SPACE_DIR}; /space not mounted (set SPACE_DIR to change)")

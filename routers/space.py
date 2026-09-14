"""Space: the local workspace knowledge graph.

Serves the Space UI folder as static files under /space, plus a tiny control
API the UI uses for its server on/off widget and the Setup tab's self-update.

The folder location comes from SPACE_DIR (env), defaulting to space_ui/ in the
repo. The UI's DATA comes from the workspace .xo directory via /xo/*.json
(routers/xo_data.py), not from this mount.
"""

import asyncio
import ipaddress
import os
import re
import signal
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

# Bundled UI (space_ui/ at the repo root); SPACE_DIR env var overrides, e.g.
# to point at a live xo-atlas checkout during UI development.
DEFAULT_SPACE_DIR = str(Path(__file__).resolve().parent.parent / "space_ui")
SPACE_DIR = Path(os.getenv("SPACE_DIR", DEFAULT_SPACE_DIR)).expanduser()

router = APIRouter(prefix="/space", tags=["space"])
_SERVER_INSTANCE = str(time.time_ns())


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _origin_triple(value: str) -> tuple[str, str, int] | None:
    """A browser ``Origin`` → ``(scheme, host, port)``, or ``None`` when it is
    not exactly an origin (userinfo, path, query, fragment, whitespace)."""
    if not value or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return (parsed.scheme, host, port)


_CODER_APP_LABEL = re.compile(r"[a-z0-9-]+")


def _is_this_coder_workspace_host(host: str) -> bool:
    """Whether ``host`` is one of *this* Coder workspace's app hostnames.

    Coder serves a workspace app at ``<app>--<workspace>--<owner>.<domain>``
    and tells the pod its workspace and owner names through the environment
    (``coder_identity``). Its proxy forwards the real ``Host`` but adds no
    forwarding headers (checked against the live proxy), so this is the one
    signal that a public Origin is the pod's own front door rather than a
    DNS-rebinding page. Off Coder both names are unset and nothing matches,
    so a local install keeps refusing every public host.
    """
    from services.cowork_agent.coder_identity import owner_name, workspace_name

    workspace, owner = workspace_name(), owner_name()
    if not workspace or not owner:
        return False
    app, marker, domain = host.partition(f"--{workspace}--{owner}.")
    return bool(marker) and bool(domain) and _CODER_APP_LABEL.fullmatch(app) is not None


def _is_local_mutation(request: Request) -> bool:
    """Allow local CLI calls and same-origin browser mutations.

    A page on another site can submit a simple POST without a CORS preflight,
    so a browser request (one that carries ``Origin``) must come from this
    Space's own origin. Two origins count as its own:

    - the loopback origin the request was addressed to — a local install,
      where the browser and the server share the machine;
    - this Coder workspace's own app hostname, when the pod runs under Coder
      (``_is_this_coder_workspace_host``): the proxy connects from loopback
      and the browser's Origin is the workspace URL. Only the host is
      compared for that case — the proxy terminates TLS, so the scheme and
      port the app sees are not the browser's.

    Any other public host is refused even when it equals the request's own
    Host: that is what stops a DNS-rebinding page, whose Origin equals the
    host it hijacked, from driving a local server. CLI clients send no
    Origin; for them the loopback peer is the credential.
    """
    if not _is_local(request):
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    triple = _origin_triple(origin)
    if triple is None:
        return False
    scheme, host, port = triple
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False  # a public hostname
    if not loopback:
        return _is_this_coder_workspace_host(host) and host == request.url.hostname
    request_port = request.url.port
    if request_port is None:
        request_port = 443 if request.url.scheme == "https" else 80
    return (scheme, host, port) == (request.url.scheme, request.url.hostname, request_port)


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


@router.get("/setup/status")
async def space_setup_status():
    """Workspace metadata and verified account status, without credential values."""
    from services.setup_status import snapshot

    return await snapshot()


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
    if not _is_local_mutation(request):
        raise HTTPException(status_code=403, detail="restart requires a local, same-origin request")
    from services.cowork_agent.runtime_config import REPO_ROOT, native_restart_pid, restart_mode
    from utils.commands import spawn_detached

    mode = restart_mode()
    if mode == "foreground":
        raise HTTPException(status_code=409, detail="Ctrl-C and re-run the server from the terminal where you launched it.")
    if mode == "native":
        pid = native_restart_pid()
        if pid is None:
            raise HTTPException(status_code=409, detail="The native runner changed; refresh before restarting.")
        result = spawn_detached(
            ["./cowork-api.sh", "restart-owned", str(pid), str(os.getpid())], cwd=REPO_ROOT,
        )
        if not result.ok:
            raise HTTPException(status_code=503, detail=f"Could not start the restart script: {result.output}")
    else:
        async def _terminate_soon():
            await asyncio.sleep(0.4)
            os.kill(os.getpid(), signal.SIGTERM)

        # Starlette starts background work only after sending the response body.
        return JSONResponse(
            {"ok": True, "restarting": True, "mode": mode, "instance_id": _SERVER_INSTANCE},
            background=BackgroundTask(_terminate_soon),
        )
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

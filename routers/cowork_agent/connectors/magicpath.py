"""
REST routes for the MagicPath connector.

Installs the MagicPath agent skill + `magicpath-ai` CLI and manages the
workspace user's MagicPath session (login / logout / status).

Endpoints:
  GET  /api/connectors/magicpath/status     — skill/CLI install state + live auth identity
  POST /api/connectors/magicpath/setup      — install skill (npx skills add) + CLI (npm -g); idempotent
  POST /api/connectors/magicpath/login      — no code: returns the browser login URL (bootstrap);
                                              with code: exchange it via `login --code`
  POST /api/connectors/magicpath/logout     — `magicpath-ai logout` (idempotent)
  GET  /callback                            — auto-login landing for the MagicPath redirect (dispatcher)

Login flow: the login bootstrap (POST /login with no code) deliberately omits
MagicPath's ``?port=`` parameter, so after the user approves, MagicPath's page
displays the authorization code with a copy button instead of redirecting to
``http://localhost:<port>/callback`` — that redirect only works when the
user's browser can reach this API's port, which a remote workspace without
port forwarding cannot offer. The user pastes the code back into the same
endpoint. /callback is retained for setups that do forward the port (the
redirect then auto-completes login).

/callback ordering invariant: when MagicPath's web flow does redirect, it goes
to ``http://localhost:<port>/callback?code=<JWT>`` — the same path as the Vercel
OAuth callback (connectors/vercel.py). This router must be mounted BEFORE
``vercel_router`` (see routers/cowork_agent/__init__.py): the dispatcher below
claims code-only JWT requests (MagicPath — its codes never carry ``state``) and
delegates everything else to ``vercel_oauth_callback`` unchanged, so Vercel's
observable contract is preserved. Vercel codes always arrive with a PKCE
``state`` and can never be mistaken for MagicPath's.

Session custody: ``~/.magicpath/session.json`` is written and deleted by the
CLI only — this module never reads or stores tokens. Authorization codes are
single-use secrets: they must never appear in response bodies, and command
logging must keep redacting ``--code`` values from argv/output.
"""

import asyncio
import html
import json
import logging
import os
import re
import shutil
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from services.cowork_agent import skill_installer
from utils.commands import CommandResult, run

from .vercel import vercel_oauth_callback

log = logging.getLogger(__name__)
router = APIRouter()


def _int_env(name: str, fallback: int) -> int:
    try:
        return int(os.getenv(name) or fallback)
    except ValueError:
        return fallback


WEB_URL = (os.getenv("MAGICPATH_WEB_URL") or "https://www.magicpath.ai").rstrip("/")
# Informational only since the login bootstrap dropped ?port= (manual-code
# flow). Kept in the no-code login response shape and relevant to /callback
# when a user forwards this API's port themselves.
LOGIN_PORT = _int_env("MAGICPATH_LOGIN_PORT", _int_env("PORT", 5002))
MAGICPATH_CLI = (os.getenv("MAGICPATH_CLI_PATH") or "magicpath-ai").strip()

SKILL_SOURCE = "magicpathai/agent-skills"
SKILL_NAME = "magicpath"
# Universal (agent-agnostic) install target of `npx skills add -g`. The skills
# CLI only symlinks agents it can auto-detect (e.g. ~/.claude); setup then runs
# skill_installer.link_global_skill so every installed registry agent gets it.
SKILL_MD = Path.home() / ".agents" / "skills" / SKILL_NAME / "SKILL.md"

SETUP_TIMEOUT_SECONDS = 300
LOGIN_TIMEOUT_SECONDS = 60
WHOAMI_TIMEOUT_SECONDS = 15
LOGOUT_TIMEOUT_SECONDS = 30
VERSION_TIMEOUT_SECONDS = 10

_setup_lock = asyncio.Lock()
_session_lock = asyncio.Lock()  # serialises login/logout (both rewrite the CLI session)
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


class LoginBody(BaseModel):
    # Raw authorization code, or the full callback URL pasted from the browser.
    # None/blank selects the login-URL bootstrap response instead of an exchange.
    code: str | None = Field(default=None, max_length=16384)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _version_key(entry: Path) -> tuple[int, ...]:
    nums = re.findall(r"\d+", entry.name)
    return tuple(int(n) for n in nums) if nums else (0,)


def _ensure_node_on_path() -> None:
    """Prepend a node bin dir to PATH when npm isn't already resolvable.

    When the FastAPI process is launched outside an interactive shell, nvm's
    shim never runs and `shutil.which("npm")` returns None. Unlike the codex
    helper this also covers system-wide nvm under /opt/nvm (where node lives
    on coder workspaces), and sorts versions numerically so v9 never beats v24.
    """
    if shutil.which("npm"):
        return
    candidates: list[Path] = []
    for root in (Path.home() / ".nvm" / "versions" / "node", Path("/opt/nvm/versions/node")):
        try:
            if root.is_dir():
                versions = sorted(root.iterdir(), key=_version_key, reverse=True)
                candidates.extend(v / "bin" for v in versions)
        except OSError:
            continue
    candidates.extend([Path("/usr/local/bin"), Path("/usr/bin")])
    for bin_dir in candidates:
        if (bin_dir / "npm").is_file():
            current = os.environ.get("PATH", "")
            if str(bin_dir) not in current.split(os.pathsep):
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{current}"
                log.info("magicpath: PATH augmented with %s", bin_dir)
            return
    log.warning("magicpath: npm not found in PATH, nvm dirs, or system bin dirs")


def _cli_binary() -> str | None:
    """Absolute-path override via MAGICPATH_CLI_PATH, else PATH lookup."""
    _ensure_node_on_path()
    if os.path.sep in MAGICPATH_CLI:
        candidate = Path(MAGICPATH_CLI)
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    return shutil.which(MAGICPATH_CLI)


def _extract_code(raw: str) -> str | None:
    """Accept a raw authorization code or a full pasted callback URL."""
    text = raw.strip().strip("\"'")
    if "://" in text:
        try:
            query = urllib.parse.urlparse(text).query
        except ValueError:
            return None
        text = (urllib.parse.parse_qs(query).get("code") or [""])[0].strip()
    return text if _JWT_RE.match(text) else None


def _parse_json_object(text: str) -> dict | None:
    """Parse CLI JSON output, tolerating stray non-JSON lines around it."""
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


async def _whoami(cli: str) -> dict | None:
    """Live identity probe; None on any failure (not logged in, offline, ...)."""
    result = await run([cli, "whoami", "-o", "json"], timeout=WHOAMI_TIMEOUT_SECONDS)
    if not result.ok:
        return None
    data = _parse_json_object(result.output)
    if data is None:
        return None
    return {
        "id": str(data.get("id") or ""),
        "name": str(data.get("displayName") or data.get("name") or ""),
        "email": str(data.get("email") or ""),
    }


async def _exchange_code(cli: str, code: str) -> CommandResult:
    # No log_path, and callers must not log argv/output: both carry the code.
    return await run([cli, "login", "--code", code], timeout=LOGIN_TIMEOUT_SECONDS)


def _step_error(result: CommandResult, failure_slug: str) -> str:
    if result.binary_missing:
        return "npm_not_found"
    if result.timed_out:
        return "timeout"
    return failure_slug


async def _install_skill() -> dict:
    if SKILL_MD.is_file():
        step = {"ok": True, "skipped": True, "error": None}
    else:
        result = await run(
            ["npx", "-y", "skills", "add", SKILL_SOURCE, "--skill", SKILL_NAME, "-g", "-y"],
            timeout=SETUP_TIMEOUT_SECONDS,
            env={**os.environ, "CI": "1"},
        )
        if not result.ok or not SKILL_MD.is_file():
            log.warning(
                "magicpath: skill install failed (exit %s): %s",
                result.returncode, result.output[:2000],
            )
            return {
                "ok": False, "skipped": False,
                "error": _step_error(result, "skill_install_failed"),
                "agents": {},
            }
        step = {"ok": True, "skipped": False, "error": None}
    # Runs on the skipped path too: an agent installed after the universal copy
    # landed still needs its symlink on the next setup call.
    step["agents"] = await asyncio.to_thread(skill_installer.link_global_skill, SKILL_NAME)
    return step


async def _install_cli() -> dict:
    if _cli_binary():
        return {"ok": True, "skipped": True, "error": None}
    result = await run(["npm", "install", "-g", "magicpath-ai"], timeout=SETUP_TIMEOUT_SECONDS)
    if not result.ok or not _cli_binary():
        log.warning(
            "magicpath: CLI install failed (exit %s): %s",
            result.returncode, result.output[:2000],
        )
        return {"ok": False, "skipped": False, "error": _step_error(result, "cli_install_failed")}
    return {"ok": True, "skipped": False, "error": None}


def _describe_step(label: str, step: dict) -> str:
    if step["skipped"]:
        return f"{label} already installed"
    if step["ok"]:
        return f"{label} installed"
    return f"{label} install failed ({step['error']})"


def _callback_page(title: str, message: str, *, status_code: int) -> HTMLResponse:
    safe_title = html.escape(title)
    content = (
        "<!DOCTYPE html>\n"
        f"<html><head><title>{safe_title}</title></head>\n"
        f"<body>\n<h2>{safe_title}</h2>\n<p>{html.escape(message)}</p>\n</body></html>"
    )
    return HTMLResponse(content=content, status_code=status_code)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/api/connectors/magicpath/status")
async def magicpath_status() -> JSONResponse:
    """Install + auth snapshot. Never errors — probe failures read as false.

    `logged_in` is live auth state (whoami round-trips to api.magicpath.ai),
    so a valid session with the network down reports false.
    """
    cli = _cli_binary()
    cli_version: str | None = None
    user: dict | None = None
    if cli:
        version_result = await run([cli, "--version"], timeout=VERSION_TIMEOUT_SECONDS)
        if version_result.ok and version_result.output.strip():
            cli_version = version_result.output.strip().splitlines()[-1].strip()
        user = await _whoami(cli)
    return JSONResponse({
        "skill_installed": SKILL_MD.is_file(),
        "cli_installed": cli is not None,
        "cli_version": cli_version,
        "logged_in": user is not None,
        "user": user,
    })


@router.post("/api/connectors/magicpath/setup")
async def magicpath_setup() -> JSONResponse:
    """Install the MagicPath skill + CLI. Idempotent; one run at a time.

    Raw npm/npx output stays server-side (same contract as skills.py — output
    can embed registry URLs and host paths); clients get categorical slugs.
    """
    if _setup_lock.locked():
        raise HTTPException(409, detail="magicpath setup is already running")
    async with _setup_lock:
        _ensure_node_on_path()
        skill = await _install_skill()
        cli = await _install_cli()  # independent of the skill step's outcome
    return JSONResponse({
        "ok": skill["ok"] and cli["ok"],
        "skill": skill,
        "cli": cli,
        "summary": f"{_describe_step('skill', skill)}; {_describe_step('CLI', cli)}",
    })


@router.post("/api/connectors/magicpath/login")
async def magicpath_login(body: LoginBody | None = None) -> JSONResponse:
    """Login bootstrap + code exchange.

    No code (missing body, {}, or blank code) → returns the browser login URL;
    the user approves there and MagicPath displays an authorization code. With
    a code → exchanges it for a CLI session. The two 200 shapes are disjoint:
    `login_url` presence marks the bootstrap response.
    """
    raw = (body.code if body is not None else None) or ""
    if not raw.strip():
        # No ?port= on purpose: without a valid port param MagicPath's auth
        # page skips the http://localhost:<port>/callback redirect and instead
        # displays the authorization code with a copy button — the only flow
        # that works when this API runs on a remote workspace with no port
        # forward. `port` stays in the response for shape compatibility;
        # /callback still handles redirects for setups that do forward the port.
        return JSONResponse({
            "login_url": f"{WEB_URL}/auth/cli",
            "port": LOGIN_PORT,
            "cli_installed": _cli_binary() is not None,
            "instructions": (
                "Open login_url in any browser and approve the request. "
                "MagicPath then displays an authorization code with a copy "
                "button. POST that code back to this same endpoint as "
                '{"code": "<code>"}. No port forwarding is needed.'
            ),
        })
    code = _extract_code(raw)
    if code is None:
        raise HTTPException(400, detail={
            "error": "invalid_code_format",
            "detail": "Expected a MagicPath authorization code (JWT) or the full callback URL containing ?code=...",
            "suggestion": "After approving login in the browser, copy the complete URL from the address bar and send that.",
        })
    cli = _cli_binary()
    if cli is None:
        raise HTTPException(400, detail={
            "error": "cli_not_installed",
            "detail": "The magicpath-ai CLI is not installed.",
            "suggestion": "POST /api/connectors/magicpath/setup first.",
        })
    if _session_lock.locked():
        raise HTTPException(409, detail="another magicpath login/logout is in progress")
    async with _session_lock:
        result = await _exchange_code(cli, code)
        if result.timed_out:
            raise HTTPException(504, detail={
                "error": "exchange_timeout",
                "detail": f"magicpath-ai login did not finish within {LOGIN_TIMEOUT_SECONDS}s.",
                "suggestion": "Check network access to api.magicpath.ai and retry.",
            })
        if not result.ok:
            log.warning(
                "magicpath: code exchange failed (exit %s, %.1fs)",
                result.returncode, result.duration_seconds,
            )
            raise HTTPException(400, detail={
                "error": "exchange_failed",
                "detail": "MagicPath rejected the authorization code (it may be expired or already used).",
                "suggestion": "POST this endpoint again with no code to get a fresh login link, then retry.",
            })
        user = await _whoami(cli)
    return JSONResponse({"ok": True, "user": user})


@router.post("/api/connectors/magicpath/logout")
async def magicpath_logout() -> JSONResponse:
    """Log out (idempotent — already-logged-out still returns 200)."""
    cli = _cli_binary()
    if cli is None:
        raise HTTPException(400, detail={
            "error": "cli_not_installed",
            "detail": "The magicpath-ai CLI is not installed.",
            "suggestion": "POST /api/connectors/magicpath/setup first.",
        })
    if _session_lock.locked():
        raise HTTPException(409, detail="another magicpath login/logout is in progress")
    async with _session_lock:
        result = await run([cli, "logout"], timeout=LOGOUT_TIMEOUT_SECONDS)
    if not result.ok:
        log.warning("magicpath: logout failed (exit %s)", result.returncode)
        raise HTTPException(502, detail={
            "error": "logout_failed",
            "detail": "magicpath-ai logout did not complete.",
            "suggestion": "Retry; if it persists check the server log.",
        })
    return JSONResponse({"ok": True, "logged_in": False})


@router.get("/callback", include_in_schema=False)
async def magicpath_or_vercel_callback(
    code: str = Query(default=None),
    state: str = Query(default=None),
    error: str = Query(default=None),
    error_description: str = Query(default=None),
) -> HTMLResponse:
    """Shared /callback dispatcher (see module docstring).

    MagicPath requests are exactly `?code=<JWT>` with no state/error; every
    other shape — Vercel's state-carrying redirects, error redirects, and
    malformed requests — is delegated to the Vercel handler, which reproduces
    the pre-existing behavior byte-for-byte.
    """
    is_magicpath = (
        state is None and error is None
        and code is not None and _JWT_RE.match(code) is not None
    )
    if not is_magicpath:
        return await vercel_oauth_callback(
            code=code, state=state, error=error, error_description=error_description,
        )

    cli = _cli_binary()
    if cli is None:
        return _callback_page(
            "MagicPath login failed",
            "The magicpath-ai CLI is not installed on the workspace. "
            "POST /api/connectors/magicpath/setup, then start again by POSTing "
            "/api/connectors/magicpath/login with no code.",
            status_code=400,
        )
    if _session_lock.locked():
        return _callback_page(
            "MagicPath login busy",
            "Another MagicPath login or logout is in progress. Close this tab and retry in a moment.",
            status_code=409,
        )
    async with _session_lock:
        result = await _exchange_code(cli, code)
        if not result.ok:
            log.warning(
                "magicpath: callback code exchange failed (exit %s, timed_out=%s)",
                result.returncode, result.timed_out,
            )
            return _callback_page(
                "MagicPath login failed",
                "The authorization code was rejected (it may be expired or already used). "
                "Get a fresh link by POSTing /api/connectors/magicpath/login with no code and try again.",
                status_code=400,
            )
        user = await _whoami(cli)
    identity = (user or {}).get("email") or (user or {}).get("name") or "your MagicPath account"
    return _callback_page(
        "MagicPath connected",
        f"Signed in as {identity}. You can close this tab and return to your workspace.",
        status_code=200,
    )

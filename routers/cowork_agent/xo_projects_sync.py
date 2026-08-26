"""
xo-projects backup/restore router.

All endpoints under ``/api/xo-projects-sync``. The router is thin: it
maps HTTP requests to ``services.cowork_agent.xo_projects_sync``
functions, translates domain errors into the right HTTP status codes,
and never holds long-running logic itself.

Repo model: each xo-project lives in its own private GitHub repo named
``xo-project-<project_id>``. Repos are created lazily on first backup
of that project — no upfront repo creation at setup.

Auth + config preconditions:
- Every endpoint except ``/setup`` requires ``SyncConfig.configured``
  (passphrase in env) AND a resolvable GitHub token. Failing either
  returns a structured 400/401 with the exact next step the user (or
  the agent on their behalf) needs to take.
- ``/setup`` only persists the passphrase; it does NOT touch GitHub
  repos. The first per-project backup is what creates that project's
  remote.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.cowork_agent.xo_projects_sync import backup as backup_mod
from services.cowork_agent.xo_projects_sync import config as cfg_mod
from services.cowork_agent.xo_projects_sync import crypto, github
from services.cowork_agent.xo_projects_sync import restore as restore_mod


log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/xo-projects-sync", tags=["xo-projects-sync"])


# ── Request bodies ───────────────────────────────────────────────────────────


class SetupBody(BaseModel):
    passphrase: str = Field(..., min_length=1,
                             description="Symmetric encryption passphrase. Required to restore on any machine.")
    confirm_rotation: bool = Field(
        False,
        description=(
            "Required to be true when overwriting an existing, different "
            "passphrase. Rotating the passphrase makes every previously-"
            "encrypted snapshot permanently unrecoverable."
        ),
    )


class BackupBody(BaseModel):
    note: str | None = Field(None, max_length=500,
                              description="Optional short note appended to the commit message.")


class RestoreBody(BaseModel):
    snapshot_id: str | None = Field(None,
                                     description="YYYYMMDD-HHMMSS snapshot id; defaults to latest.")
    force: bool = Field(False,
                         description="Overwrite the local project folder if it exists. Destructive.")


class RestoreAllBody(BaseModel):
    snapshot_id_map: dict[str, str] | None = Field(None,
                                                    description="Per-project snapshot pins; missing projects use latest.")
    force: bool = False


# ── Auth/config helpers ──────────────────────────────────────────────────────


async def _require_config_and_auth() -> tuple[cfg_mod.SyncConfig, github.GitHubAuth, str]:
    """Common precondition for every operation post-setup.

    Returns (config, auth, owner). Raises HTTPException with actionable
    detail when either side is missing. The per-project repo URL is
    derived inside the service layer; there is no shared repo URL.
    """
    cfg = cfg_mod.load_config()
    if not cfg.configured:
        raise HTTPException(400, detail={
            "error": "not_configured",
            "detail": "Backup is not configured.",
            "suggestion": "POST /api/xo-projects-sync/setup with {passphrase} first.",
        })

    try:
        auth = await github.resolve_auth()
    except github.AuthMissingError as exc:
        raise HTTPException(401, detail={
            "error": "github_auth_missing",
            "detail": str(exc),
            "suggestion": (
                "Either complete the GitHub connector flow in the XO Cowork UI, "
                f"or add {cfg_mod.ENV_GITHUB_PAT}=<your_token> to the xo-space checkout's .env."
            ),
        })

    try:
        owner = await github.discover_owner(auth)
    except github.GitHubAPIError as exc:
        raise HTTPException(exc.status, detail={
            "error": "github_identity_failed",
            "detail": exc.message,
        })

    return cfg, auth, owner


# ── /setup ───────────────────────────────────────────────────────────────────


@router.post("/setup")
async def setup(body: SetupBody) -> JSONResponse:
    """Bootstrap config. Does NOT create any repos — those are created lazily on first backup.

    Steps:
      1. Verify GPG is installed (we'd fail at first backup otherwise).
      2. Resolve a GitHub token; fail 401 with a clear message if missing.
      3. Discover the owner (lets the response confirm which account we're configured against).
      4. Persist passphrase into the xo-space checkout's .env and os.environ.
    """
    try:
        crypto.check_gpg_available()
    except crypto.GpgUnavailableError as exc:
        raise HTTPException(500, detail={"error": "gpg_missing", "detail": str(exc)})

    # Rotation guardrail: if a passphrase is already set and the caller
    # is trying to replace it with a *different* one, refuse unless the
    # body explicitly confirms. Setting the same passphrase twice is a
    # safe no-op and is allowed without confirmation.
    existing = cfg_mod.load_config()
    if (
        existing.configured
        and existing.passphrase != body.passphrase
        and not body.confirm_rotation
    ):
        raise HTTPException(409, detail={
            "error": "passphrase_already_set",
            "detail": (
                "A backup passphrase is already configured. Re-running "
                "/setup with a different passphrase rotates it — every "
                "existing snapshot was encrypted with the old passphrase "
                "and will be permanently unrecoverable after rotation."
            ),
            "suggestion": (
                "If the user has explicitly confirmed they don't need to "
                "restore from existing snapshots, re-send with "
                "`confirm_rotation: true`. To keep old backups restorable, "
                "restore them under the current passphrase first, then "
                "rotate, then back up again."
            ),
        })

    try:
        auth = await github.resolve_auth()
    except github.AuthMissingError as exc:
        raise HTTPException(401, detail={
            "error": "github_auth_missing",
            "detail": str(exc),
            "suggestion": (
                "Either complete the GitHub connector flow in the XO Cowork UI, "
                f"or add {cfg_mod.ENV_GITHUB_PAT}=<your_token> to the xo-space checkout's .env."
            ),
        })

    try:
        owner = await github.discover_owner(auth)
    except github.GitHubAPIError as exc:
        raise HTTPException(exc.status, detail={
            "error": "github_identity_failed",
            "detail": exc.message,
        })

    cfg_mod.upsert_env({cfg_mod.ENV_PASSPHRASE: body.passphrase})

    return JSONResponse({
        "configured": True,
        "repo_owner": owner,
        "token_source": auth.source,
    })


# ── /status ──────────────────────────────────────────────────────────────────


@router.get("/status")
async def status() -> JSONResponse:
    """Lightweight: report current config + token source. No GitHub network calls."""
    cfg = cfg_mod.load_config()
    token_source: str | None
    try:
        auth = await github.resolve_auth()
        token_source = auth.source
    except github.AuthMissingError:
        token_source = None
    try:
        crypto.check_gpg_available()
        gpg_ok = True
    except crypto.GpgUnavailableError:
        gpg_ok = False
    return JSONResponse({
        "configured": cfg.configured,
        "token_source": token_source,
        "gpg_available": gpg_ok,
    })


# ── /projects ────────────────────────────────────────────────────────────────


@router.get("/projects")
async def list_projects_in_repo() -> JSONResponse:
    _cfg, auth, owner = await _require_config_and_auth()
    try:
        projects = await restore_mod.list_remote_projects(auth=auth, owner=owner)
    except github.GitHubAPIError as exc:
        raise HTTPException(exc.status, detail={"error": "github_list_failed", "detail": exc.message})
    except RuntimeError as exc:
        raise HTTPException(502, detail={"error": "git_failed", "detail": str(exc)})
    return JSONResponse([p.to_dict() for p in projects])


# ── /projects/{id} (backup one) ──────────────────────────────────────────────


@router.post("/projects/{project_id}")
async def backup_project(project_id: str, body: BackupBody | None = None) -> JSONResponse:
    cfg, auth, owner = await _require_config_and_auth()
    note = body.note if body else None
    try:
        result = await backup_mod.backup_one(
            project_id, cfg=cfg, auth=auth, owner=owner, note=note,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, detail={"error": "project_not_found", "detail": str(exc)})
    except github.GitHubAPIError as exc:
        raise HTTPException(502, detail={
            "error": "repo_create_failed",
            "detail": exc.message,
            "suggestion": (
                "Token may lack `repo` scope. Regenerate the PAT with `repo` "
                "scope, or complete `gh auth login` via the UI."
            ),
        })
    except RuntimeError as exc:
        raise HTTPException(502, detail={"error": "backup_failed", "detail": str(exc)})

    if not result.ok:
        raise HTTPException(500, detail={"error": "backup_failed", "detail": result.error})
    return JSONResponse(result.to_dict())


# ── /all (backup all) ────────────────────────────────────────────────────────


@router.post("/all")
async def backup_all_projects(body: BackupBody | None = None) -> JSONResponse:
    cfg, auth, owner = await _require_config_and_auth()
    note = body.note if body else None
    results = await backup_mod.backup_all(
        cfg=cfg, auth=auth, owner=owner, note=note,
    )
    return JSONResponse([r.to_dict() for r in results])


# ── /projects/{id}/restore ───────────────────────────────────────────────────


@router.post("/projects/{project_id}/restore")
async def restore_project(project_id: str, body: RestoreBody | None = None) -> JSONResponse:
    cfg, auth, owner = await _require_config_and_auth()
    snapshot_id = body.snapshot_id if body else None
    force = body.force if body else False
    try:
        result = await restore_mod.restore_one(
            project_id, cfg=cfg, auth=auth, owner=owner,
            snapshot_id=snapshot_id, force=force,
        )
    except restore_mod.ProjectExistsError as exc:
        raise HTTPException(409, detail={
            "error": "project_exists",
            "detail": str(exc),
            "suggestion": "Pass force=true in the body to overwrite; existing local data will be lost.",
        })
    except restore_mod.SnapshotNotFoundError as exc:
        raise HTTPException(404, detail={"error": "snapshot_not_found", "detail": str(exc)})
    except restore_mod.ChecksumMismatchError as exc:
        raise HTTPException(502, detail={"error": "verify_failed", "detail": str(exc)})
    except RuntimeError as exc:
        raise HTTPException(500, detail={"error": "restore_failed", "detail": str(exc)})
    return JSONResponse(result.to_dict())


# ── /all/restore ─────────────────────────────────────────────────────────────


@router.post("/all/restore")
async def restore_all_projects(body: RestoreAllBody | None = None) -> JSONResponse:
    cfg, auth, owner = await _require_config_and_auth()
    snapshot_id_map = body.snapshot_id_map if body else None
    force = body.force if body else False
    results = await restore_mod.restore_all(
        cfg=cfg, auth=auth, owner=owner,
        snapshot_id_map=snapshot_id_map, force=force,
    )
    return JSONResponse([r.to_dict() for r in results])

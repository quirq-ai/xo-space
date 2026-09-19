"""The settings module's routes: the runtime settings, the secrets and the
onboarding state, at the paths the four routers they came from always
served (the manifest's aliases outside ``/api/settings``).

  GET    /api/runtime-config              the Setup status payload (configured, applied, restart state, roots, agents, paths)
  PUT    /api/runtime-config              save the runtime settings: {agent_name, watcher_enabled, watcher_interval_seconds, watcher_source_mode}
  PUT    /api/runtime-config/roots        save the roots: {xo_projects_root, quirq_state_root}
  POST   /api/runtime-config/restart      compatibility alias of POST /space/server/restart

  GET    /api/secrets                     the masked listing {items: [{key, is_set, preview}], total}
  GET    /api/secrets/{key}/reveal        {key, value} for one key
  PUT    /api/secrets                     replace the store: {items: [{key, value}]}; the listing after
  PATCH  /api/secrets/{key}               set one key: {value}; its summary
  DELETE /api/secrets/{key}               {key, deleted}; idempotent

  GET    /api/secrets/env                 the legacy whole-file view {entries: [{key, value}]}
  GET    /api/secrets/env/keys            {keys: [...]} with a non-empty value; no secret material
  PUT    /api/secrets/env                 overwrite the store with {entries: [{key, value}]}

  GET    /api/onboarding                  {completed, completed_at}
  POST   /api/onboarding/complete         mark the first-run flow done; {ok}

Thin handlers over ``modules.settings.service``: parse, call it, return. Its
typed errors carry their own status (400 with the bare message for a save
the validator refused, as the runtime routes always answered; 400
``invalid_key`` / ``invalid_value`` / ``duplicate_key``, 404
``key_not_found`` and 500 ``scope_unavailable`` for the curated secret
routes; 500 with the bare message for the legacy whole-file routes) and
reach the wire through the app's service error handler. Bodies keep their
shapes: the pydantic models below are the ones the old routers declared.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from services.errors import ServiceError

from . import service

router = APIRouter()


# ── Bodies and responses ─────────────────────────────────────────────────────


class RuntimeConfigRequest(BaseModel):
    agent_name: str
    watcher_enabled: bool
    watcher_interval_seconds: float
    watcher_source_mode: str


class RootConfigRequest(BaseModel):
    xo_projects_root: str
    quirq_state_root: str


class SecretSummary(BaseModel):
    key: str
    is_set: bool
    preview: Optional[str] = None


class ListSecretsResponse(BaseModel):
    items: list[SecretSummary]
    total: int


class RevealResponse(BaseModel):
    key: str
    value: str


class SecretItem(BaseModel):
    key: str
    value: str


class PutSecretsRequest(BaseModel):
    items: list[SecretItem]


class PatchSecretRequest(BaseModel):
    value: str


class DeleteSecretResponse(BaseModel):
    key: str
    deleted: bool


# ── Runtime settings and roots ───────────────────────────────────────────────


@router.get("/api/runtime-config")
def get_runtime_config() -> dict:
    """The Setup status: configured and applied settings, restart state, roots, agents, paths."""
    return service.runtime_status()


@router.put("/api/runtime-config")
def put_runtime_config(body: RuntimeConfigRequest) -> dict:
    """Save the runtime settings to runtime.env; they apply on the next restart."""
    saved = service.save_settings(body.model_dump())
    return {"ok": True, "saved": saved, "status": service.runtime_status()}


@router.put("/api/runtime-config/roots")
def put_runtime_roots(body: RootConfigRequest) -> dict:
    """Save the projects root and the state root to roots.env; they apply on the next restart."""
    saved = service.save_root_settings(body.model_dump())
    return {"ok": True, "saved": saved, "status": service.runtime_status()}


@router.post("/api/runtime-config/restart")
async def restart_runtime(request: Request) -> dict:
    """Compatibility alias for the Setup process control."""
    from routers.space import space_server_restart

    return await space_server_restart(request)


# ── Secrets: the legacy whole-file view ──────────────────────────────────────


@router.get("/api/secrets/env")
async def get_env_secrets() -> dict:
    """Return the active secrets store as a list of key-value entries."""
    return {"entries": service.env_entries()}


@router.get("/api/secrets/env/keys")
async def get_env_keys() -> dict:
    """Return only the keys with non-empty values; no secret material is transmitted."""
    return {"keys": service.env_keys()}


@router.put("/api/secrets/env")
async def put_env_secrets(request: Request) -> dict:
    """Overwrite the active secrets store with the provided entries."""
    body = await request.json()
    if not isinstance(body, dict):
        raise ServiceError(None, "The body must be an object with entries.", 400)
    service.save_env_entries(body.get("entries", []))
    return {"ok": True}


# ── Secrets: the curated routes ──────────────────────────────────────────────


@router.get("/api/secrets", response_model=ListSecretsResponse)
def list_secrets() -> ListSecretsResponse:
    """List the saved secrets with fixed masks; values never cross the wire."""
    return ListSecretsResponse(**service.list_secrets())


@router.get("/api/secrets/{key}/reveal", response_model=RevealResponse)
def reveal_secret(key: str) -> RevealResponse:
    """The raw value of one key."""
    return RevealResponse(**service.reveal_secret(key))


@router.put("/api/secrets", response_model=ListSecretsResponse)
def put_secrets(body: PutSecretsRequest) -> ListSecretsResponse:
    """Replace the whole store; the masked listing after."""
    return ListSecretsResponse(**service.put_secrets([item.model_dump() for item in body.items]))


@router.patch("/api/secrets/{key}", response_model=SecretSummary)
def patch_secret(key: str, body: PatchSecretRequest) -> SecretSummary:
    """Set or update one key."""
    return SecretSummary(**service.patch_secret(key, body.value))


@router.delete("/api/secrets/{key}", response_model=DeleteSecretResponse)
def delete_secret(key: str) -> DeleteSecretResponse:
    """Delete one key; idempotent."""
    return DeleteSecretResponse(**service.delete_secret(key))


# ── Onboarding ───────────────────────────────────────────────────────────────


@router.get("/api/onboarding")
def onboarding_status() -> dict:
    """Whether the first-run flow was completed, and when."""
    return service.onboarding_status()


@router.post("/api/onboarding/complete")
def onboarding_complete() -> dict:
    """Mark the first-run flow completed, now."""
    service.complete_onboarding()
    return {"ok": True}

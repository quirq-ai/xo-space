"""BFF secrets routes — read & edit with curated shape.

Curated wrapper over services/cowork_agent/agent_env via the
SecretsScope handle in services.cowork_agent.scopes. Five routes:

  GET    /api/secrets                  list with masked previews
  GET    /api/secrets/{key}/reveal     raw value for one key
  PUT    /api/secrets                  bulk replace
  PATCH  /api/secrets/{key}            single set/update
  DELETE /api/secrets/{key}            single hard-delete (idempotent)

See docs/bff-endpoints-design.md §9.2 for the full contract.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api._filters import (
    HIDDEN_KEYS,
    is_valid_key,
    is_valid_value,
    preview_value,
)
from services.cowork_agent import scopes

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Pydantic models ───────────────────────────────────────────────────────────


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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _shape(entries: list[dict]) -> list[SecretSummary]:
    """Filter on the way out (P4): drop hidden keys, drop malformed
    keys (log WARN), and convert to typed summaries."""
    out: list[SecretSummary] = []
    for e in entries:
        key = (e.get("key") or "").strip()
        if not key:
            continue
        if not is_valid_key(key):
            logger.warning("Skipping malformed key in secrets store: %r", key)
            continue
        if key in HIDDEN_KEYS:
            continue
        value = e.get("value") or ""
        if not is_valid_value(value):
            # Lenient-on-read: surface as is_set=false rather than rejecting.
            out.append(SecretSummary(key=key, is_set=False, preview=None))
            continue
        is_set = bool(value.strip())
        out.append(
            SecretSummary(
                key=key,
                is_set=is_set,
                preview=preview_value(value) if is_set else None,
            )
        )
    out.sort(key=lambda s: s.key)
    return out


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": code, "message": message})


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "key_not_found", "message": "Secret not found."},
    )


def _scope_unavailable() -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={
            "code": "scope_unavailable",
            "message": "Secrets store is not accessible.",
        },
    )


def _require_valid_key(key: str) -> None:
    if not is_valid_key(key):
        raise _bad_request("invalid_key", "Key must match ^[A-Z_][A-Z0-9_]*$.")


def _require_valid_value(value: str) -> None:
    if not is_valid_value(value):
        raise _bad_request(
            "invalid_value", "Value must not contain newline or null bytes."
        )


def _require_writable_key(key: str) -> None:
    _require_valid_key(key)
    if key in HIDDEN_KEYS:
        raise _bad_request("invalid_key", "Key is reserved and cannot be modified.")


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("", response_model=ListSecretsResponse)
def list_secrets() -> ListSecretsResponse:
    handle = scopes.resolve_scope("secrets")
    try:
        entries = handle.load()
    except OSError as exc:
        raise _scope_unavailable() from exc
    items = _shape(entries)
    return ListSecretsResponse(items=items, total=len(items))


@router.get("/{key}/reveal", response_model=RevealResponse)
def reveal_secret(key: str) -> RevealResponse:
    _require_valid_key(key)
    if key in HIDDEN_KEYS:
        raise _not_found()
    handle = scopes.resolve_scope("secrets")
    try:
        entries = handle.load()
    except OSError as exc:
        raise _scope_unavailable() from exc
    entry = next((e for e in entries if e.get("key") == key), None)
    if entry is None:
        raise _not_found()
    return RevealResponse(key=key, value=entry.get("value") or "")


@router.put("", response_model=ListSecretsResponse)
def put_secrets(body: PutSecretsRequest) -> ListSecretsResponse:
    seen: set[str] = set()
    cleaned: list[dict] = []
    for item in body.items:
        key = item.key.strip()
        _require_writable_key(key)
        _require_valid_value(item.value)
        if key in seen:
            raise _bad_request("duplicate_key", f"Duplicate key in items: {key}")
        seen.add(key)
        cleaned.append({"key": key, "value": item.value})

    handle = scopes.resolve_scope("secrets")
    try:
        handle.save(cleaned)
        entries = handle.load()
    except OSError as exc:
        raise _scope_unavailable() from exc
    items = _shape(entries)
    return ListSecretsResponse(items=items, total=len(items))


@router.patch("/{key}", response_model=SecretSummary)
def patch_secret(key: str, body: PatchSecretRequest) -> SecretSummary:
    _require_writable_key(key)
    _require_valid_value(body.value)
    handle = scopes.resolve_scope("secrets")
    try:
        handle.upsert(key, body.value)
    except OSError as exc:
        raise _scope_unavailable() from exc
    is_set = bool(body.value.strip())
    return SecretSummary(
        key=key,
        is_set=is_set,
        preview=preview_value(body.value) if is_set else None,
    )


@router.delete("/{key}", response_model=DeleteSecretResponse)
def delete_secret(key: str) -> DeleteSecretResponse:
    _require_valid_key(key)
    if key in HIDDEN_KEYS:
        return DeleteSecretResponse(key=key, deleted=False)
    handle = scopes.resolve_scope("secrets")
    try:
        deleted = handle.delete(key)
    except OSError as exc:
        raise _scope_unavailable() from exc
    return DeleteSecretResponse(key=key, deleted=deleted)

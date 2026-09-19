"""
Legacy secrets endpoints backed by the active secrets store.

GET parses the file into key/value entries, PUT overwrites it with a new list
of entries. New frontend code should use the curated ``/api/secrets`` BFF
routes, which never return plaintext values from list operations.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.cowork_agent.registry.agent_env import load_env_entries, save_env_entries

router = APIRouter()


@router.get("/env")
async def get_env_secrets():
    """Return the active secrets store as a list of key-value entries."""
    try:
        return {"entries": load_env_entries()}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.get("/env/keys")
async def get_env_keys():
    """Return only the keys with non-empty values — no secret material is
    transmitted. Used by onboarding to detect which provider keys are
    already configured without the full /env payload (and without
    sending plaintext values to the browser)."""
    try:
        keys = [
            e["key"]
            for e in load_env_entries()
            if (e.get("value") or "").strip()
        ]
        return {"keys": keys}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.put("/env")
async def put_env_secrets(request: Request):
    """Overwrite the active secrets store with the provided entries."""
    body = await request.json()
    entries = body.get("entries", [])
    try:
        save_env_entries(entries)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

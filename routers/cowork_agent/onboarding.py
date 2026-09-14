"""
Onboarding state — persisted on disk so the first-run flow does not
re-trigger when the user opens xo-cowork in a new browser, incognito
window, or after clearing localStorage.

Storage: ~/.quirq/settings/onboarding.json (see
`services/cowork_agent/xo_cowork_state.py`).
"""

from datetime import datetime, timezone

from fastapi import APIRouter

from services.cowork_agent.xo_cowork_state import get_state, update_state

router = APIRouter()


@router.get("/api/onboarding")
def onboarding_status():
    state = get_state()
    return {
        "completed": bool(state.get("onboarding_completed")),
        "completed_at": state.get("onboarding_completed_at"),
    }


@router.post("/api/onboarding/complete")
def onboarding_complete():
    update_state({
        "onboarding_completed": True,
        "onboarding_completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    return {"ok": True}

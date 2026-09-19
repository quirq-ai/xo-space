"""``GET /api/messages/{session_id}``: a session's messages, paged.

The handler lives with the other session readers in ``api/sessions/routes.py``;
this folder only gives it its URL.
"""

from fastapi import APIRouter

from api.sessions.routes import get_messages

router = APIRouter()
router.get("/{session_id}")(get_messages)

"""Swarm calls for Plane-A chat storage (/ask_question saves the exchange).
Frozen behaviour, moved here so the swarm has one door; the prints are the
original ones because the legacy log format is what operators grep for."""
from __future__ import annotations

from typing import Any, Optional

from ._http import request


class ChatAPIClient:
    """Client for the swarm's chat storage endpoints."""

    async def push_message(
        self,
        project_id: str,
        user_id: str,
        message: str,
        message_type: str = "@xo",
    ) -> Optional[dict[str, Any]]:
        res = await request("POST", "/chat/add_message", json={
            "project_id": project_id, "user_id": user_id,
            "message": message, "type": message_type,
        })
        if res.ok:
            print(f"✅ Pushed message: project={project_id}, type={message_type}")
            return res.data if isinstance(res.data, dict) else {}
        if res.offline or res.unauthenticated:
            print(f"⚠️ Chat API error: {res.detail}")
        else:
            print(f"⚠️ Failed to push message: {res.status} - {res.text}")
        return None

    async def fetch_messages(self, project_id: str, limit: int = 50) -> Optional[list]:
        res = await request("GET", "/chat/get_messages", params={"project_id": project_id, "limit": limit})
        if res.ok:
            messages = (res.data or {}).get("messages", []) if isinstance(res.data, dict) else []
            print(f"✅ Fetched {len(messages)} messages: project={project_id}")
            return messages
        if res.offline or res.unauthenticated:
            print(f"⚠️ Chat API error: {res.detail}")
        else:
            print(f"⚠️ Failed to fetch messages: {res.status} - {res.text}")
        return None

    async def get_message_count(self, project_id: str) -> int:
        messages = await self.fetch_messages(project_id, limit=100)
        return len(messages) if messages else 0

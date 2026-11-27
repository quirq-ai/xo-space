"""The one HTTP call every provider client makes: read the provider's key
from ``router.env`` (per call, so connecting it takes effect at once), POST
JSON with it as a bearer token, and turn what came back into a :class:`Reply`.

It never retries: a decision that arrives late is worth less than the default
it would have replaced. A transport failure is a reply, not an exception;
anything else raised here is a bug and propagates.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import httpx

from services.decision_models import keys

USER_AGENT = "xo-space"


@dataclass
class Reply:
    """``kind`` is None on success (a 2xx with a JSON object body, in
    ``data``), else one word for decision records: ``not_configured`` (no
    key, nothing sent), ``timeout``, ``offline``, ``bad_response`` or the
    provider's word for the HTTP status."""

    kind: Optional[str] = None
    data: Any = None

    @property
    def ok(self) -> bool:
        return self.kind is None


async def post(url: str, body: dict, *, env_key: str, timeout: float, kinds: Mapping[int, str]) -> Reply:
    """POST ``body`` to ``url`` with the key named ``env_key``. ``kinds`` maps
    a provider's error statuses to their kind; any other is ``server`` (5xx)
    or ``http``."""
    key = await asyncio.to_thread(keys.read, env_key)
    if key is None:
        return Reply("not_configured")
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.post(url, json=body, headers={"Authorization": f"Bearer {key}",
                                                            "User-Agent": USER_AGENT})
    except httpx.TimeoutException:
        return Reply("timeout")
    except httpx.HTTPError:
        return Reply("offline")
    if not 200 <= resp.status_code < 300:
        return Reply(kinds.get(resp.status_code, "server" if resp.status_code >= 500 else "http"))
    try:
        data = resp.json()
    except ValueError:
        data = None
    return Reply(data=data) if isinstance(data, dict) else Reply("bad_response")

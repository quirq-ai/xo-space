"""The one HTTP door to Levanto Sage.

Everything xo-space asks Sage goes through :func:`decide`: this module is the
only place that knows the base URL, reads ``LEVANTO_API_KEY``, and shapes a
failure. It never raises and never retries (a decision that arrives late is
worth less than the default it would have replaced); callers get a
:class:`SageResult` and decide what a failure means for their feature.

The key is read per call, so setting it in Setup → Secrets takes effect
without a restart, and it is only ever sent in the ``Authorization`` header.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://sage.levanto.ai"
ENV_API_KEY = "LEVANTO_API_KEY"
USER_AGENT = "xo-space"


def api_key() -> str | None:
    key = (os.getenv(ENV_API_KEY, "") or "").strip()
    return key or None


def configured() -> bool:
    return api_key() is not None


@dataclass
class SageResult:
    """What a Sage call came back with, never an exception.

    ok              2xx with a JSON object body
    status          HTTP status; 0 when no response arrived
    data            the parsed body on success
    detail          one human sentence: Sage's ``detail`` when it sent one,
                    else "Sage returned NNN", else the transport error
    latency_ms      wall time of the call
    not_configured  no API key, so nothing was sent
    timed_out       no answer within the timeout
    offline         the request never reached Sage (DNS, refused, TLS)
    """

    ok: bool
    status: int = 0
    data: Any = None
    detail: str = ""
    latency_ms: float = 0.0
    not_configured: bool = False
    timed_out: bool = False
    offline: bool = False

    @property
    def kind(self) -> str | None:
        """``None`` on success, else one word for logs and decision records."""
        if self.ok:
            return None
        if self.not_configured:
            return "not_configured"
        if self.timed_out:
            return "timeout"
        if self.offline:
            return "offline"
        if self.status == 401:
            return "auth"
        if self.status == 402:
            return "balance"
        if self.status in (400, 422):
            return "invalid"
        if self.status == 429:
            return "rate"
        if self.status >= 500:
            return "server"
        if 200 <= self.status < 300:
            return "bad_response"
        return "http"


def _detail_from(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str) and detail:
            return detail[:300]
    except Exception:  # noqa: BLE001 - a non-JSON error body still has a status
        pass
    return f"Sage returned {resp.status_code}"


async def decide(
    content: str,
    question: dict,
    *,
    timeout: float,
    reasoning: str | None = None,
) -> SageResult:
    """One ``POST /decide``: ``content`` plus one ``question``."""
    key = api_key()
    if key is None:
        return SageResult(ok=False, detail=f"{ENV_API_KEY} is not set", not_configured=True)
    body: dict[str, Any] = {"content": content, "question": question}
    if reasoning is not None:
        body["reasoning"] = reasoning
    headers = {"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT}
    started = time.perf_counter()

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 1)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{BASE_URL}/decide", json=body, headers=headers)
    except httpx.TimeoutException:
        return SageResult(ok=False, detail=f"Sage did not answer within {timeout:g}s",
                          latency_ms=elapsed(), timed_out=True)
    except Exception as exc:  # noqa: BLE001 - a network failure is a result, not a crash
        log.debug("levanto: /decide failed: %s", exc)
        return SageResult(ok=False, detail=f"Sage is unreachable: {type(exc).__name__}",
                          latency_ms=elapsed(), offline=True)

    latency = elapsed()
    if not 200 <= resp.status_code < 300:
        return SageResult(ok=False, status=resp.status_code, detail=_detail_from(resp), latency_ms=latency)
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = None
    if not isinstance(data, dict):
        return SageResult(ok=False, status=resp.status_code, detail="Sage returned a body that is not a JSON object",
                          latency_ms=latency)
    return SageResult(ok=True, status=resp.status_code, data=data, latency_ms=latency)

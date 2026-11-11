"""Claude's usage, read from Anthropic with no model call (contract 1.1,
``UsageReader``; storm plan W4, Step 0).

The same read Claude Code's own ``/usage`` makes: ``GET /api/oauth/usage``
with the claude.ai login's access token. It answers every window the
account has (the 5-hour and the weekly, and a per-model weekly one when it
has one), with the share used and the reset, and whether extra usage
(paid, beyond the plan) is on. It calls no model and spends nothing.

* **The token is claude's own, and only read.** It comes from
  ``~/.claude/.credentials.json``, the login claude runs on whenever one is
  usable (``ClaudeCodeAdapter.apply_auth``). It is never refreshed here: a
  refresh would rotate the login under the CLI. An expired token gives
  ``auth_required``, and the read works again once claude's next turn has
  renewed it. Nothing here logs or keeps the token.
* **No claude.ai login** (an API key or a token in the environment): there
  is nothing to read with, so usage is what claude's turns report.
* **The vendor's warning thresholds aren't in this answer**, so a window
  reads ``limited`` at 100 % (or locked) and ``ok`` below it; a warning is
  what the turns report.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from services.cowork_agent.adapters.claude_code.runtime.account import CREDENTIAL_FILES
from services.cowork_agent.adapters.contract import PaidUsage, RuntimeFailure, UsageSnapshot, UsageWindow
from services.timestamps import TS_FORMAT, parse_ts

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
#: The beta the claude.ai login's endpoints answer under.
OAUTH_BETA = "oauth-2025-04-20"
READ_TIMEOUT_S = 10.0
#: A token this close to expiring isn't used: it could lapse on the way.
EXPIRY_MARGIN_S = 60.0


def access_token(claude_home: Path, now_s: float) -> tuple[str, str | None]:
    """The claude.ai login's access token and its plan. Raises
    ``auth_required`` when there is no such login or its token has expired.
    Blocking: call it in a thread."""
    for name in CREDENTIAL_FILES:
        try:
            data = json.loads((claude_home / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            raise RuntimeFailure("auth_required", f"claude's login file {name} can't be read ({type(exc).__name__})",
                                 retryable=True) from exc
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        if not isinstance(token, str) or not token:
            continue
        expires = oauth.get("expiresAt")
        if isinstance(expires, (int, float)) and expires / 1000.0 - EXPIRY_MARGIN_S <= now_s:
            raise RuntimeFailure("auth_required", "claude's access token has expired; claude renews it at its "
                                                  "next turn, and usage is read again then", retryable=True)
        plan = oauth.get("subscriptionType")
        return token, plan if isinstance(plan, str) else None
    raise RuntimeFailure("auth_required", "claude has no claude.ai login to read usage with (an API key or a "
                                          "token in the environment has no usage to read)")


async def fetch(token: str, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """The usage endpoint's answer. Raises ``auth_required`` (401, 403),
    ``rate_limited`` (429), ``timeout`` or ``protocol``."""
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": OAUTH_BETA, "Accept": "application/json"}
    try:
        if client is not None:
            response = await client.get(USAGE_URL, headers=headers, timeout=READ_TIMEOUT_S)
        else:
            async with httpx.AsyncClient(timeout=READ_TIMEOUT_S) as own:
                response = await own.get(USAGE_URL, headers=headers)
    except httpx.TimeoutException as exc:
        raise RuntimeFailure("timeout", f"Anthropic's usage endpoint didn't answer within {READ_TIMEOUT_S:g} s",
                             retryable=True) from exc
    except httpx.HTTPError as exc:
        raise RuntimeFailure("protocol", f"Anthropic's usage endpoint can't be reached ({type(exc).__name__})",
                             retryable=True) from exc
    if response.status_code in (401, 403):
        raise RuntimeFailure("auth_required", f"Anthropic refused the usage read ({response.status_code}); "
                                              "claude's login may need renewing", retryable=True)
    if response.status_code == 429:
        raise RuntimeFailure("rate_limited", "Anthropic's usage endpoint asked to slow down (429)", retryable=True)
    if response.status_code != 200:
        raise RuntimeFailure("protocol", f"Anthropic's usage endpoint answered {response.status_code}",
                             retryable=True)
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeFailure("protocol", "Anthropic's usage endpoint answered with no JSON") from exc
    if not isinstance(body, dict):
        raise RuntimeFailure("protocol", "Anthropic's usage endpoint answered with no JSON object")
    return body


def snapshot(body: dict[str, Any], *, account: str | None, plan: str | None) -> UsageSnapshot:
    """The endpoint's answer in the contract's terms. A window is any entry
    with a share used and a reset (``five_hour``, ``seven_day``,
    ``seven_day_opus``, …), so a window Anthropic adds is read too."""
    windows = []
    for name, entry in sorted(body.items()):
        if not isinstance(entry, dict) or "resets_at" not in entry or not _number(entry.get("utilization")):
            continue
        utilization = min(max(float(entry["utilization"]) / 100.0, 0.0), 1.0)
        status = "limited" if utilization >= 1.0 or entry.get("locked_reason") else "ok"
        windows.append(UsageWindow(name=name, status=status, utilization=utilization,
                                   resets_at=_utc(entry.get("resets_at"))))
    now = datetime.now(timezone.utc).strftime(TS_FORMAT)
    return UsageSnapshot(windows=tuple(windows), plan=plan, overage=False, updated_at=now, account=account,
                         paid=_paid(body), source="measured")


def _paid(body: dict[str, Any]) -> PaidUsage:
    """Extra usage: on, and not at its spend limit, means the account can go
    on past its plan on paid usage."""
    extra = body.get("extra_usage") if isinstance(body.get("extra_usage"), dict) else {}
    spend = body.get("spend") if isinstance(body.get("spend"), dict) else {}
    enabled = extra.get("is_enabled")
    possible = (enabled and not extra.get("spend_limit_reached")) if isinstance(enabled, bool) else None
    balance = spend.get("balance")
    reason = extra.get("disabled_reason") or spend.get("disabled_reason")
    return PaidUsage(possible=possible, credits=float(balance) if _number(balance) else None,
                     disabled_reason=reason if isinstance(reason, str) else None)


def _utc(stamp: object) -> str | None:
    when = parse_ts(stamp)
    return when.strftime(TS_FORMAT) if when is not None else None


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)

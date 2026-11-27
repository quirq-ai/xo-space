"""Claude's usage with no model call (contract 1.1, ``UsageReader``), in the contract's terms.

* **claude reads it, with its own login.** The runtime asks a prompt-less
  claude for ``get_usage`` (``probe.usage``), the request the Agent SDK sends
  for its usage call; claude fetches its account's windows itself. XO never
  opens claude's credentials file for a token and never calls Anthropic.
  The answer's ``rate_limits`` holds every window the account has (the
  5-hour and the weekly, and a per-model weekly one when it has one), with
  the share used and the reset, and the paid channels (extra usage, credits).
* **No claude.ai login**: with an API key (``ANTHROPIC_API_KEY`` that
  claude runs on) every token is billed, so the read says so (``api_key_usage``:
  paid usage possible and in use, no window); a login claude can't read
  windows for (``rate_limits_available`` false) is ``auth_required``, and its
  usage is what claude's turns report.
* **The answer has no status**, only the share used: a window is
  ``limited`` at 100 % (or locked), and below that its status is left
  unsaid (None), never guessed as ``ok``: whether claude warns there is
  what its turns report.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.cowork_agent.adapters.contract import PaidUsage, RuntimeFailure, UsageSnapshot, UsageWindow
from services.timestamps import TS_FORMAT, parse_ts


def from_cli(body: dict[str, Any], *, account: str | None) -> UsageSnapshot:
    """claude's ``get_usage`` answer as a snapshot. Raises ``auth_required``
    when claude has no windows to read (no claude.ai login)."""
    limits = body.get("rate_limits")
    if body.get("rate_limits_available") is False or not isinstance(limits, dict):
        raise RuntimeFailure("auth_required", "claude has no claude.ai login whose usage it can read (a token "
                                              "in the environment has no usage to read)")
    plan = body.get("subscription_type")
    return snapshot(limits, account=account, plan=plan if isinstance(plan, str) else None)


def snapshot(body: dict[str, Any], *, account: str | None, plan: str | None) -> UsageSnapshot:
    """The endpoint's answer in the contract's terms. A window is any entry
    with a share used and a reset (``five_hour``, ``seven_day``,
    ``seven_day_opus``, …), so a window Anthropic adds is read too."""
    windows = []
    for name, entry in sorted(body.items()):
        if not isinstance(entry, dict) or "resets_at" not in entry or not _number(entry.get("utilization")):
            continue
        utilization = min(max(float(entry["utilization"]) / 100.0, 0.0), 1.0)
        status = "limited" if utilization >= 1.0 or entry.get("locked_reason") else None
        windows.append(UsageWindow(name=name, status=status, utilization=utilization,
                                   resets_at=_utc(entry.get("resets_at"))))
    now = datetime.now(timezone.utc).strftime(TS_FORMAT)
    return UsageSnapshot(windows=tuple(windows), plan=plan, overage=False, updated_at=now, account=account,
                         paid=_paid(body), source="measured")


def _paid(body: dict[str, Any]) -> PaidUsage:
    """Whether the account can go on past its plan on paid usage, from every
    paid channel the answer names: extra usage (on, and not at its spend
    limit) and usage credits (``spend``: enabled, a balance, or auto-reload).
    Possible when any of them is on; not possible only when the answer says
    each is off and holds no credits; else unknown."""
    extra = body.get("extra_usage") if isinstance(body.get("extra_usage"), dict) else {}
    spend = body.get("spend") if isinstance(body.get("spend"), dict) else {}
    enabled = extra.get("is_enabled")
    extra_on = (enabled and not extra.get("spend_limit_reached")) if isinstance(enabled, bool) else None
    spend_on = spend.get("enabled") if isinstance(spend.get("enabled"), bool) else None
    balance = _amount(spend.get("balance"))
    reload = spend.get("auto_reload")
    reload_on = reload.get("enabled") if isinstance(reload, dict) else reload if isinstance(reload, bool) else None
    channels = (extra_on, spend_on, (balance > 0) if balance is not None else None, reload_on)
    if any(on is True for on in channels):
        possible: bool | None = True
    elif extra_on is False and spend_on is False and not balance and reload_on is not True:
        possible = False
    else:
        possible = None
    reason = extra.get("disabled_reason") or spend.get("disabled_reason")
    return PaidUsage(possible=possible, credits=balance,
                     disabled_reason=reason if isinstance(reason, str) else None)


def api_key_usage(account: str | None) -> UsageSnapshot:
    """What a login by API key has to read: no window, and every token
    billed (paid usage, in use, by its nature)."""
    now = datetime.now(timezone.utc).strftime(TS_FORMAT)
    return UsageSnapshot(windows=(), overage=True, updated_at=now, account=account,
                         paid=PaidUsage(possible=True, in_use=True), source="measured")


def _amount(value: object) -> float | None:
    """A balance as a number: plain, or the vendor's ``{amount_minor, exponent}``."""
    if _number(value):
        return float(value)
    if isinstance(value, dict) and _number(value.get("amount_minor")):
        exponent = value.get("exponent") if _number(value.get("exponent")) else 0
        return float(value["amount_minor"]) / 10 ** int(exponent)
    return None


def _utc(stamp: object) -> str | None:
    when = parse_ts(stamp)
    return when.strftime(TS_FORMAT) if when is not None else None


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)

"""What an agy session knows about its account: the login it started with
and agy's own usage windows (storm ch 04 §10).

* ``login_fingerprint``: a digest of who agy is signed in as (its token
  file's sign-in method and account email, and the key variables). agy
  rewrites the file each time it refreshes its access token (hourly), so the
  tokens themselves are left out: a refresh is not a new login. A session
  compares it at each turn boundary; a change means the person connected,
  disconnected or switched accounts, and the engine recycles the session.
* ``account_fingerprint``: a stable, non-secret label of the account, so the
  engine keys its pool by agent × account.
* ``parse_usage``: ``agy --output-format json -p /usage`` (model-free) lists
  each model group's windows with the share still left and their reset; its
  text form, one ``<group> TAB <window> TAB <NN>% TAB <reset>`` row each, is
  read when the JSON holds none. ``UsageGuard`` keeps the latest per account; while a window of
  the session's group is used up and its reset is ahead, ``send`` refuses
  with ``rate_limited``.
* ``agy_error``: agy's ``AGY_ERROR: {…}`` stderr line; a quota error is a
  ``RateLimit(limited)``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from services.cowork_agent.adapters.antigravity.runtime.env import AUTH_VARS, token_file
from services.cowork_agent.adapters.contract import RuntimeFailure, UsageSnapshot, UsageWindow

#: A window at or past this share used is a warning (the engine's pool warns at 80 %).
WARN_AT = 0.8
_ROW = re.compile(r"^(?P<group>[^\t]+)\t(?P<window>[^\t]+)\t(?P<left>\d+(?:\.\d+)?)%\t(?P<reset>\S+)\s*$")
_ERROR = re.compile(r"AGY_ERROR:\s*(\{.*\})")
#: agy's error codes that mean a quota is used up.
QUOTA_CODES = frozenset({"RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED", "RATE_LIMIT_EXCEEDED", "429"})


def _token(environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads(token_file(environ).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"unreadable": True}
    return data if isinstance(data, dict) else {"unreadable": True}


def _identity(environ: Mapping[str, str] | None) -> str:
    data = _token(environ)
    if data is None:
        return "absent"
    if data.get("unreadable"):
        return "unreadable"
    token = data.get("token") if isinstance(data.get("token"), dict) else {}
    return "\0".join((str(data.get("auth_method") or ""), str(data.get("id_token_email") or ""),
                      "refresh" if token.get("refresh_token") else "access" if token.get("access_token") else ""))


def login_fingerprint(environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    keys = "\0".join(hashlib.sha256(environ[k].encode()).hexdigest() if environ.get(k) else "" for k in AUTH_VARS)
    return hashlib.sha256(f"{_identity(environ)}\0{keys}".encode()).hexdigest()


def account_fingerprint(environ: Mapping[str, str] | None = None) -> str | None:
    """A label of the account that is no credential: a digest of the email
    (or of the key in use), None when agy isn't signed in."""
    environ = os.environ if environ is None else environ
    data = _token(environ)
    email = data.get("id_token_email") if isinstance(data, dict) else None
    if isinstance(email, str) and email:
        return "agy:" + hashlib.sha256(email.encode()).hexdigest()[:16]
    for key in AUTH_VARS:
        if environ.get(key):
            return "agy-key:" + hashlib.sha256(environ[key].encode()).hexdigest()[:16]
    return None


def logged_in(environ: Mapping[str, str] | None = None) -> bool:
    """agy is signed in: its token file holds a refresh token (agy refreshes
    the access token itself) or an access token not yet expired, or ADC or a
    gateway key is configured."""
    environ = os.environ if environ is None else environ
    if environ.get("AGY_ADC_AUTH") or environ.get("AGY_LLM_GATEWAY_API_KEY"):
        return True
    data = _token(environ)
    if not isinstance(data, dict) or data.get("unreadable"):
        return False
    token = data.get("token") if isinstance(data.get("token"), dict) else {}
    if token.get("refresh_token"):
        return True
    expiry = _parse(token.get("expiry")) if isinstance(token.get("expiry"), str) else None
    return bool(token.get("access_token")) and (expiry is None or expiry > datetime.now(timezone.utc))


@dataclass(frozen=True)
class Window:
    group: str                # agy's model group: "Gemini Models", "Claude and GPT models"
    name: str                 # "five_hour", "weekly"
    used: float               # 0..1
    resets_at: str | None

    @property
    def status(self) -> str:
        return "limited" if self.used >= 1 else "warning" if self.used >= WARN_AT else "ok"


def _slug(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in ("limit", "remaining")]
    return "_".join(words) or "window"


#: ``agy --output-format json -p /usage``: model-free, like the text form, but structured.
USAGE_ARGV = ("--output-format", "json", "-p", "/usage")


def command_data(text: str) -> Mapping[str, Any] | None:
    """The ``command.data`` of agy's JSON answer to a model-free command
    (``models``, ``-p /usage``), or None if the output holds none."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        answer = json.loads(text[start:])
    except ValueError:
        return None
    command = answer.get("command") if isinstance(answer, dict) else None
    data = command.get("data") if isinstance(command, dict) else None
    return data if isinstance(data, Mapping) else None


def parse_usage(text: str) -> list[Window]:
    """agy's usage report: the JSON form's buckets (each group's remaining
    fraction and reset time), else its text rows (``group TAB window TAB NN%
    TAB reset``; other lines are left out)."""
    data = command_data(text)
    if data is not None and isinstance(data.get("groups"), list):
        return _usage_buckets(data["groups"])
    response = json_response(text)
    if isinstance(response, str):
        text = response
    out = []
    for line in text.splitlines():
        found = _ROW.match(line.strip("\r"))
        if not found:
            continue
        left = min(max(float(found["left"]) / 100, 0.0), 1.0)
        reset = found["reset"] if _parse(found["reset"]) else None
        out.append(Window(group=found["group"].strip(), name=_slug(found["window"]), used=round(1 - left, 4),
                          resets_at=reset))
    return out


def json_response(text: str) -> str | None:
    start = text.find("{")
    try:
        answer = json.loads(text[start:]) if start >= 0 else None
    except ValueError:
        return None
    return answer.get("response") if isinstance(answer, dict) else None


def _usage_buckets(groups: list[Any]) -> list[Window]:
    out = []
    for group in groups:
        if not isinstance(group, Mapping) or not isinstance(group.get("name"), str):
            continue
        for bucket in group.get("buckets") or ():
            left = bucket.get("remaining_fraction") if isinstance(bucket, Mapping) else None
            if not isinstance(left, (int, float)) or not isinstance(bucket.get("name"), str):
                continue
            reset = bucket.get("reset_time") if isinstance(bucket.get("reset_time"), str) else None
            out.append(Window(group=group["name"].strip(), name=_slug(bucket["name"]),
                              used=round(1 - min(max(float(left), 0.0), 1.0), 4),
                              resets_at=reset if reset and _parse(reset) else None))
    return out


def group_of(model: str | None, groups: set[str]) -> str | None:
    """The model group a model's quota is counted in: agy names one for
    Gemini and one for the others."""
    if not groups:
        return None
    gemini = next((g for g in groups if "gemini" in g.lower()), None)
    other = next((g for g in groups if g != gemini), None)
    if model is None or "gemini" in model.lower():
        return gemini or other
    return other or gemini


class UsageGuard:
    """The latest usage per window, for one account."""

    def __init__(self) -> None:
        self._windows: dict[tuple[str, str], Window] = {}
        self.updated_at: str | None = None

    def update(self, windows: list[Window]) -> None:
        if windows:
            self._windows = {(w.group, w.name): w for w in windows}
            self.updated_at = _now()

    def limited(self, model: str | None) -> Window | None:
        group = group_of(model, {g for g, _ in self._windows})
        now = datetime.now(timezone.utc)
        for (g, _), w in self._windows.items():
            if g == group and w.status == "limited":
                reset = _parse(w.resets_at)
                if reset is None or reset > now:
                    return w
        return None

    def worst(self, model: str | None) -> Window | None:
        group = group_of(model, {g for g, _ in self._windows})
        found = [w for (g, _), w in self._windows.items() if g == group]
        return max(found, key=lambda w: w.used, default=None)

    def check(self, model: str | None) -> None:
        window = self.limited(model)
        if window is not None:
            raise RuntimeFailure("rate_limited", f"agy's {window.name.replace('_', ' ')} limit for "
                                 f"{window.group} is used up until {window.resets_at or 'it resets'}",
                                 retryable=True, retry_at=_parse(window.resets_at))

    def snapshot(self) -> UsageSnapshot:
        windows = tuple(UsageWindow(name=f"{_slug(w.group)}.{w.name}", status=w.status, utilization=w.used,
                                    resets_at=w.resets_at) for w in self._windows.values())
        return UsageSnapshot(windows=windows, updated_at=self.updated_at)


def agy_error(line: str) -> dict[str, Any] | None:
    """agy's ``AGY_ERROR: {…}`` line, parsed; None for any other line."""
    found = _ERROR.search(line)
    if not found:
        return None
    try:
        data = json.loads(found.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def is_quota(error: Mapping[str, Any]) -> bool:
    code = str(error.get("code") or error.get("status") or "").upper()
    message = str(error.get("message") or "").lower()
    return code in QUOTA_CODES or "quota" in message or "rate limit" in message


def _parse(stamp: object) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def token_path(environ: Mapping[str, str] | None = None) -> Path:
    return token_file(environ)

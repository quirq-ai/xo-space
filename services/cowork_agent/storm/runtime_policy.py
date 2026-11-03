"""Which runtime a new thread runs on (mixed-runtime plan, step 5).

**The policy** is the owner's: Space-wide (``space_settings['runtime_policy']``)
with a per-project override (``runtime_policy:<chat id>``); a field the
project leaves unset is the Space's.

* ``allowed``: the runtimes work may run on (null: every runtime);
* ``default``: the runtime a thread runs on when nobody chose (null: the
  server's own agent);
* ``preferences``: the owner's words on which runtime suits which work, for
  the coordinator to read;
* ``fallback``: whether a new thread may run on another allowed runtime when
  the chosen one can't take work now. Off by default: moving work to another
  runtime sends it to another provider, which the owner decides.

**The choice** (``choose``) is pure: the policy, each runtime's health and
what was asked in, a runtime and the one sentence saying why out. It never
names an agent; agents are data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from services.cowork_agent.storm.health import Health
from services.errors import ServiceError

SPACE_KEY = "runtime_policy"
PREFERENCES_MAX = 2000
FIELDS = ("allowed", "default", "preferences", "fallback")


def project_key(chat_id: str) -> str:
    return f"{SPACE_KEY}:{chat_id}"


class PolicyRefused(ServiceError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(code, message, status=status)


class NoRuntime(ServiceError):
    """No runtime can take this work now; the message says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status=409)


@dataclass(frozen=True)
class Policy:
    allowed: tuple[str, ...] | None = None
    default: str | None = None
    preferences: str | None = None
    fallback: bool = False

    def permits(self, agent: str) -> bool:
        return self.allowed is None or agent in self.allowed

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["allowed"] = list(self.allowed) if self.allowed is not None else None
        return data


def effective(space: Mapping[str, Any], project: Mapping[str, Any] | None) -> Policy:
    """The Space's policy with the project's set fields over it."""
    merged = {**{k: space.get(k) for k in FIELDS}, **{k: v for k, v in (project or {}).items()
                                                      if k in FIELDS and v is not None}}
    allowed = merged.get("allowed")
    return Policy(allowed=tuple(allowed) if allowed is not None else None, default=merged.get("default"),
                  preferences=merged.get("preferences"), fallback=bool(merged.get("fallback")))


def validate(fields: Mapping[str, Any], providers: Sequence[str]) -> dict[str, Any]:
    """The fields as stored, or ``PolicyRefused``: every runtime named must
    be one this server has, and the default must be allowed."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in FIELDS:
            raise PolicyRefused("invalid_argument", f"{key} isn't a policy field ({', '.join(FIELDS)}).")
        out[key] = value
    unknown = [a for a in (*(out.get("allowed") or ()), *([out["default"]] if out.get("default") else ()))
               if a not in providers]
    if unknown:
        raise PolicyRefused("unknown_runtime", f"No storm runtime is named {', '.join(map(repr, unknown))}; "
                                               f"this server has {', '.join(providers) or 'none'}.")
    if out.get("allowed") is not None and not out["allowed"]:
        raise PolicyRefused("invalid_argument", "allowed names at least one runtime, or is null for every one.")
    if out.get("default") and out.get("allowed") is not None and out["default"] not in out["allowed"]:
        raise PolicyRefused("invalid_argument", f"The default {out['default']!r} must be one of allowed.")
    if out.get("preferences") is not None and len(out["preferences"]) > PREFERENCES_MAX:
        raise PolicyRefused("invalid_argument", f"preferences is at most {PREFERENCES_MAX} characters.")
    return out


@dataclass(frozen=True)
class Choice:
    agent: str
    note: str | None = None              # said on the thread when the choice isn't the one asked for, or waits
    fell_back_from: str | None = None


def choose(policy: Policy, health: Sequence[Health], *, wanted: str | None, server_default: str) -> Choice:
    """The runtime a new thread runs on.

    The first of ``wanted`` (the coordinator's or a job's choice), the
    policy's default and the server's own agent is the one asked for. It is
    taken when it can take work now. Otherwise, with ``fallback`` on, the
    first other allowed runtime that can (one whose limit is open before one
    that warns). With none, a runtime that is only at its usage limit is
    still taken (the thread waits for the reset, saying so); one that is not
    logged in, can't be reached or isn't allowed is refused (``NoRuntime``).
    Without an explicit ``wanted``, work that can go to an open runtime isn't
    started on one whose limit warns, when fallback is on."""
    by = {h.agent: h for h in health}
    if wanted is not None and not policy.permits(wanted):
        raise NoRuntime("runtime_not_allowed", f"{wanted} isn't allowed in this project; it allows "
                                               f"{', '.join(policy.allowed or ())}.")
    asked = wanted or next((a for a in (policy.default, server_default) if a and policy.permits(a)), None) \
        or next((h.agent for h in health if policy.permits(h.agent) and h.installed), None)
    if asked is None:
        raise NoRuntime("no_runtime", "No allowed runtime is installed here.")
    primary = by.get(asked) or Health(asked, installed=False)
    others = [h for h in health if h.agent != asked and policy.permits(h.agent) and h.usable]
    others.sort(key=lambda h: h.limit == "warn")
    if primary.usable and not (wanted is None and policy.fallback and primary.limit == "warn"
                               and others and others[0].limit != "warn"):
        return Choice(asked)
    if policy.fallback and others:
        other = others[0].agent
        why = primary.why_not or "its usage limit is close"
        return Choice(other, f"{asked} can't take this work now ({why}), so it runs on {other}.", asked)
    if primary.usable:
        return Choice(asked)
    if primary.installed and primary.logged_in and primary.error is None:
        return Choice(asked, f"{asked} is at its usage limit{f' until {primary.resets_at}' if primary.resets_at else ''}; "
                             "this thread waits for it.")
    code = ("auth_required" if primary.error is None else primary.error["code"]) if primary.installed else "no_runtime"
    raise NoRuntime(code, f"{asked} can't run this thread: {primary.why_not}."
                          + ("" if policy.fallback else " Fallback to another runtime is off in this project."))


# ── stored ───────────────────────────────────────────────────────────────────

async def read(engine: Any, chat_id: str | None = None) -> dict[str, Any]:
    """``{"space", "project"?, "effective"}``: the stored policies (fields
    as set; a project's unset ones are null) and the one that applies."""
    from services.cowork_agent.storm.chats import router as chat_router, rows

    def get(conn) -> dict[str, Any]:
        if chat_id is not None:
            chat = rows.get_chat(conn, chat_id)
            if chat is None or chat["kind"] != "project":
                raise PolicyRefused("no_such_chat", f"There is no project chat {chat_id!r}.", status=404)
        space = chat_router.read_setting(conn, SPACE_KEY, {})
        project = chat_router.read_setting(conn, project_key(chat_id), {}) if chat_id else None
        out: dict[str, Any] = {"space": {k: space.get(k) for k in FIELDS}}
        if chat_id is not None:
            out["project"] = {k: (project or {}).get(k) for k in FIELDS}
        out["effective"] = effective(space, project).as_dict()
        return out
    return await engine.store.read(get)


async def write(engine: Any, fields: Mapping[str, Any], providers: Sequence[str],
                chat_id: str | None = None) -> dict[str, Any]:
    """Replace the Space's policy, or a project's override (its null fields
    follow the Space), with ``fields``; then ``read``."""
    from services.cowork_agent.storm.chats import router as chat_router, rows

    stored = validate(fields, providers)

    def put(w) -> None:
        if chat_id is not None:
            chat = rows.get_chat(w, chat_id)
            if chat is None or chat["kind"] != "project":
                raise PolicyRefused("no_such_chat", f"There is no project chat {chat_id!r}.", status=404)
        space = chat_router.read_setting(w, SPACE_KEY, {})
        merged = effective(space, stored) if chat_id else effective(stored, None)
        if merged.default and not merged.permits(merged.default):
            raise PolicyRefused("invalid_argument", f"The default {merged.default!r} must be one of allowed.")
        chat_router.write_setting(w, project_key(chat_id) if chat_id else SPACE_KEY, stored)
    await engine.write(put)
    return await read(engine, chat_id)

"""The founder's intents: the rows, how they are written, and which one
holds (record design v2 §2, §5 rules 1 to 3; w2-w3-design §1 and §2).

An intent is the founder's saved choice for one scope. ``run`` holds the
work there: ``stopped`` drops what is queued, ``paused`` keeps it. ``paid``
says whether a pool may go on past its plan: ``never``, or ``ask``. Only
the founder sets one, through the founder's routes; no agent tool and no
engine path calls ``set_intent`` or ``clear_intent``. Each change writes
the row, one ``intent`` event and one ledger entry in the caller's
transaction, so the three commit together or not at all.

The resolution is pure, over the rows ``load`` reads (a snapshot the engine
can keep, so admission never waits on the store): ``scope_chain`` is the
scopes a session sits in, ``run_state`` the most restrictive ``run``
holding over them, and ``paid_mode`` a pool's ``paid``. A row whose
``until`` has passed no longer holds; nothing has to remove it for that.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Sequence

from services.cowork_agent.storm.records import ledger
from services.errors import ServiceError
from services.timestamps import iso, parse_ts

if TYPE_CHECKING:
    import sqlite3

    from services.cowork_agent.storm.engine.ports import Writes
    from services.cowork_agent.storm.engine.session import Meta

SCOPES = ("space", "chat", "coordinator", "thread", "session", "pool")
#: Each key's values, the most restrictive first.
VALUES: Mapping[str, tuple[str, ...]] = {"run": ("stopped", "paused"), "paid": ("never", "ask")}
#: The scopes each key is set for.
KEY_SCOPES: Mapping[str, tuple[str, ...]] = {"run": ("space", "chat", "coordinator", "thread", "session"),
                                              "paid": ("space", "pool")}
#: What each scope's id is (the space's is always '').
SCOPE_IDS: Mapping[str, str] = {"chat": "a chat id", "coordinator": "its chat's id",
                                "thread": "<chat_id>/<thread_ts>", "session": "a session id", "pool": "a pool id"}
#: A pool's paid mode when neither the pool nor the space has one: no paid
#: usage. It is the code's default, so an upgraded store needs no row seeded.
PAID_DEFAULT = "never"
SPACE = ("space", "")

#: A scope as (scope, scope_id); an intent's key as (scope, scope_id, key).
Scope = tuple[str, str]
Key = tuple[str, str, str]


class IntentInvalid(ServiceError):
    """An intent the store wouldn't hold; the message says what it would."""

    def __init__(self, message: str) -> None:
        super().__init__("intent_invalid", message)


@dataclass(frozen=True)
class Intent:
    """One ``intents`` row; its fields are the table's columns, in order."""

    scope: str
    scope_id: str
    key: str
    value: str
    until: str | None
    cites: str | None
    set_at: str

    def live(self, now: float) -> bool:
        """Whether it still holds at ``now`` (wall seconds). An ``until`` that
        doesn't read as a time (only an edit outside this module writes one)
        never lapses: a hold that can't tell its end keeps holding."""
        if self.until is None:
            return True
        end = parse_ts(self.until)
        return end is None or end.timestamp() > now


def load(conn: sqlite3.Connection) -> dict[Key, Intent]:
    """Every intent row, lapsed ones too: whether one holds depends on when
    it is asked."""
    rows = conn.execute(f"SELECT {', '.join(ledger.INTENT_COLUMNS)} FROM intents")
    return {(intent.scope, intent.scope_id, intent.key): intent for intent in (Intent(*row) for row in rows)}


def set_intent(w: Writes, *, scope: str, scope_id: str, key: str, value: str, until: str | None = None,
               cites: str | None = None) -> Intent:
    """Set the founder's ``key`` for one scope, replacing what it held, with
    its event and ledger entry. ``until`` (an ISO time, kept as the store
    writes times) is when it lapses; ``cites`` is the founder's message or
    decision it came from. Raises ``IntentInvalid`` before writing anything."""
    _check(scope, scope_id, key)
    if value not in VALUES[key]:
        raise IntentInvalid(f"A {key} intent's value is {_either(VALUES[key])}; {value!r} isn't.")
    if until is not None:
        if (end := parse_ts(until)) is None:
            raise IntentInvalid(f"until is an ISO time, such as 2026-11-12T18:00:00Z; {until!r} isn't.")
        until = iso(end)
    intent = Intent(scope, scope_id, key, value, until, cites, w.now)
    w.execute("INSERT INTO intents (scope, scope_id, key, value, until, cites, set_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
              "ON CONFLICT (scope, scope_id, key) DO UPDATE SET value = excluded.value, until = excluded.until, "
              "cites = excluded.cites, set_at = excluded.set_at", dataclasses.astuple(intent))
    _record(w, "set", scope, scope_id, key, intent)
    return intent


def clear_intent(w: Writes, *, scope: str, scope_id: str, key: str) -> bool:
    """Lift the founder's ``key`` for one scope, with its event and ledger
    entry. Returns whether there was one: clearing nothing writes nothing."""
    _check(scope, scope_id, key)
    if not w.execute("DELETE FROM intents WHERE scope = ? AND scope_id = ? AND key = ?",
                     (scope, scope_id, key)).rowcount:
        return False
    _record(w, "clear", scope, scope_id, key, None)
    return True


def scope_chain(meta: Meta) -> tuple[Scope, ...]:
    """The scopes a session sits in, broadest first: the space, its chat,
    then the chat's coordinator (both coordinator roles end so, see
    ``engine/model.py`` ``ROLES``) or its thread, then the session itself."""
    chain = [SPACE]
    if meta.chat_id:
        chain.append(("chat", meta.chat_id))
        if meta.role.endswith("coordinator"):
            chain.append(("coordinator", meta.chat_id))
        elif meta.thread_ts:
            chain.append(("thread", f"{meta.chat_id}/{meta.thread_ts}"))
    chain.append(("session", meta.id))
    return tuple(chain)


def run_state(chain: Sequence[Scope], intents: Mapping[Key, Intent], now: float) -> Intent | None:
    """The ``run`` intent holding over ``chain`` at ``now`` (wall seconds),
    or None. The most restrictive decides (``stopped`` beats ``paused``),
    and of two alike the broadest, which still holds once the narrower is
    lifted. It is returned whole, so a refusal can say who held what, and
    since when."""
    rank = VALUES["run"].index
    decided: Intent | None = None
    for scope, scope_id in chain:
        intent = intents.get((scope, scope_id, "run"))
        if intent is not None and intent.live(now) and (decided is None or rank(intent.value) < rank(decided.value)):
            decided = intent
    return decided


def paid_mode(pool_id: str, intents: Mapping[Key, Intent], now: float) -> Intent | None:
    """The ``paid`` intent holding for a pool at ``now``: the pool's own,
    else the space's. None means ``PAID_DEFAULT``."""
    for scope, scope_id in (("pool", pool_id), SPACE):
        intent = intents.get((scope, scope_id, "paid"))
        if intent is not None and intent.live(now):
            return intent
    return None


def _check(scope: str, scope_id: str, key: str) -> None:
    """Refuse what the table's CHECK constraints would, saying what they allow."""
    if scope not in SCOPES:
        raise IntentInvalid(f"An intent's scope is {_either(SCOPES)}; {scope!r} isn't.")
    if key not in VALUES:
        raise IntentInvalid(f"An intent's key is {_either(tuple(VALUES))}; {key!r} isn't.")
    if scope not in KEY_SCOPES[key]:
        raise IntentInvalid(f"A {key} intent is set for {_either(KEY_SCOPES[key])}, not for {scope}.")
    if scope == "space":
        if scope_id != "":
            raise IntentInvalid(f"The space is one scope, whose id is ''; {scope_id!r} isn't.")
    elif not isinstance(scope_id, str) or not scope_id:
        raise IntentInvalid(f"A {scope} scope needs its id: {SCOPE_IDS[scope]}.")
    elif scope == "thread" and not all(scope_id.partition("/")):
        raise IntentInvalid(f"A thread's scope id is {SCOPE_IDS['thread']}; {scope_id!r} isn't.")


def _record(w: Writes, action: str, scope: str, scope_id: str, key: str, intent: Intent | None) -> None:
    """A change's ``intent`` event and ledger entry (``intent`` None: cleared)."""
    data = {"action": action, "scope": scope, "scope_id": scope_id, "key": key, "value": None, "until": None,
            "cites": None}
    if intent is not None:
        data.update(value=intent.value, until=intent.until, cites=intent.cites)
    w.append("intent", data, **_ids(scope, scope_id))
    ledger.append(w, actor=ledger.FOUNDER, action=action, record=ledger.intent_record(scope, scope_id, key),
                  row=dataclasses.asdict(intent) if intent is not None else None)


def _ids(scope: str, scope_id: str) -> dict[str, str]:
    """The event's id columns, as every event names the chat, thread or
    session it concerns."""
    if scope in ("chat", "coordinator"):
        return {"chat_id": scope_id}
    if scope == "thread":
        chat_id, _, thread_ts = scope_id.partition("/")
        return {"chat_id": chat_id, "thread_ts": thread_ts}
    if scope == "session":
        return {"session_id": scope_id}
    return {}


def _either(words: Sequence[str]) -> str:
    """``a, b or c``."""
    return f"{', '.join(words[:-1])} or {words[-1]}"

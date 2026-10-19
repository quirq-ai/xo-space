"""The router: who decides where the person's message on a timeline goes
(design ch 11 §3, §14.1–§14.4, §14.7; §0.47–§0.52).

* **One Space-wide setting** (``space_settings['router']``), which a chat may
  override (its ``intelligence`` row's ``routing``). On by default: with no
  decision model chosen, the chat's coordinator routes (``agent``). ``off``
  refuses new timeline messages (409 ``router_off``). No env switches.
* **Decision models** are connected like provider keys: a provider, a model
  and an API key, kept in ``router.env`` (``services/decision_models/keys``),
  never shown again. A decision model only routes (forward, start a thread,
  follow up a thread), on the coordinator's behalf through the same comms
  tools; when it abstains, is unsure or fails, the message goes to the
  coordinator with a note saying why (D12).
* **Every decision** is a ``router_decisions`` row, the PoC's shape, whoever
  made it.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any, Mapping

import services.decision_models as dm
from services.cowork_agent.storm.chats import rows
from services.cowork_agent.storm.engine import Engine, Writes
from services.decision_models import keys
from services.errors import ServiceError

OFF, AGENT, DECISION = "off", "agent", "decision"
MODES = (OFF, AGENT, DECISION)
ROUTER_KEY = "router"
MODELS_KEY = "router.models"
#: Under this probability a decision model's choice goes to the agent.
DEFAULT_ABSTAIN_BELOW = 0.5
DEFAULT = {"mode": AGENT, "decision_model": None, "abstain_below": DEFAULT_ABSTAIN_BELOW}
API_KEY_MAX = 512
CONTENT_MAX = 4000
TITLE_FROM_TEXT = 60
THREADS_ASKED = 20
SCHEMA = 1

#: A coordinator's comms tool, as the routing action it records (PoC).
ACTION_OF = {"start_thread_session": "start_thread", "message_thread": "message_thread",
             "ask_workspace": "ask_workspace", "post_message": "answer", "react": "react",
             "no_reply_needed": "no_reply", "forward_to_project": "forward", "no_such_project": "no_such_project",
             "propose_project": "propose_project"}

INTENT_ID, THREAD_ID, PROJECTS_ID = "intent", "thread", "projects"
INTENTS = {
    "new_task": "asks for new work that changes the project: code, files, a fix, a feature, a PR",
    "follow_up": "adds to or changes work already under way in one of this project's threads",
    "question": "asks a question about the project that can be answered without changing it",
    "chat": "a greeting, thanks, or a remark that asks for nothing",
}
NO_THREAD = "none"


class RouterRefused(ServiceError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(code, message, status=status)


# ── settings ─────────────────────────────────────────────────────────────────

def read_setting(conn, key: str, default: Any) -> Any:
    row = conn.execute("SELECT value FROM space_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def write_setting(w: Writes, key: str, value: Any) -> None:
    w.execute("INSERT INTO space_settings (key, value, updated_at) VALUES (?, json(?), ?) ON CONFLICT (key) "
              "DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at", (key, json.dumps(value), w.now))
    w.append("settings", {"key": key, "value": value})


def space_router(conn) -> dict[str, Any]:
    return {**DEFAULT, **read_setting(conn, ROUTER_KEY, {})}


def chat_override(conn, chat_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT routing FROM intelligence WHERE scope = 'chat' AND scope_id = ?", (chat_id,)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def effective(conn, chat_id: str) -> dict[str, Any]:
    """What routes this chat: its own override, else the Space's (§0.52)."""
    return {**space_router(conn), **(chat_override(conn, chat_id) or {})}


def check(conn, mode: str, decision_model: str | None) -> None:
    """A routing choice the Space can make: a known mode; a connected
    decision model for ``decision``."""
    if mode not in MODES:
        raise RouterRefused("bad_mode", f"mode is one of {', '.join(MODES)}.")
    if mode == DECISION:
        if decision_model not in read_setting(conn, MODELS_KEY, {}):
            raise RouterRefused("model_not_connected", "Connect that decision model first "
                                                       "(POST /api/chat/router/models), then choose it.")
    elif decision_model is not None:
        raise RouterRefused("bad_mode", "decision_model goes with mode decision.")


async def get(engine: Engine) -> dict[str, Any]:
    return await engine.store.read(space_router)


async def put(engine: Engine, *, mode: str, decision_model: str | None,
              abstain_below: float = DEFAULT_ABSTAIN_BELOW) -> dict[str, Any]:
    if not 0.0 <= abstain_below <= 1.0:
        raise RouterRefused("bad_threshold", "abstain_below is a probability, from 0 to 1.")

    def save(w: Writes) -> dict[str, Any]:
        check(w, mode, decision_model)
        value = {"mode": mode, "decision_model": decision_model, "abstain_below": abstain_below}
        write_setting(w, ROUTER_KEY, value)
        return value
    return await engine.write(save)


# ── decision models ──────────────────────────────────────────────────────────

def _provider(provider: str):
    try:
        return dm.provider_module(provider)
    except ValueError:
        raise RouterRefused("unknown_provider", f"There is no decision model {provider!r}; choose one of "
                                                f"{', '.join(dm.PROVIDERS)}.", status=404) from None


async def models(engine: Engine) -> list[dict[str, Any]]:
    """Each provider, its model and whether it is connected; never a key."""
    chosen = await engine.store.read(lambda conn: read_setting(conn, MODELS_KEY, {}))
    out = []
    for provider in dm.PROVIDERS:
        module = _provider(provider)
        connected = provider in chosen and await asyncio.to_thread(keys.read, module.ENV_KEY) is not None
        out.append({"id": provider, "provider": provider, "label": module.LABEL, "docs_url": module.DOCS_URL,
                    "model": chosen.get(provider, {}).get("model") or module.DEFAULT_MODEL, "connected": connected})
    return out


async def connect(engine: Engine, *, provider: str, model: str | None, api_key: str) -> list[dict[str, Any]]:
    module = _provider(provider)
    value = api_key.strip()
    if not value or len(value) > API_KEY_MAX or re.search(r"\s", value):
        raise RouterRefused("invalid_key", "That doesn't look like an API key (one line, no spaces).")
    await asyncio.to_thread(keys.write, module.ENV_KEY, value)

    def save(w: Writes) -> None:
        chosen = read_setting(w, MODELS_KEY, {})
        write_setting(w, MODELS_KEY, {**chosen, provider: {"model": model or module.DEFAULT_MODEL}})
    await engine.write(save)
    return await models(engine)


async def disconnect(engine: Engine, provider: str) -> list[dict[str, Any]]:
    """Forget the key; a Space or chat routing with it falls back to the agent."""
    module = _provider(provider)

    def save(w: Writes) -> None:
        chosen = read_setting(w, MODELS_KEY, {})
        write_setting(w, MODELS_KEY, {k: v for k, v in chosen.items() if k != provider})
        router = space_router(w)
        if router["decision_model"] == provider:
            write_setting(w, ROUTER_KEY, {**router, "mode": AGENT, "decision_model": None})
    await engine.write(save)
    await asyncio.to_thread(keys.write, module.ENV_KEY, None)
    return await models(engine)


# ── decisions ────────────────────────────────────────────────────────────────

def record(w: Writes, *, chat_id: str, message_id: str, decided_by: str, route: Mapping[str, Any]) -> str:
    decision_id = f"rdec_{uuid.uuid4().hex[:16]}"
    w.execute("INSERT INTO router_decisions (id, chat_id, message_id, decided_by, route, created_at) "
              "VALUES (?, ?, ?, ?, json(?), ?)", (decision_id, chat_id, message_id, decided_by,
                                                   json.dumps({"schema": SCHEMA, **route}), w.now))
    return decision_id


def title_from(text: str) -> str:
    """A thread's title when a decision model starts it: its first sentence."""
    first = re.split(r"[.\n!?]", text.strip(), maxsplit=1)[0]
    return (first[:TITLE_FROM_TEXT].rstrip() + ("…" if len(first) > TITLE_FROM_TEXT else "")) or "Thread"


def project_questions(threads: list[Mapping[str, Any]]) -> list:
    intent = "The content is a message the owner of a software project sent to the project's chat. What does it ask for?"
    if threads:
        # Without the threads in view, "also make it do X" reads as new work.
        listed = "; ".join(f"\"{t['title']}\" ({t['state']})" for t in threads)
        intent += (f" The project's threads so far: {listed}. A message that adds to, corrects or extends the "
                   "work of one of them is a follow-up, even when that thread is resolved.")
    questions = [dm.Choice(INTENT_ID, intent, dict(INTENTS))]
    if threads:
        options = {t["ts"]: f"the thread working on: {t['title']} ({t['state']})" for t in threads}
        options[NO_THREAD] = "none of these threads is about it"
        questions.append(dm.Choice(THREAD_ID, "If the message is about work already under way, which thread is "
                                              "doing that work?", options))
    return questions


def workspace_questions(projects: Mapping[str, str]) -> list:
    return [dm.Tags(PROJECTS_ID, "The content is a message to a workspace that holds these projects. Tag every "
                                 "project the message asks to change or asks about. Do not tag a project it only "
                                 "mentions in passing.", dict(projects))] if projects else []


def decide(kind: str, result: dm.Result, threshold: float) -> dict[str, Any]:
    """The model's answers as a route: ``actions`` (empty: the agent takes
    the message), the confidence and the reasons."""
    route: dict[str, Any] = {"provider": result.provider, "actions": [], "reasons": [],
                             "usage": {"input_tokens": result.input_tokens}, "latency_ms": result.latency_ms}
    if kind == rows.WORKSPACE:
        answer = result.answers.get(PROJECTS_ID)
        chosen = sorted(k for k, v in (answer.applies if answer else {}).items() if v is True)
        if answer is None or answer.error or not chosen:
            route["reasons"].append(answer.error if answer and answer.error else "no project clearly applies")
            return route
        route["actions"] = [{"kind": "forward", "project": p} for p in chosen]
        route["confidence"] = min(answer.probabilities.get(p, 0.0) for p in chosen)
        return route
    intent = result.answers.get(INTENT_ID)
    if intent is None or intent.abstains(threshold):
        route["reasons"].append(intent.error if intent and intent.error else "unsure what the message asks for")
        return route
    route["confidence"] = intent.probability
    if intent.chosen == "new_task":
        route["actions"] = [{"kind": "start_thread"}]
    elif intent.chosen == "follow_up":
        thread = result.answers.get(THREAD_ID)
        if thread is None or thread.abstains(threshold) or thread.chosen == NO_THREAD:
            route["reasons"].append("a follow-up, but unsure to which thread")
        else:
            route["actions"] = [{"kind": "message_thread", "thread_id": thread.chosen}]
            route["confidence"] = min(intent.probability or 0.0, thread.probability or 0.0)
    else:
        route["reasons"].append(f"{intent.chosen}: it needs words, so the agent answers")
    return route


async def classify(*, provider: str, model: str, kind: str, text: str, threads: list[Mapping[str, Any]],
                   projects: Mapping[str, str], threshold: float) -> dict[str, Any]:
    questions = workspace_questions(projects) if kind == rows.WORKSPACE else project_questions(threads[:THREADS_ASKED])
    started = time.perf_counter()
    result = await dm.ask(provider, text[:CONTENT_MAX], questions, model=model) if questions \
        else dm.Result(provider, {})
    route = decide(kind, result, threshold)
    route["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return route

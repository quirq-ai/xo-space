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
from services.cowork_agent.adapters.contract import ModelInfo
from services.cowork_agent.storm.chats import rows
from services.cowork_agent.storm.engine import Engine, Writes
from services.cowork_agent.storm.engine.catalog import Catalog
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
#: The setup questions: a new thread's model and effort (ch 11 §3.3, §0.105).
MODEL_ID, EFFORT_ID = "model", "effort"
#: A choice question needs two options at least (Sage takes 2 to 120); with
#: one, it is the answer.
MIN_OPTIONS = 2
#: A model option's text, its name and what the runtime says it is for, at
#: most this long: enough for a sentence, so a classifier tells them apart.
OPTION_TEXT_MAX = 120
#: Why a thread a decision model starts has no model and effort of its choosing.
UNSURE = "The decision model that routed this message wasn't sure which model and effort fit the work"
NOT_ASKED = ("The thread agent's models couldn't be read when the decision model routed this message, so it wasn't "
             "asked to choose them")
INTENTS = {
    "new_task": "asks for new work that changes the project: code, files, a fix, a feature, a PR",
    "follow_up": "adds to or changes work already under way in one of this project's threads",
    "question": "asks a question about the project that can be answered without changing it",
    "chat": "a greeting, thanks, or a remark that asks for nothing",
}
NO_THREAD = "none"
#: What each effort fits, for a decision model choosing one.
EFFORT_FITS = {
    "low": "a small, quick edit or lookup",
    "medium": "an ordinary change",
    "high": "hard work: a feature, a tricky bug, a change across several files",
    "xhigh": "very hard work: a large feature or a deep investigation",
    "max": "the hardest work, where depth matters more than time",
}


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

#: The route of a decision a model is still making (``settle`` replaces it).
PENDING: Mapping[str, Any] = {"pending": True}


def record(w: Writes, *, chat_id: str, message_id: str, decided_by: str, route: Mapping[str, Any]) -> str:
    decision_id = f"rdec_{uuid.uuid4().hex[:16]}"
    w.execute("INSERT INTO router_decisions (id, chat_id, message_id, decided_by, route, created_at) "
              "VALUES (?, ?, ?, ?, json(?), ?)", (decision_id, chat_id, message_id, decided_by,
                                                   json.dumps({"schema": SCHEMA, **route}), w.now))
    return decision_id


def settle(w: Writes, decision_id: str, route: Mapping[str, Any]) -> None:
    """A pending decision's route, once made."""
    w.execute("UPDATE router_decisions SET route = json(?) WHERE id = ?",
              (json.dumps({"schema": SCHEMA, **route}), decision_id))


def title_from(text: str) -> str:
    """A thread's title when a decision model starts it: its first sentence."""
    # A sentence ends at . ! ? before a space or the end, or at a line break: never inside a word
    # ("todo.txt", "v1.2"; live run 4 cut "Create a file todo.txt…" to "Create a file todo").
    first = re.split(r"[.!?](?=\s|$)|\n", text.strip(), maxsplit=1)[0]
    return (first[:TITLE_FROM_TEXT].rstrip() + ("…" if len(first) > TITLE_FROM_TEXT else "")) or "Thread"


def model_options(catalog: Catalog) -> dict[str, str]:
    """The model question's options, by the id of the model each stands for
    (§0.116): one per family the runtime marks, its ``family_default`` model
    standing for it (the first listed, should a runtime mark none), and one
    per name for a model in no family (a model's alias and its dated id share
    a name; the first listed stands for it). Not the default entry, which is
    no choice of model (the CLI's own, what a thread gets when the decision
    model is sure only of the effort). Each option's text is
    ``option_text``."""
    stands: dict[tuple[str, str], ModelInfo] = {}
    for m in catalog.models:
        if m.is_default:
            continue
        key = ("family", m.family) if m.family else ("name", m.name or m.id)
        if key not in stands or (m.family_default and not stands[key].family_default):
            stands[key] = m
    return {m.id: option_text(m) for m in stands.values()}


def option_text(model: ModelInfo) -> str:
    """``<name>: <description>`` (the name alone when the runtime gives no
    description), cut to ``OPTION_TEXT_MAX``."""
    text = f"{model.name or model.id}: {model.description}" if model.description else model.name or model.id
    return text if len(text) <= OPTION_TEXT_MAX else text[:OPTION_TEXT_MAX - 1].rstrip() + "…"


def setup_questions(catalog: Catalog) -> list:
    """A new thread's model and effort, as choices over the thread agent's
    catalog (ch 11 §3.3); a choice with one option isn't asked."""
    questions = []
    if len(options := model_options(catalog)) >= MIN_OPTIONS:
        questions.append(dm.Choice(MODEL_ID, "If the message asks for new work, which model should do that work? A "
                                             "more capable model for hard work, a faster and lighter one for a small "
                                             "edit.", options))
    if len(catalog.all_efforts()) >= MIN_OPTIONS:
        questions.append(dm.Choice(EFFORT_ID, "If the message asks for new work, how much effort does that work need?",
                                   {e: EFFORT_FITS[e] for e in catalog.all_efforts()}))
    return questions


def project_questions(threads: list[Mapping[str, Any]], catalog: Catalog | None = None) -> list:
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
    return questions + (setup_questions(catalog) if catalog is not None else [])


def workspace_questions(projects: Mapping[str, str]) -> list:
    return [dm.Tags(PROJECTS_ID, "The content is a message to a workspace that holds these projects. Tag every "
                                 "project the message asks to change or asks about. Do not tag a project it only "
                                 "mentions in passing.", dict(projects))] if projects else []


def decide(kind: str, result: dm.Result, threshold: float, *, catalog: Catalog | None) -> dict[str, Any]:
    """The model's answers as a route: ``actions`` (empty: the agent takes
    the message), the confidence and the reasons. ``catalog`` is what a new
    thread's model and effort were asked from (None: they weren't)."""
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
        route["actions"] = [{"kind": "start_thread", **setup(result, threshold, catalog)}]
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


def setup(result: dm.Result, threshold: float, catalog: Catalog | None) -> dict[str, Any]:
    """A new thread's ``setup {model, effort}`` (no effort for a model that
    takes none) when the model is sure of each it was asked, or of the
    effort alone (asked, and one the default model takes): the default runs at
    it (with no default entry, no model named: the CLI's own). Otherwise
    ``unchosen``, why there is none: the thread then runs on its default and
    says so (§0.105). A question with one option has it as its answer; with
    none (the catalog lists only its default), the default is."""
    if catalog is None or not catalog.models:
        return {"unchosen": NOT_ASKED}
    default = next((m.id for m in catalog.models if m.is_default), None)
    options = model_options(catalog)
    model = _sure(result, MODEL_ID, threshold) if len(options) >= MIN_OPTIONS else next(iter(options), default)
    efforts = catalog.all_efforts()
    effort = _sure(result, EFFORT_ID, threshold) if len(efforts) >= MIN_OPTIONS else next(iter(efforts), None)
    if model is None:                           # sure of the work's effort at most, not of its model
        asked = len(efforts) >= MIN_OPTIONS     # an effort nobody chose says nothing about the work
        if not asked or effort is None or effort not in catalog.efforts(default):
            return {"unchosen": UNSURE}
        return {"setup": {**({"model": default} if default is not None else {}), "effort": effort}}
    if not catalog.efforts(model):
        return {"setup": {"model": model}}
    if effort is None:
        return {"unchosen": UNSURE}
    return {"setup": {"model": model, "effort": effort}}


def _sure(result: dm.Result, question: str, threshold: float) -> str | None:
    answer = result.answers.get(question)
    return None if answer is None or answer.abstains(threshold) else answer.chosen


async def classify(*, provider: str, model: str, kind: str, text: str, threads: list[Mapping[str, Any]],
                   projects: Mapping[str, str], threshold: float, catalog: Catalog | None) -> dict[str, Any]:
    """The decision model's route for the message. In a project, ``catalog``
    (the thread agent's, None when it can't be read) is what a new thread's
    model and effort are chosen from."""
    questions = workspace_questions(projects) if kind == rows.WORKSPACE \
        else project_questions(threads[:THREADS_ASKED], catalog)
    started = time.perf_counter()
    result = await dm.ask(provider, text[:CONTENT_MAX], questions, model=model) if questions \
        else dm.Result(provider, {})
    route = decide(kind, result, threshold, catalog=catalog)
    route["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return route


OWNER_ID = "owner"
#: "No project": a name no folder can have.
NO_PROJECT = "/none"


async def owning_project(engine: Engine, chat_id: str, *, proposal: str, projects: Mapping[str, str]) -> dict | None:
    """The duplicate check before a ``create_project`` item (ch 11 §9): when
    the workspace routes with a decision model, does an existing project
    already own the proposed work? A confident match becomes a suggestion on
    the item, never applied by itself; with no model, the coordinator's own
    ``list_projects`` is the check."""
    routing = await engine.store.read(lambda conn: effective(conn, chat_id))
    if routing["mode"] != DECISION or not projects:
        return None
    provider = routing["decision_model"]
    chosen = await engine.store.read(lambda conn: read_setting(conn, MODELS_KEY, {}))
    question = dm.Choice(OWNER_ID, "The content proposes a new project. Does one of the existing projects already "
                                   "own this work?", {**projects, NO_PROJECT: "none of these projects owns it"})
    result = await dm.ask(provider, proposal[:CONTENT_MAX], [question],
                          model=(chosen.get(provider) or {}).get("model") or provider)
    answer = result.answers.get(OWNER_ID)
    if answer is None or answer.abstains(routing["abstain_below"]) or answer.chosen == NO_PROJECT:
        return None
    return {"forward_to": answer.chosen, "probability": answer.probability, "by": provider}

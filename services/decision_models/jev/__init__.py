"""TypeSafe Jev (provider ``jev``) through its SystemOne endpoint.

Every request goes through ``client.py``. All questions go in one request (Jev
evaluates them in parallel). Jev has no tags kind, so a tags question is asked
as one ``noul`` (yes/no) per tag, and ``applies`` is derived with the shared
thresholds. Jev never abstains; the caller's confidence threshold does.

SystemOne: ``{state, model, questions}`` →
``{answers: {id: {choice, probabilities} | {noul}}, usage: {input_tokens}}``.
"""

from __future__ import annotations

from services.decision_models.interface import (
    Answer, Choice, Provider, Question, Result, applies_from, failed,
)
from services.decision_models.jev import client

ID = "jev"
_SEP = "__"


def request_body(content: str, questions: list[Question], model: str) -> dict:
    out: dict = {}
    for q in questions:
        if isinstance(q, Choice):
            out[q.id] = {"type": "choice", "instructions": q.instructions, "criteria": dict(q.options)}
        else:
            for tag, description in q.tags.items():
                out[f"{q.id}{_SEP}{tag}"] = {"type": "noul", "instructions": f"{q.instructions} {tag}: {description}"}
    return {"state": content, "model": model, "questions": out}


def parse(questions: list[Question], data: dict) -> dict[str, Answer]:
    answers = data.get("answers") or {}
    out = {}
    for q in questions:
        if isinstance(q, Choice):
            a = answers.get(q.id) or {}
            probs = {k: v for k, v in (a.get("probabilities") or {}).items()
                     if k in q.options and isinstance(v, (int, float))}
            chosen = a.get("choice") if a.get("choice") in q.options else None
            out[q.id] = Answer(q.id, "choice", chosen=chosen, probability=probs.get(chosen) if chosen else None,
                               probabilities=probs)
        else:
            probs = {}
            for tag in q.tags:
                p = (answers.get(f"{q.id}{_SEP}{tag}") or {}).get("noul")
                if isinstance(p, (int, float)):
                    probs[tag] = float(p)
            out[q.id] = Answer(q.id, "tags", probabilities=probs, applies={k: applies_from(v) for k, v in probs.items()})
    return out


async def ask(content: str, questions: list[Question], *, model: str, timeout: float) -> Result:
    res = await client.systemone(request_body(content, questions, model), timeout=timeout)
    if not res.ok:
        return failed(ID, questions, res.kind)
    return Result(ID, parse(questions, res.data),
                  input_tokens=int((res.data.get("usage") or {}).get("input_tokens") or 0))


PROVIDER = Provider(id=ID, label="TypeSafe Jev", docs_url="https://docs.typesafe.ai",
                    env_key=client.ENV_API_KEY, default_model="jev-latest", ask=ask)

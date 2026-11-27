"""Levanto Sage (provider ``sage``) through its ``/decide`` endpoint (native
choice and tags).

Every request goes through ``client.py``. Questions are sent concurrently, one
``/decide`` each. Sage serves one model, so the connection's model is only a
label. Sage's ``chosen: null`` (too close to call) and tags' ``applies: null``
are kept as "no pick" / "unsure".
"""

from __future__ import annotations

import asyncio

from services.decision_models.interface import Answer, Choice, Provider, Question, Result, kind_of
from services.decision_models.levanto import client
from services.decision_models.transport import Reply

ID = "sage"


def _question(q: Question) -> dict:
    if isinstance(q, Choice):
        return {"id": q.id, "kind": "choice", "instructions": q.instructions,
                "options": [{"option": k, "description": v} for k, v in q.options.items()]}
    return {"id": q.id, "kind": "tags", "instructions": q.instructions,
            "tags": [{"id": k, "name": f"{k}: {v}"} for k, v in q.tags.items()]}


def _answer(q: Question, res: Reply) -> Answer:
    kind = kind_of(q)
    if not res.ok:
        return Answer(q.id, kind, error=res.kind)
    body = res.data.get("result") or {}
    if isinstance(q, Choice):
        probs = {i.get("option"): i.get("probability") for i in body.get("probabilities") or []
                 if isinstance(i, dict) and i.get("option") in q.options}
        chosen = body.get("chosen") if body.get("chosen") in q.options else None
        return Answer(q.id, kind, chosen=chosen, probability=probs.get(chosen) if chosen else None,
                      probabilities={k: v for k, v in probs.items() if isinstance(v, (int, float))})
    tags = {t.get("id"): t for t in body.get("tags") or [] if isinstance(t, dict) and t.get("id") in q.tags}
    return Answer(q.id, kind, probabilities={k: t.get("probability") for k, t in tags.items()
                                             if isinstance(t.get("probability"), (int, float))},
                  applies={k: t.get("applies") if isinstance(t.get("applies"), bool) else None for k, t in tags.items()})


async def ask(content: str, questions: list[Question], *, model: str, timeout: float) -> Result:
    results = await asyncio.gather(*(client.decide(content, _question(q), timeout=timeout) for q in questions))
    tokens = sum(((r.data or {}).get("meta") or {}).get("usage", {}).get("billed_input_tokens") or 0
                 for r in results if r.ok)
    return Result(ID, {q.id: _answer(q, r) for q, r in zip(questions, results)}, input_tokens=int(tokens))


PROVIDER = Provider(id=ID, label="Levanto Sage", docs_url="https://docs.levanto.ai",
                    env_key=client.ENV_API_KEY, default_model="sage", ask=ask)

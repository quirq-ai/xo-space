"""Levanto Sage through its ``/decide`` endpoint (native choice and tags).

Every request goes through ``services/levanto/client.py``, the only module that
reads the Levanto key. Questions are sent concurrently, one ``/decide`` each.
Sage's ``chosen: null`` (too close to call) and tags' ``applies: null`` are
kept as "no pick" / "unsure".
"""

from __future__ import annotations

import asyncio

from services.decision_models import Answer, Choice, Result, Tags
from services.levanto import client


#: The env var holding the key, as the Levanto client names it (it is its only reader).
ENV_KEY = client.ENV_API_KEY
LABEL = "Levanto Sage"
DOCS_URL = "https://docs.levanto.ai"


def configured() -> bool:
    return client.configured()


def _question(q) -> dict:
    if isinstance(q, Choice):
        return {"id": q.id, "kind": "choice", "instructions": q.instructions,
                "options": [{"option": k, "description": v} for k, v in q.options.items()]}
    return {"id": q.id, "kind": "tags", "instructions": q.instructions,
            "tags": [{"id": k, "name": f"{k}: {v}"} for k, v in q.tags.items()]}


def _answer(q, res: client.SageResult) -> Answer:
    kind = "choice" if isinstance(q, Choice) else "tags"
    if not res.ok:
        return Answer(q.id, kind, error=res.kind or "error")
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


async def ask(content: str, questions: list, *, timeout: float) -> Result:
    results = await asyncio.gather(*(client.decide(content, _question(q), timeout=timeout) for q in questions))
    tokens = sum(((r.data or {}).get("meta") or {}).get("usage", {}).get("billed_input_tokens") or 0
                 for r in results if r.ok)
    return Result("sage", {q.id: _answer(q, r) for q, r in zip(questions, results)},
                  input_tokens=int(tokens), latency_ms=max((r.latency_ms for r in results), default=0.0))

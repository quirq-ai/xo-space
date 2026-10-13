"""TypeSafe Jev through its SystemOne endpoint (``POST /v1/systemone``).

This module is the only reader of ``TYPESAFE_API_KEY``; without a key nothing
is sent. All questions go in one request (Jev evaluates them in parallel).
Jev has no tags kind, so a tags question is asked as one ``noul`` (yes/no)
per tag, and ``applies`` is derived with the shared thresholds. Jev never
abstains; the caller's confidence threshold does.

Docs: https://docs.typesafe.ai/api.md (``{state, model, questions}`` →
``{answers: {id: {choice, probabilities} | {noul}}, usage: {input_tokens}}``;
401 bad key, 422 validation, 429 rate limit, 529 overloaded).
"""

from __future__ import annotations

import os
import time

import httpx

from services.decision_models import Answer, Choice, Result, Tags, applies_from

BASE_URL = "https://api.typesafe.ai"
ENV_API_KEY = "TYPESAFE_API_KEY"
MODEL = "jev-latest"
_SEP = "__"


def api_key() -> str | None:
    key = (os.getenv(ENV_API_KEY, "") or "").strip()
    return key or None


def configured() -> bool:
    return api_key() is not None


def request_body(content: str, questions: list) -> dict:
    out: dict = {}
    for q in questions:
        if isinstance(q, Choice):
            out[q.id] = {"type": "choice", "instructions": q.instructions, "criteria": dict(q.options)}
        else:
            for tag, description in q.tags.items():
                out[f"{q.id}{_SEP}{tag}"] = {"type": "noul", "instructions": f"{q.instructions} {tag}: {description}"}
    return {"state": content, "model": MODEL, "questions": out}


def _kind(status: int) -> str:
    return {401: "auth", 422: "invalid", 429: "rate", 529: "overloaded"}.get(status, "server" if status >= 500 else "http")


def parse(questions: list, data: dict) -> dict[str, Answer]:
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


async def ask(content: str, questions: list, *, timeout: float) -> Result:
    key = api_key()
    if key is None:
        return Result("jev", {q.id: Answer(q.id, "choice" if isinstance(q, Choice) else "tags", error="not_configured")
                              for q in questions})
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.post(f"{BASE_URL}/v1/systemone", json=request_body(content, questions),
                                   headers={"Authorization": f"Bearer {key}", "User-Agent": "xo-space"})
    except httpx.TimeoutException:
        error = "timeout"
    except Exception:  # noqa: BLE001 - a network failure is an answer, not a crash
        error = "offline"
    else:
        latency = round((time.perf_counter() - started) * 1000, 1)
        if 200 <= resp.status_code < 300:
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                return Result("jev", parse(questions, data),
                              input_tokens=int((data.get("usage") or {}).get("input_tokens") or 0), latency_ms=latency)
            error = "bad_response"
        else:
            error = _kind(resp.status_code)
    return Result("jev", {q.id: Answer(q.id, "choice" if isinstance(q, Choice) else "tags", error=error)
                          for q in questions})

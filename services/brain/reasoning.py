"""What the brain asks a model, and how it reads the answers (Brain.md §9).

Every prompt asks for one JSON object and every answer is parsed leniently
(the first ``{...}`` block wins), so a model that wraps its JSON in prose or
a code fence still works. An answer that does not parse raises
:class:`BadAnswer`; callers fall back to the statistical path or report the
step as failed, never invent a result.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from services.brain import text as tx
from services.brain.chunker import Chunk
from services.brain.extract import Concept, Relation, Understanding
from services.brain.model import BrainModel

SYSTEM = (
    "You are the reasoning layer of a knowledge graph. Answer with one JSON object and nothing else. "
    "Use only what the input supports; when unsure, leave a field out rather than guess."
)
BATCH_CHARS = 9000


class BadAnswer(Exception):
    pass


def parse_json(answer: str) -> Any:
    cleaned = re.sub(r"^```(?:json)?|```$", "", answer.strip(), flags=re.M).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    start = cleaned.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start:i + 1])
                    except ValueError:
                        break
        start = cleaned.find("{", start + 1)
    raise BadAnswer("the model's answer held no JSON object")


async def ask_json(model: BrainModel, prompt: str, *, max_tokens: int = 2000) -> dict:
    answer = await model.complete(prompt, system=SYSTEM, max_tokens=max_tokens)
    data = parse_json(answer)
    if not isinstance(data, dict):
        raise BadAnswer("the model's answer was not a JSON object")
    return data


def _s(value: Any, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


# ── Understand ───────────────────────────────────────────────────────────────


def batches(chunks: list[tuple[int, str, Chunk]]) -> list[list[tuple[int, str, Chunk]]]:
    out: list[list[tuple[int, str, Chunk]]] = []
    cur: list[tuple[int, str, Chunk]] = []
    size = 0
    for item in chunks:
        length = min(len(item[2].text), 3000)
        if cur and size + length > BATCH_CHARS:
            out.append(cur)
            cur, size = [], 0
        cur.append(item)
        size += length
    if cur:
        out.append(cur)
    return out


async def extract(model: BrainModel, batch: list[tuple[int, str, Chunk]]) -> dict[int, Understanding]:
    """One call for a batch of chunks: ``{index: Understanding}``."""
    parts = []
    for index, path, chunk in batch:
        parts.append(f"### chunk {index} ({path}, lines {chunk.start_line}-{chunk.end_line})\n{chunk.text[:3000]}")
    prompt = (
        "For each chunk below, list the concepts it uses or explains and the relationships between them.\n"
        "A concept: a technique, component, idea or entity worth remembering (not a variable or a filler word).\n"
        "Return {\"chunks\": [{\"id\": <chunk number>, \"concepts\": [{\"name\": short canonical name, "
        "\"description\": one generic sentence true outside this chunk, \"usage\": how this chunk uses it, "
        "\"level\": 0 concrete to 3 very abstract, \"keywords\": [up to 5 words]}], "
        "\"relations\": [{\"subject\": concept name, \"relation\": short verb phrase as written or implied, "
        "\"object\": concept name}]}]}.\n"
        "Include \"is a kind of\" relations when a concept is a specific case of another.\n\n"
        + "\n\n".join(parts))
    data = await ask_json(model, prompt, max_tokens=4000)
    out: dict[int, Understanding] = {}
    wanted = {index for index, _, _ in batch}
    for row in data.get("chunks") or []:
        if not isinstance(row, dict) or row.get("id") not in wanted:
            continue
        concepts: list[Concept] = []
        names: dict[str, str] = {}
        for c in row.get("concepts") or []:
            if not isinstance(c, dict):
                continue
            name = _s(c.get("name"), 80)
            key = tx.phrase_key(name)
            if not key or key in names.values():
                continue
            names[name.lower()] = key
            level = c.get("level")
            concepts.append(Concept(
                name=name, key=key, weight=2.0, usage=_s(c.get("usage"), 240),
                description=_s(c.get("description"), 400),
                level=max(0, min(5, int(level))) if isinstance(level, (int, float)) else 0,
                keywords=[_s(k, 40).lower() for k in (c.get("keywords") or []) if _s(k, 40)][:5]))
        relations: list[Relation] = []
        for r in row.get("relations") or []:
            if not isinstance(r, dict):
                continue
            subj = tx.phrase_key(_s(r.get("subject"), 80))
            obj = tx.phrase_key(_s(r.get("object"), 80))
            phrase = _s(r.get("relation"), 60).lower()
            keys = {c.key for c in concepts}
            if subj in keys and obj in keys and subj != obj and phrase:
                relations.append(Relation(subj, phrase, obj))
        out[row["id"]] = Understanding(concepts, relations, extractor="model")
    return out


async def same_concept(model: BrainModel, a: tuple[str, str], b: tuple[str, str]) -> bool:
    data = await ask_json(model, (
        "Do these two descriptions name the same concept (the same thing, not merely related)?\n"
        f"A: {a[0]} — {a[1]}\nB: {b[0]} — {b[1]}\n"
        "Return {\"same\": true|false}."), max_tokens=100)
    return data.get("same") is True


# ── Learn relation types ─────────────────────────────────────────────────────


async def group_relations(model: BrainModel, labels: list[str]) -> list[list[str]]:
    data = await ask_json(model, (
        "Group these relation phrases so that each group means the same relationship "
        "(e.g. \"speeds up\", \"accelerates\", \"makes faster\"). Every phrase appears in exactly one group; "
        "put the most typical phrase first.\n"
        f"Phrases: {json.dumps(labels)}\nReturn {{\"groups\": [[phrase, ...], ...]}}."), max_tokens=2000)
    groups = []
    for group in data.get("groups") or []:
        if isinstance(group, list):
            clean = [g for g in (_s(x, 60) for x in group) if g in labels]
            if clean:
                groups.append(clean)
    return groups


# ── Discover ─────────────────────────────────────────────────────────────────


async def name_pattern(model: BrainModel, members: dict[str, list[str]]) -> tuple[str, str]:
    listing = "\n".join(f"- in {src}: {', '.join(names)}" for src, names in members.items())
    data = await ask_json(model, (
        "The same group of connected concepts recurs in several sources, sometimes under different names:\n"
        f"{listing}\nName the recurring pattern and describe it in one or two sentences.\n"
        "Return {\"name\": ..., \"description\": ...}."), max_tokens=400)
    return _s(data.get("name"), 80), _s(data.get("description"), 600)


async def explain_analogy(model: BrainModel, source_a: str, source_b: str,
                          mapping: list[tuple[str, str]], suggestions: list[str]) -> str:
    pairs = "\n".join(f"- {a} ↔ {b}" for a, b in mapping)
    data = await ask_json(model, (
        f"Source \"{source_a}\" and source \"{source_b}\" share a structure:\n{pairs}\n"
        f"\"{source_a}\" also has: {', '.join(suggestions) or 'nothing more'}.\n"
        f"Explain in two sentences how \"{source_b}\" could do this the way \"{source_a}\" does.\n"
        "Return {\"explanation\": ...}."), max_tokens=400)
    return _s(data.get("explanation"), 800)


async def judge_hypotheses(model: BrainModel, pairs: list[dict]) -> dict[int, tuple[str, float]]:
    listing = "\n".join(
        f"{i}. {p['a']} ? {p['b']} (both connect to: {', '.join(p['via'])})" for i, p in enumerate(pairs))
    data = await ask_json(model, (
        "For each pair, say whether a direct relationship probably exists, name it with a short verb phrase, "
        f"and rate how plausible it is from 0 to 1.\n{listing}\n"
        "Return {\"pairs\": [{\"i\": number, \"relation\": phrase, \"plausibility\": 0..1}]}."), max_tokens=2000)
    out: dict[int, tuple[str, float]] = {}
    for row in data.get("pairs") or []:
        if isinstance(row, dict) and isinstance(row.get("i"), int) and 0 <= row["i"] < len(pairs):
            p = row.get("plausibility")
            out[row["i"]] = (_s(row.get("relation"), 60).lower(), float(p) if isinstance(p, (int, float)) else 0.0)
    return out


# ── Answer ───────────────────────────────────────────────────────────────────


async def answer(model: BrainModel, question: str, material: dict) -> dict:
    """A written answer from what recall found (``recall.answer_material``):
    ``{text, citations, missing}``. Citations are the numbered evidence the
    answer refers to; a number the material does not hold is dropped."""
    evidence = material.get("evidence") or []
    prompt = (
        "Answer the question using only the knowledge below, recalled from the user's own projects. "
        "Do not use tools or read files. Refer to evidence by its number in square brackets, like [2]. "
        "Where the knowledge does not cover something, say so plainly instead of guessing; "
        "anything listed under guesses may only be mentioned as a possibility.\n\n"
        f"Question: {question}\n"
        + (f"The question is about the project \"{material['context']}\".\n" if material.get("context") else "")
        + "\nKnowledge:\n" + json.dumps({k: material.get(k) for k in ("pieces", "guesses", "evidence")},
                                         ensure_ascii=False)[:16000]
        + "\n\nReturn {\"answer\": the answer in Markdown (short paragraphs or bullets), "
          "\"cited\": [evidence numbers used], \"missing\": what the knowledge does not cover, or \"\"}.")
    data = await ask_json(model, prompt, max_tokens=2000)
    text = data.get("answer") if isinstance(data.get("answer"), str) else ""
    if not text.strip():
        raise BadAnswer("the model's answer was empty")
    by_n = {e["n"]: e for e in evidence}
    cited = [n for n in data.get("cited") or [] if isinstance(n, int) and n in by_n]
    cited += [int(n) for n in re.findall(r"\[(\d{1,3})\]", text) if int(n) in by_n and int(n) not in cited]
    return {"text": text.strip()[:12000], "citations": [by_n[n] for n in sorted(set(cited))],
            "missing": _s(data.get("missing"), 600)}


# ── Create ───────────────────────────────────────────────────────────────────


async def propose_designs(model: BrainModel, goal: str, knowledge: dict) -> list[dict]:
    prompt = (
        f"Goal: {goal}\n\n"
        "Knowledge recalled for this goal, with ids. Facts are backed by evidence; hypotheses are guesses:\n"
        f"{json.dumps(knowledge, ensure_ascii=False)[:14000]}\n\n"
        "Combine this into 2 or 3 different designs for the goal. Each design says what it reuses (by id, "
        "and how) and what is new. Prefer proven knowledge; a hypothesis is a risk.\n"
        "Return {\"designs\": [{\"title\": ..., \"summary\": 2-4 sentences, "
        "\"reuses\": [{\"piece_id\": id or null, \"pattern_id\": id or null, \"how\": ...}], "
        "\"new_parts\": [short descriptions], \"risks\": [short descriptions], "
        "\"steps\": [build steps], \"test_command\": [argv list to verify the build, e.g. [\"pytest\", \"-q\"]] "
        "or null}]}.")
    data = await ask_json(model, prompt, max_tokens=4000)
    designs = [d for d in data.get("designs") or [] if isinstance(d, dict) and _s(d.get("title"), 120)]
    if not designs:
        raise BadAnswer("the model proposed no designs")
    return designs[:3]


def design_text(design: dict) -> str:
    """Everything a design says, as one string (for fit scoring)."""
    parts = [_s(design.get("title"), 200), _s(design.get("summary"), 2000)]
    parts += [_s(x, 200) for x in design.get("new_parts") or []]
    parts += [_s(r.get("how"), 200) for r in design.get("reuses") or [] if isinstance(r, dict)]
    return " ".join(p for p in parts if p)


def build_prompt(goal: str, design: dict, reused: list[dict], sources: dict[str, str]) -> str:
    lines = [
        f"Build this in the current folder (a new, empty project). Goal: {goal}",
        f"Design: {design.get('title')}\n{design.get('summary', '')}",
    ]
    if design.get("steps"):
        lines.append("Steps:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(design["steps"])))
    if reused:
        lines.append("Reuse this proven knowledge. The evidence is in other projects: read it, never modify it.")
        for item in reused:
            where = "; ".join(f"{sources.get(e['source_id'], e['source_id'])}/{e['path']}:{e['start_line']}-{e['end_line']}"
                              for e in item.get("evidence", [])[:3])
            lines.append(f"- {item['name']}: {item.get('how') or item.get('description', '')} [{where}]")
    if design.get("new_parts"):
        lines.append("New parts: " + "; ".join(str(x) for x in design["new_parts"]))
    test = design.get("test_command")
    if test:
        lines.append(f"When done, `{' '.join(test)}` must pass in this folder. Add tests if there are none.")
    lines.append("Finish with one line: RESULT: worked | failed | partial — and a one-sentence lesson.")
    return "\n\n".join(lines)


def fix_prompt(test_command: list[str], output: str) -> str:
    return (f"`{' '.join(test_command)}` failed in this project. Fix the cause (not the test) and "
            f"finish with RESULT: worked | failed | partial.\n\nOutput (last lines):\n{output[-4000:]}")


def reported_result(message: str) -> Optional[tuple[str, str]]:
    m = re.search(r"RESULT:\s*(worked|failed|partial)\b[\s—:-]*(.*)", message or "", re.I)
    if not m:
        return None
    return m.group(1).lower(), _s(m.group(2), 400)

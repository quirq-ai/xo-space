"""Create (Brain.md §8): goal → designs → approval → build → test → learn.

1. **recall** what the goal needs from every source: facts, hypotheses
   (marked), the patterns they belong to, the analogies over those patterns
   and past experiences with similar goals;
2. **combine**: the model proposes two or three designs, each saying what it
   reuses (by id) and what is new;
3. **choose**: designs are ranked by proven reuse, past experience, risk and
   fit. Nothing is built until a person approves one;
4. **build** the approved design in a new project (``scaffold_project``),
   never in a source, by the Space's active agent working in that folder;
5. **test** with the design's own test command (shown to the person before
   approval; argv only, no shell), asking the agent to fix a failure up to
   ``BRAIN_FIX_ATTEMPTS`` times;
6. **learn**: the new project becomes a source and is learned, and an
   experience records the result, moving the reused pieces' and patterns'
   scores up or down so later recalls and rankings reflect it.

Without a model, proposing answers 501 ``model_required`` (Brain.md §9: no
reasoning features without one). Recording an experience by hand works
either way.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from services.brain import config, graph, reasoning, recall, store, vectors
from services.brain import text as tx
from services.brain.discover import pattern_members
from services.brain.model import BrainModel
from services.timestamps import now_iso
from utils import commands

logger = logging.getLogger(__name__)

RESULTS = ("worked", "failed", "partial")
SCORE_DELTA = {"worked": 1.0, "partial": 0.25, "failed": -1.0}
TEST_ARGS_MAX = 20

Runner = Callable[[str, str], Awaitable[str]]


def _require_model(model: BrainModel) -> None:
    if not model.can_reason:
        detail = model.describe().get("error")
        raise store.BrainError(
            "model_required",
            "Designing needs a model. Set BRAIN_MODEL (e.g. BRAIN_MODEL=agent) and restart."
            + (f" ({detail})" if detail else ""), 501)


# ── 1. recall what the goal needs ────────────────────────────────────────────


def gather(conn: sqlite3.Connection, goal: str, context_source: Optional[str], now: str) -> dict:
    found = recall.recall(conn, goal, now=now, context_source=context_source, context=goal, limit=20,
                          explore=True, reinforce=False)
    fact_ids = [r["piece"]["id"] for r in found["results"]]
    hyp_ids = [r["piece"]["id"] for r in found["hypotheses"]]
    facts = [{"id": r["piece"]["id"], "name": r["piece"]["name"], "description": r["piece"]["description"][:240],
              "sources": [n["source"] for n in r["notes"]], "score": r["piece"]["score"],
              "why": r["explanation"][:240]} for r in found["results"]]
    hypotheses = [{"id": r["piece"]["id"], "name": r["piece"]["name"], "why": r["explanation"][:240]}
                  for r in found["hypotheses"]]
    patterns, analogies = [], []
    if fact_ids:
        marks = ",".join("?" * len(fact_ids))
        for row in conn.execute(
                f"SELECT DISTINCT p.* FROM patterns p JOIN pattern_members m ON m.pattern_id = p.id "
                f"WHERE m.piece_id IN ({marks}) ORDER BY p.support DESC, p.score DESC LIMIT 8", fact_ids):
            members = pattern_members(conn, row["id"])
            patterns.append({"id": row["id"], "name": row["name"], "description": row["description"],
                             "support": row["support"], "score": row["score"],
                             "members": {s: [m["name"] for m in ms][:8] for s, ms in members.items()}})
            for a in conn.execute("SELECT explanation FROM analogies WHERE pattern_id = ? ORDER BY score DESC LIMIT 2",
                                  (row["id"],)):
                analogies.append({"pattern_id": row["id"], "explanation": a[0][:400]})
    goal_vec = vectors.weighted(conn, vectors.text_vector(goal))
    experiences = []
    for row in conn.execute("SELECT goal, result, lessons FROM experiences ORDER BY id DESC LIMIT 200"):
        sim = tx.cosine(goal_vec, vectors.weighted(conn, vectors.text_vector(row["goal"])))
        if sim >= 0.2:
            experiences.append({"goal": row["goal"], "result": row["result"], "lessons": row["lessons"][:300],
                                "similarity": round(sim, 3)})
    experiences.sort(key=lambda e: -e["similarity"])
    return {"facts": facts, "hypotheses": hypotheses, "patterns": patterns, "analogies": analogies,
            "experiences": experiences[:5], "fact_ids": fact_ids, "hypothesis_ids": hyp_ids}


# ── 2–3. combine and choose ──────────────────────────────────────────────────


def _test_command(value) -> Optional[list[str]]:
    if not isinstance(value, list) or not value or len(value) > TEST_ARGS_MAX:
        return None
    if not all(isinstance(a, str) and a and "\x00" not in a and len(a) < 500 for a in value):
        return None
    return list(value)


def score_design(conn: sqlite3.Connection, goal: str, design: dict, fact_ids: set[int],
                 hyp_ids: set[int]) -> tuple[dict, dict]:
    """``(plan, scores)``: the design with its references resolved, and why
    it ranks where it does."""
    reuses, pieces, patterns, hypotheses_used = [], [], [], 0
    for r in design.get("reuses") or []:
        if not isinstance(r, dict):
            continue
        how = re.sub(r"\s+", " ", str(r.get("how") or ""))[:300]
        pid, pat = r.get("piece_id"), r.get("pattern_id")
        if isinstance(pid, int):
            row = conn.execute("SELECT id, name, score FROM pieces WHERE id = ?", (pid,)).fetchone()
            if row is None:
                continue
            sources = conn.execute("SELECT COUNT(*) FROM notes WHERE piece_id = ?", (pid,)).fetchone()[0]
            guess = pid in hyp_ids and pid not in fact_ids
            hypotheses_used += int(guess)
            reuses.append({"piece_id": pid, "name": row["name"], "how": how, "sources": sources, "guess": guess})
            pieces.append((row["score"], min(1.0, sources / 2.0) * (0.3 if guess else 1.0)))
        elif isinstance(pat, int):
            row = conn.execute("SELECT id, name, support, score FROM patterns WHERE id = ?", (pat,)).fetchone()
            if row is None:
                continue
            reuses.append({"pattern_id": pat, "name": row["name"], "how": how, "sources": row["support"],
                           "guess": False})
            patterns.append((row["score"], min(1.0, row["support"] / 2.0)))
    new_parts = [re.sub(r"\s+", " ", str(x))[:300] for x in design.get("new_parts") or [] if str(x).strip()][:12]
    risks = [re.sub(r"\s+", " ", str(x))[:300] for x in design.get("risks") or [] if str(x).strip()][:12]
    steps = [re.sub(r"\s+", " ", str(x))[:400] for x in design.get("steps") or [] if str(x).strip()][:20]
    proven = sum(p for _, p in pieces) + sum(p for _, p in patterns)
    reuse = proven / max(1, len(pieces) + len(patterns) + len(new_parts))
    scored = [s for s, _ in pieces] + [s for s, _ in patterns]
    experience = sum(0.5 + 0.5 * math.tanh(s) for s in scored) / len(scored) if scored else 0.5
    unknowns = len(new_parts) + len(risks) + 2 * hypotheses_used
    risk = unknowns / (unknowns + len(pieces) + len(patterns) + 1)
    fit = tx.cosine(vectors.weighted(conn, vectors.text_vector(goal)),
                    vectors.weighted(conn, vectors.text_vector(reasoning.design_text(design))))
    total = 0.35 * reuse + 0.2 * experience + 0.25 * fit + 0.2 * (1 - risk)
    plan = {"reuses": reuses, "new_parts": new_parts, "risks": risks, "steps": steps,
            "test_command": _test_command(design.get("test_command"))}
    scores = {"reuse": round(reuse, 4), "experience": round(experience, 4), "risk": round(risk, 4),
              "fit": round(fit, 4), "total": round(total, 4)}
    return plan, scores


def save_designs(conn: sqlite3.Connection, goal: str, context_source: Optional[str], designs: list[dict],
                 knowledge: dict, now: str) -> int:
    goal_id = int(conn.execute("INSERT INTO goals(text, context_source, status, created_at, updated_at) "
                               "VALUES (?,?,?,?,?)", (goal, context_source, "proposed", now, now)).lastrowid)
    ranked = []
    for d in designs:
        plan, scores = score_design(conn, goal, d, set(knowledge["fact_ids"]), set(knowledge["hypothesis_ids"]))
        ranked.append((scores["total"], d, plan, scores))
    ranked.sort(key=lambda x: -x[0])
    for rank, (total, d, plan, scores) in enumerate(ranked, start=1):
        conn.execute(
            "INSERT INTO designs(goal_id, title, summary, plan, scores, score, rank, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (goal_id, str(d.get("title"))[:200], re.sub(r"\s+", " ", str(d.get("summary") or ""))[:2000],
             store.dumps(plan), store.dumps(scores), total, rank, "proposed", now, now))
    store.upsert_finding(conn, kind="design", ref=str(goal_id), title=f"Designs ready for approval: {goal[:200]}",
                         body=f"{len(ranked)} designs, best: {ranked[0][1].get('title')}", source_id=context_source,
                         now=now)
    return goal_id


async def propose(model: BrainModel, goal: str, *, context_source: Optional[str] = None,
                  db: Optional[Path] = None) -> int:
    _require_model(model)
    goal = re.sub(r"\s+", " ", goal).strip()
    if not goal:
        raise store.BrainError("invalid_value", "goal is required.")
    now = now_iso()

    def load() -> dict:
        with store.write(db) as conn:
            return gather(conn, goal, context_source, now)

    knowledge = await asyncio.to_thread(load)
    for_model = {k: knowledge[k] for k in ("facts", "hypotheses", "patterns", "analogies", "experiences")}
    try:
        designs = await reasoning.propose_designs(model, goal, for_model)
    except reasoning.BadAnswer as exc:
        raise store.BrainError("model_answer_invalid", f"The model's designs could not be read: {exc}", 502) from exc

    def save() -> int:
        with store.write(db) as conn:
            return save_designs(conn, goal, context_source, designs, knowledge, now_iso())

    return await asyncio.to_thread(save)


def approve(conn: sqlite3.Connection, design_id: int, now: str) -> None:
    row = conn.execute("SELECT id, goal_id, status FROM designs WHERE id = ?", (design_id,)).fetchone()
    if row is None:
        raise store.BrainError("design_not_found", f"No design {design_id}.", 404)
    if row["status"] not in ("proposed", "approved"):
        raise store.BrainError("design_not_approvable", f"Design {design_id} is {row['status']}.", 409)
    conn.execute("UPDATE designs SET status = 'approved', updated_at = ? WHERE id = ?", (now, design_id))
    conn.execute("UPDATE designs SET status = 'set_aside', updated_at = ? WHERE goal_id = ? AND id != ? "
                 "AND status = 'proposed'", (now, row["goal_id"], design_id))
    conn.execute("UPDATE goals SET status = 'approved', updated_at = ? WHERE id = ?", (now, row["goal_id"]))
    conn.execute("UPDATE findings SET status = 'done', updated_at = ? WHERE kind = 'design' AND ref = ?",
                 (now, str(row["goal_id"])))


# ── 6. experience ────────────────────────────────────────────────────────────


def record_experience(conn: sqlite3.Connection, *, goal: str, result: str, lessons: str = "",
                      design_id: Optional[int] = None, source_id: Optional[str] = None,
                      piece_ids: list[int] = (), pattern_ids: list[int] = (), now: str) -> int:
    if result not in RESULTS:
        raise store.BrainError("invalid_value", "result must be worked, failed or partial.")
    pieces = [p for p in dict.fromkeys(piece_ids)
              if conn.execute("SELECT 1 FROM pieces WHERE id = ?", (p,)).fetchone()]
    patterns = [p for p in dict.fromkeys(pattern_ids)
                if conn.execute("SELECT 1 FROM patterns WHERE id = ?", (p,)).fetchone()]
    if source_id and not conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone():
        raise store.BrainError("source_not_found", f"No source {source_id!r}.", 404)
    eid = int(conn.execute(
        "INSERT INTO experiences(goal, design_id, source_id, result, lessons, pieces, patterns, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (goal[:1000], design_id, source_id, result, lessons[:2000], store.dumps(pieces), store.dumps(patterns),
         now)).lastrowid)
    delta = SCORE_DELTA[result]
    conn.executemany("UPDATE pieces SET score = score + ? WHERE id = ?", [(delta, p) for p in pieces])
    conn.executemany("UPDATE patterns SET score = score + ? WHERE id = ?", [(delta, p) for p in patterns])
    amount = 0.2 if result == "worked" else 0.05
    for i, a in enumerate(pieces):
        for b in pieces[i + 1:]:
            graph.strengthen(conn, a, b, amount=amount, now=now, phrase=graph.USED_TOGETHER,
                             kind="generated", signal="experience")
    if source_id:
        conn.executemany(
            "INSERT INTO notes(piece_id, source_id, note, strength, mentions, origin, updated_at) "
            "VALUES (?,?,?,?,0,'experience',?) ON CONFLICT(piece_id, source_id) DO NOTHING",
            [(p, source_id, f"Reused for: {goal[:200]} ({result})", 0.5, now) for p in pieces])
    return eid


# ── 4–5. build and test ──────────────────────────────────────────────────────


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:40].rstrip("-") or "build")


def build_agent() -> str:
    """The harness that builds: ``BRAIN_BUILD_AGENT`` (any installed agent),
    else the active one. An unknown name raises with the installed ones."""
    from services.brain.model.agent import resolve_agent

    return resolve_agent(config.build_agent())


async def agent_runner(prompt: str, project: str) -> str:
    """The build agent (:func:`build_agent`), working inside ``project``'s folder."""
    from services.brain.model.agent import ask_agent

    return await asyncio.wait_for(ask_agent(build_agent(), prompt, agent_id=project),
                                  timeout=config.build_timeout_s())


async def build(design_id: int, *, model: BrainModel, runner: Optional[Runner] = None,
                db: Optional[Path] = None, learn_budget: Optional[int] = None) -> dict:
    """Build an approved design end to end. Returns the design's build record."""
    from services.brain import learn
    from services.cowork_agent import project_layout

    runner = runner or agent_runner
    now = now_iso()

    def start() -> tuple[dict, dict, str, list[dict], dict]:
        with store.write(db) as conn:
            d = conn.execute("SELECT d.*, g.text AS goal FROM designs d JOIN goals g ON g.id = d.goal_id "
                             "WHERE d.id = ?", (design_id,)).fetchone()
            if d is None:
                raise store.BrainError("design_not_found", f"No design {design_id}.", 404)
            if d["status"] != "approved":
                raise store.BrainError("design_not_approved",
                                       f"Design {design_id} is {d['status']}; approve it before building.", 409)
            base, name, n = f"brain-{slug(d['title'])}", None, 1
            while name is None or project_layout.project_dir_exists(name):
                name = base if n == 1 else f"{base}-{n}"
                n += 1
            plan = store.loads(d["plan"], {})
            reused = []
            for r in plan.get("reuses", []):
                if r.get("piece_id"):
                    brief = recall.piece_brief(conn, r["piece_id"]) if conn.execute(
                        "SELECT 1 FROM pieces WHERE id = ?", (r["piece_id"],)).fetchone() else None
                    if brief:
                        reused.append({**brief, "how": r.get("how", ""),
                                       "evidence": recall.evidence(conn, r["piece_id"], limit=3)})
            sources = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM sources")}
            build_rec = {"started_at": now, "project": name, "attempts": []}
            conn.execute("UPDATE designs SET status = 'building', project = ?, build = ?, updated_at = ? WHERE id = ?",
                         (name, store.dumps(build_rec), now, design_id))
            return dict(d), plan, name, reused, {"sources": sources, "build": build_rec}

    d, plan, project, reused, extra = await asyncio.to_thread(start)
    build_rec = extra["build"]

    def finish(status: str, outcome: Optional[str] = None, lessons: str = "", source_id: Optional[str] = None,
               error: str = "") -> None:
        with store.write(db) as conn:
            build_rec.update(finished_at=now_iso(), outcome=outcome, error=error[:1000] or None)
            conn.execute("UPDATE designs SET status = ?, build = ?, updated_at = ? WHERE id = ?",
                         (status, store.dumps(build_rec), now_iso(), design_id))
            conn.execute("UPDATE goals SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), d["goal_id"]))
            if outcome:
                record_experience(
                    conn, goal=d["goal"], result=outcome, lessons=lessons, design_id=design_id, source_id=source_id,
                    piece_ids=[r["piece_id"] for r in plan.get("reuses", []) if r.get("piece_id")],
                    pattern_ids=[r["pattern_id"] for r in plan.get("reuses", []) if r.get("pattern_id")],
                    now=now_iso())

    try:
        if runner is agent_runner:
            build_rec["agent"] = build_agent()
        await asyncio.to_thread(project_layout.scaffold_project, project, display_name=d["title"][:120],
                                description=d["goal"][:300])
        pdir = project_layout.project_dir(project)
        message = await runner(reasoning.build_prompt(d["goal"], {**plan, "title": d["title"],
                                                                  "summary": d["summary"]},
                                                      reused, extra["sources"]), project)
        reported = reasoning.reported_result(message)
        outcome, lessons = (reported or ("partial", ""))
        test = plan.get("test_command")
        if test:
            outcome = "failed"
            for attempt in range(config.fix_attempts() + 1):
                res = await commands.run(test, cwd=pdir, timeout=config.test_timeout_s(),
                                         log_label=f"brain: test design {design_id}")
                build_rec["attempts"].append({"attempt": attempt, "ok": res.ok, "returncode": res.returncode,
                                              "output_tail": res.output[-1500:]})
                if res.ok:
                    outcome = "worked"
                    break
                if attempt < config.fix_attempts():
                    message = await runner(reasoning.fix_prompt(test, res.output), project)
                    reported = reasoning.reported_result(message) or reported
            if reported and reported[1]:
                lessons = reported[1]
            if outcome == "failed" and not lessons:
                lessons = f"`{' '.join(test)}` still failed after {config.fix_attempts()} fixes."
        build_rec["agent_reply_tail"] = message[-1500:]
        await asyncio.to_thread(_write_episode, pdir, d, outcome, lessons, plan)
        source_id = await asyncio.to_thread(_register_built, db, project, pdir)
        if source_id:
            budget = config.model_calls_per_learn() if learn_budget is None else learn_budget
            try:
                await learn.learn_source(source_id, model=model, budget=budget, db=db)
            except Exception as exc:  # noqa: BLE001 - the build result stands without it
                build_rec["learn_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - recorded on the design, never lost
        logger.exception("brain: building design %s failed", design_id)
        await asyncio.to_thread(finish, "failed", None, "", None, f"{type(exc).__name__}: {exc}")
        return build_rec
    await asyncio.to_thread(finish, "built" if outcome != "failed" else "failed", outcome, lessons, source_id)
    return build_rec


def _register_built(db: Optional[Path], project: str, pdir: Path) -> Optional[str]:
    from services.brain import learn
    from services.storage.reader import read_json

    meta = read_json(pdir / ".xo" / "project.json") or {}
    pid = meta.get("pid") if isinstance(meta, dict) else None
    if not isinstance(pid, str) or not pid:
        return None
    with store.write(db) as conn:
        learn.register_source(conn, source_id=pid, kind="built", name=project, location=str(pdir), now=now_iso())
    return pid


def _write_episode(pdir: Path, design: dict, outcome: Optional[str], lessons: str, plan: dict) -> None:
    """The template's episodic memory format, so agents in the built
    project see how it came to be."""
    folder = pdir / "memory" / "episodic"
    if not folder.is_dir() or not outcome:
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mapped = {"worked": "success", "failed": "failure", "partial": "partial"}[outcome]
    reused = ", ".join(r["name"] for r in plan.get("reuses", [])) or "nothing recorded"
    body = (f"---\ndate: {day}\ntags: [brain, build]\noutcome: {mapped}\n---\n\n"
            f"## What\nBuilt by the knowledge brain: {design['title']}.\n\n"
            f"## Why it mattered\nGoal: {design['goal']}\n\n"
            f"## How it went\nDesign: {design['summary']}\nReused: {reused}.\n"
            f"Result: {outcome}. {lessons}\n")
    try:
        (folder / f"{day}-brain-{slug(design['title'])}.md").write_text(body, encoding="utf-8")
    except OSError as exc:
        logger.warning("brain: could not write the build episode in %s: %s", folder, exc)

"""The brain's one router-facing surface.

``routers/cowork_agent/bff/brain.py`` calls only this module; everything it
raises is a :class:`services.brain.store.BrainError` (a ``ServiceError``).
Learning and building run as background tasks of this process, one per
source and one per design; their progress is read back from ``runs`` and
``designs``. The Inbox reads :func:`inbox_findings` (inbox → brain, never
the other way round).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from services.brain import config, create, discover, graph, learn, reasoning, recall, store, vectors
from services.brain.model import BrainModel, load_model
from services.timestamps import now_iso

logger = logging.getLogger(__name__)

SOURCE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
FINDING_STATUSES = ("open", "done")
CUE_MAX = 2000

_learning: dict[str, asyncio.Task] = {}
_building: dict[int, asyncio.Task] = {}
_background: set[asyncio.Task] = set()
_discover_lock: Optional[asyncio.Lock] = None
_recovered = False


def model() -> BrainModel:
    return load_model()


def _source_id(value: str) -> str:
    if not isinstance(value, str) or not SOURCE_ID_RE.fullmatch(value):
        raise store.BrainError("source_not_found", "Unknown source id.", 404)
    return value


def _recover() -> None:
    """A build that was running when the server stopped is failed, with why."""
    global _recovered
    if _recovered:
        return
    _recovered = True
    with store.write() as conn:
        for row in conn.execute("SELECT id, build FROM designs WHERE status = 'building'").fetchall():
            if row["id"] in _building:
                continue
            rec = store.loads(row["build"], {})
            rec.update(error="interrupted: the server stopped while this design was building", finished_at=now_iso())
            conn.execute("UPDATE designs SET status = 'failed', build = ?, updated_at = ? WHERE id = ?",
                         (store.dumps(rec), now_iso(), row["id"]))
        conn.execute("UPDATE sources SET status = 'error', error = 'interrupted: the server stopped while learning' "
                     "WHERE status = 'learning'")
        conn.execute("UPDATE runs SET status = 'failed', error = 'interrupted', finished_at = ? "
                     "WHERE status = 'running'", (now_iso(),))


def _spawn(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


# ── status ───────────────────────────────────────────────────────────────────


def status() -> dict:
    _recover()
    with store.connect() as conn:
        count = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        counts = {
            "sources": count("SELECT COUNT(*) FROM sources"),
            "chunks": count("SELECT COUNT(*) FROM chunks"),
            "pieces": count("SELECT COUNT(*) FROM pieces"),
            "shared_pieces": count("SELECT COUNT(*) FROM (SELECT piece_id FROM notes GROUP BY piece_id "
                                   "HAVING COUNT(*) > 1)"),
            "links": count("SELECT COUNT(*) FROM links WHERE kind != 'hypothesis'"),
            "hypotheses": count("SELECT COUNT(*) FROM links WHERE kind = 'hypothesis'"),
            "relations": count("SELECT COUNT(*) FROM relations"),
            "patterns": count("SELECT COUNT(*) FROM patterns"),
            "analogies": count("SELECT COUNT(*) FROM analogies"),
            "experiences": count("SELECT COUNT(*) FROM experiences"),
            "open_findings": count("SELECT COUNT(*) FROM findings WHERE status = 'open'"),
        }
        last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return {"schema": store.SCHEMA, "store": str(store.db_path()), "model": model().describe(),
            "loop": {"enabled": config.loop_enabled(), "tick_seconds": config.tick_seconds()},
            "counts": counts, "learning": sorted(_learning), "building": sorted(_building),
            "last_run": _run_dict(last) if last else None}


def _run_dict(row) -> dict:
    return {"id": row["id"], "kind": row["kind"], "source_id": row["source_id"], "status": row["status"],
            "started_at": row["started_at"], "finished_at": row["finished_at"],
            "stats": store.loads(row["stats"], {}), "error": row["error"]}


def runs(limit: int = 20) -> list[dict]:
    with store.connect() as conn:
        return [_run_dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]


# ── sources ──────────────────────────────────────────────────────────────────


def _source_dict(row) -> dict:
    return {"id": row["id"], "kind": row["kind"], "name": row["name"], "enabled": bool(row["enabled"]),
            "status": row["status"], "error": row["error"], "learned_at": row["learned_at"],
            "created_at": row["created_at"], "stats": store.loads(row["stats"], {}),
            "learning": row["id"] in _learning}


def list_sources() -> dict:
    """The sources, and every project that could become one."""
    from services.cowork_agent.project_layout import list_projects

    _recover()
    with store.connect() as conn:
        sources = [_source_dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY name")]
        # Counts read now: "shared" grows when another source learns the same piece.
        for s in sources:
            s["stats"].update(learn.source_stats(conn, s["id"]))
    learned = {s["id"] for s in sources}
    projects = []
    for meta in list_projects():
        pid = meta.get("pid")
        projects.append({"project": meta["name"], "display_name": meta.get("display_name") or meta["name"],
                         "pid": pid if isinstance(pid, str) else None,
                         "source_id": pid if pid in learned else None})
    return {"sources": sources, "projects": projects}


def add_source(project: str) -> dict:
    from services.cowork_agent.project_layout import list_projects

    meta = next((m for m in list_projects() if m.get("name") == project), None)
    if meta is None:
        raise store.BrainError("project_not_found", f"No project folder named {project!r}.", 404)
    pid = meta.get("pid")
    if not isinstance(pid, str) or not SOURCE_ID_RE.fullmatch(pid):
        raise store.BrainError("project_not_ready",
                               f"{project} has no project id yet; open it in Space once so it gets one.", 409)
    with store.write() as conn:
        row = learn.register_source(conn, source_id=pid, kind="project", name=meta["name"],
                                    location=meta["path"], now=now_iso())
    return _source_dict(row)


def remove_source(source_id: str) -> dict:
    source_id = _source_id(source_id)
    if source_id in _learning:
        raise store.BrainError("source_busy", "That source is being learned; try again when it finishes.", 409)
    with store.write() as conn:
        if not conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone():
            raise store.BrainError("source_not_found", f"No source {source_id!r}.", 404)
        return learn.forget_source(conn, source_id, now_iso())


def _start_learning_task(source_id: str, force: bool) -> asyncio.Task:
    task = _spawn(learn.learn_source(source_id, model=model(), budget=config.model_calls_per_learn(), force=force),
                  f"brain learn {source_id}")
    _learning[source_id] = task
    task.add_done_callback(lambda t: _learning.pop(source_id, None) if _learning.get(source_id) is t else None)
    task.add_done_callback(_log_failure)
    return task


async def learn_now(source_id: str, *, force: bool = False) -> dict:
    """Learn one source and wait for it (the loop and tests use this). A run
    already in progress is joined, not repeated."""
    source_id = _source_id(source_id)
    task = _learning.get(source_id) or _start_learning_task(source_id, force)
    return await asyncio.shield(task)


def start_learning(source_id: Optional[str] = None, *, force: bool = False) -> dict:
    """Start learning one source (or every enabled one) in the background."""
    _recover()
    with store.connect() as conn:
        if source_id is not None:
            source_id = _source_id(source_id)
            if not conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone():
                raise store.BrainError("source_not_found", f"No source {source_id!r}; add the project first.", 404)
            ids = [source_id]
        else:
            ids = [r[0] for r in conn.execute("SELECT id FROM sources WHERE enabled = 1 ORDER BY name")]
    started, running = [], []
    for sid in ids:
        if sid in _learning:
            running.append(sid)
        else:
            _start_learning_task(sid, force)
            started.append(sid)
    return {"started": started, "running": running}


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.warning("brain: %s failed: %s", task.get_name(), task.exception())


# ── pieces, relations ────────────────────────────────────────────────────────


def list_pieces(*, q: str = "", source_id: Optional[str] = None, novel: bool = False, shared: bool = False,
                limit: int = 50, offset: int = 0) -> dict:
    where, params = [], []
    if q.strip():
        where.append("(p.name LIKE ? OR p.description LIKE ?)")
        params += [f"%{q.strip()}%"] * 2
    if source_id:
        where.append("p.id IN (SELECT piece_id FROM notes WHERE source_id = ?)")
        params.append(_source_id(source_id))
    if novel:
        where.append("p.novel = 1")
    if shared:
        where.append("(SELECT COUNT(*) FROM notes n WHERE n.piece_id = p.id) > 1")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with store.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM pieces p {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT p.*, (SELECT COUNT(*) FROM notes n WHERE n.piece_id = p.id) AS sources, "
            f"(SELECT COUNT(*) FROM links l WHERE (l.src = p.id OR l.dst = p.id) AND l.kind != 'hypothesis') AS degree "
            f"FROM pieces p {clause} ORDER BY sources DESC, degree DESC, p.name LIMIT ? OFFSET ?",
            (*params, limit, offset)).fetchall()
        items = [{**recall.piece_brief(conn, r["id"]), "sources": r["sources"], "degree": r["degree"]} for r in rows]
    return {"total": total, "items": items}


def _link_dict(conn, row, pid: int) -> dict:
    other = row["dst"] if row["src"] == pid else row["src"]
    name = conn.execute("SELECT name FROM pieces WHERE id = ?", (other,)).fetchone()
    ev = [{"source_id": e["source_id"], "path": e["path"], "start_line": e["start_line"], "end_line": e["end_line"]}
          for e in conn.execute("SELECT c.source_id, c.path, c.start_line, c.end_line FROM link_evidence le "
                                "JOIN chunks c ON c.id = le.chunk_id WHERE le.link_id = ? LIMIT 3", (row["id"],))]
    return {"id": row["id"], "other": {"id": other, "name": name[0] if name else str(other)},
            "direction": "out" if row["src"] == pid else "in", "relation": row["label"], "phrase": row["phrase"],
            "kind": row["kind"], "signal": row["signal"], "weight": row["weight"], "support": row["support"],
            "uses": row["uses"], "explanation": row["explanation"], "evidence": ev}


def get_piece(piece_id: int) -> dict:
    with store.connect() as conn:
        if not conn.execute("SELECT 1 FROM pieces WHERE id = ?", (piece_id,)).fetchone():
            raise store.BrainError("piece_not_found", f"No piece {piece_id}.", 404)
        links = conn.execute(
            "SELECT l.*, r.label FROM links l JOIN relations r ON r.id = l.relation_id "
            "WHERE l.src = ? OR l.dst = ? ORDER BY l.weight DESC LIMIT 80", (piece_id, piece_id)).fetchall()
        facts = [_link_dict(conn, r, piece_id) for r in links if r["kind"] != "hypothesis"]
        guesses = [_link_dict(conn, r, piece_id) for r in links if r["kind"] == "hypothesis"]
        pats = [{"id": r[0], "name": r[1]} for r in conn.execute(
            "SELECT DISTINCT p.id, p.name FROM patterns p JOIN pattern_members m ON m.pattern_id = p.id "
            "WHERE m.piece_id = ?", (piece_id,))]
        return {**recall.piece_brief(conn, piece_id), "notes": recall.notes_for(conn, piece_id),
                "evidence": recall.evidence(conn, piece_id, limit=10), "links": facts, "hypotheses": guesses,
                "patterns": pats}


def relations() -> list[dict]:
    with store.connect() as conn:
        return [{"id": r["id"], "label": r["label"], "phrases": store.loads(r["phrases"], {}), "uses": r["uses"],
                 "links": conn.execute("SELECT COUNT(*) FROM links WHERE relation_id = ?", (r["id"],)).fetchone()[0]}
                for r in conn.execute("SELECT * FROM relations ORDER BY uses DESC")]


# ── recall and use ───────────────────────────────────────────────────────────


async def recall_cue(cue: str, *, context_source: Optional[str] = None, context: Optional[str] = None,
                     limit: int = 10, hops: int = 2, explore: bool = False, reinforce: bool = True,
                     answer: bool = False) -> dict:
    """Recall, and with ``answer`` and a model that reasons, a written answer
    from what was recalled under ``answer`` (``None`` when none was asked
    for or nothing was found; ``{"error": ...}`` when the model failed, the
    recall itself still returned)."""
    cue = (cue or "").strip()
    if not cue or len(cue) > CUE_MAX:
        raise store.BrainError("invalid_value", f"cue is required (1 to {CUE_MAX} chars).")
    if context_source is not None:
        context_source = _source_id(context_source)
    m = model()
    embedding = None
    if m.can_embed:
        try:
            got = await m.embed([cue])
            embedding = [float(x) for x in got[0]] if got else None
        except Exception as exc:  # noqa: BLE001 - TF-IDF still matches
            logger.info("brain: embedding the cue failed: %s", exc)

    wants_answer = answer and m.can_reason

    def run() -> tuple[dict, Optional[dict]]:
        with store.write() as conn:
            result = recall.recall(conn, cue, now=now_iso(), context_source=context_source, context=context,
                                   limit=limit, hops=hops, explore=explore, reinforce=reinforce,
                                   cue_embedding=embedding)
            return result, recall.answer_material(conn, result) if wants_answer else None

    result, material = await asyncio.to_thread(run)
    result["answer"] = None
    if material and material["evidence"]:
        try:
            result["answer"] = {**await reasoning.answer(m, cue, material), "model": m.name,
                                "evidence": material["evidence"]}
        except Exception as exc:  # noqa: BLE001 - the recall stands without an answer
            logger.warning("brain: answering %r failed: %s", cue[:80], exc)
            result["answer"] = {"error": f"{type(exc).__name__}: {exc}"[:500], "model": m.name}
    return result


def use(piece_ids: list[int]) -> dict:
    if not 2 <= len(piece_ids) <= 50:
        raise store.BrainError("invalid_value", "piece_ids needs 2 to 50 ids.")
    with store.write() as conn:
        return {"pairs": recall.use_together(conn, piece_ids, now=now_iso())}


# ── discovery and upkeep ─────────────────────────────────────────────────────


async def discover_now() -> dict:
    global _discover_lock
    if _discover_lock is None:
        _discover_lock = asyncio.Lock()
    if _discover_lock.locked():
        raise store.BrainError("discovery_running", "Discovery is already running.", 409)
    async with _discover_lock:
        return await discover.discover(model())


def fade_now() -> dict:
    with store.write() as conn:
        return graph.fade(conn, now_iso(), config.fade_per_day())


def list_patterns() -> list[dict]:
    with store.connect() as conn:
        names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM sources")}
        return [{"id": r["id"], "name": r["name"], "description": r["description"], "support": r["support"],
                 "score": r["score"], "updated_at": r["updated_at"],
                 "members": {names.get(s, s): ms for s, ms in discover.pattern_members(conn, r["id"]).items()}}
                for r in conn.execute("SELECT * FROM patterns ORDER BY support DESC, score DESC, id")]


def list_analogies() -> list[dict]:
    with store.connect() as conn:
        pname = lambda pid: (conn.execute("SELECT name FROM pieces WHERE id = ?", (pid,)).fetchone() or [str(pid)])[0]  # noqa: E731
        out = []
        for r in conn.execute("SELECT a.*, sa.name AS na, sb.name AS nb, p.name AS pattern FROM analogies a "
                              "JOIN sources sa ON sa.id = a.source_a JOIN sources sb ON sb.id = a.source_b "
                              "JOIN patterns p ON p.id = a.pattern_id ORDER BY a.score DESC"):
            out.append({
                "id": r["id"], "pattern": {"id": r["pattern_id"], "name": r["pattern"]},
                "from": {"id": r["source_a"], "name": r["na"]}, "to": {"id": r["source_b"], "name": r["nb"]},
                "mapping": [{"a": pname(m["a"]), "b": pname(m["b"]), "similarity": m["similarity"]}
                            for m in store.loads(r["mapping"], [])],
                "suggestions": [{"id": s["piece"], "name": pname(s["piece"]), "via": pname(s["via"])}
                                for s in store.loads(r["suggestions"], [])],
                "explanation": r["explanation"], "explained_by": r["explained_by"], "score": r["score"]})
        return out


def list_hypotheses(limit: int = 100) -> list[dict]:
    with store.connect() as conn:
        return [{"id": r["id"], "a": {"id": r["src"], "name": r["na"]}, "b": {"id": r["dst"], "name": r["nb"]},
                 "relation": r["label"], "weight": r["weight"], "explanation": r["explanation"], "kind": "hypothesis"}
                for r in conn.execute(
                    "SELECT l.*, r.label, p1.name AS na, p2.name AS nb FROM links l JOIN relations r ON r.id = l.relation_id "
                    "JOIN pieces p1 ON p1.id = l.src JOIN pieces p2 ON p2.id = l.dst WHERE l.kind = 'hypothesis' "
                    "ORDER BY l.weight DESC LIMIT ?", (limit,))]


def _finding_dict(row) -> dict:
    return {"id": row["id"], "kind": row["kind"], "ref": row["ref"], "title": row["title"], "body": row["body"],
            "source_id": row["source_id"], "count": row["count"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def list_findings(status: str = "open", limit: int = 100) -> list[dict]:
    if status not in ("open", "done", "all"):
        raise store.BrainError("invalid_status", "status must be open, done or all.")
    with store.connect() as conn:
        clause = "" if status == "all" else "WHERE status = ?"
        params: tuple[Any, ...] = (limit,) if status == "all" else (status, limit)
        return [_finding_dict(r) for r in conn.execute(
            f"SELECT * FROM findings {clause} ORDER BY updated_at DESC, id DESC LIMIT ?", params)]


def update_finding(finding_id: int, status: str) -> dict:
    if status not in FINDING_STATUSES:
        raise store.BrainError("invalid_status", "status must be open or done.")
    with store.write() as conn:
        if not conn.execute("UPDATE findings SET status = ?, updated_at = ? WHERE id = ?",
                            (status, now_iso(), finding_id)).rowcount:
            raise store.BrainError("finding_not_found", f"No finding {finding_id}.", 404)
        return _finding_dict(conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone())


def inbox_findings(limit: int = 200) -> list[dict]:
    """Open findings worth a person's attention, for the Inbox feeder: every
    novelty, pattern, analogy and design, and gaps asked about more than once."""
    if not store.db_path().exists():
        return []
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT f.*, s.name AS project FROM findings f LEFT JOIN sources s ON s.id = f.source_id "
            "WHERE f.status = 'open' AND (f.kind != 'gap' OR f.count >= 2) "
            "ORDER BY f.updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [{**_finding_dict(r), "project": r["project"]} for r in rows]


def graph_view(*, source_id: Optional[str] = None, limit: int = 120) -> dict:
    """The strongest part of the graph, for drawing: nodes by degree, the
    fact links between them, hypotheses flagged."""
    with store.connect() as conn:
        params: list[Any] = []
        where = ""
        if source_id:
            where = "WHERE p.id IN (SELECT piece_id FROM notes WHERE source_id = ?)"
            params.append(_source_id(source_id))
        nodes = conn.execute(
            f"SELECT p.id, p.name, p.level, p.novel, (SELECT COUNT(*) FROM notes n WHERE n.piece_id = p.id) AS sources, "
            f"(SELECT COALESCE(SUM(l.weight), 0) FROM links l WHERE l.src = p.id OR l.dst = p.id) AS strength "
            f"FROM pieces p {where} ORDER BY strength DESC, p.id LIMIT ?", (*params, limit)).fetchall()
        ids = [n["id"] for n in nodes]
        marks = ",".join("?" * len(ids)) or "NULL"
        edges = conn.execute(
            f"SELECT l.id, l.src, l.dst, l.weight, l.kind, r.label FROM links l JOIN relations r ON r.id = l.relation_id "
            f"WHERE l.src IN ({marks}) AND l.dst IN ({marks}) ORDER BY l.weight DESC LIMIT ?",
            (*ids, *ids, limit * 4)).fetchall()
        return {"nodes": [{"id": n["id"], "name": n["name"], "level": n["level"], "novel": bool(n["novel"]),
                           "sources": n["sources"], "strength": round(n["strength"], 3)} for n in nodes],
                "links": [{"id": e["id"], "source": e["src"], "target": e["dst"], "weight": e["weight"],
                           "kind": e["kind"], "relation": e["label"]} for e in edges]}


def similar(piece_id: int, limit: int = 10) -> list[dict]:
    with store.connect() as conn:
        vec = vectors.piece_vectors(conn, [piece_id]).get(piece_id)
        if vec is None:
            raise store.BrainError("piece_not_found", f"No piece {piece_id}.", 404)
        return [{**recall.piece_brief(conn, pid), "similarity": round(s, 4)}
                for pid, s in vectors.nearest(conn, vec, limit=limit, exclude=[piece_id])]


# ── create ───────────────────────────────────────────────────────────────────


async def propose(goal: str, *, context_source: Optional[str] = None) -> dict:
    if not isinstance(goal, str) or not goal.strip() or len(goal) > CUE_MAX:
        raise store.BrainError("invalid_value", f"goal is required (1 to {CUE_MAX} chars).")
    if context_source is not None:
        context_source = _source_id(context_source)
    goal_id = await create.propose(model(), goal, context_source=context_source)
    return get_goal(goal_id)


def _design_dict(row) -> dict:
    return {"id": row["id"], "goal_id": row["goal_id"], "title": row["title"], "summary": row["summary"],
            "plan": store.loads(row["plan"], {}), "scores": store.loads(row["scores"], {}), "rank": row["rank"],
            "status": row["status"], "project": row["project"], "build": store.loads(row["build"], {}),
            "building": row["id"] in _building, "updated_at": row["updated_at"]}


def get_goal(goal_id: int) -> dict:
    _recover()
    with store.connect() as conn:
        g = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if g is None:
            raise store.BrainError("goal_not_found", f"No goal {goal_id}.", 404)
        designs = [_design_dict(d) for d in conn.execute(
            "SELECT * FROM designs WHERE goal_id = ? ORDER BY rank", (goal_id,))]
        return {"id": g["id"], "goal": g["text"], "context_source": g["context_source"], "status": g["status"],
                "created_at": g["created_at"], "designs": designs}


def list_goals(limit: int = 30) -> list[dict]:
    with store.connect() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM goals ORDER BY id DESC LIMIT ?", (limit,))]
    return [get_goal(i) for i in ids]


def approve(design_id: int) -> dict:
    with store.write() as conn:
        create.approve(conn, design_id, now_iso())
        return _design_dict(conn.execute("SELECT * FROM designs WHERE id = ?", (design_id,)).fetchone())


def start_build(design_id: int) -> dict:
    _recover()
    if design_id in _building:
        raise store.BrainError("design_building", f"Design {design_id} is already building.", 409)
    with store.connect() as conn:
        row = conn.execute("SELECT status FROM designs WHERE id = ?", (design_id,)).fetchone()
        if row is None:
            raise store.BrainError("design_not_found", f"No design {design_id}.", 404)
        if row["status"] != "approved":
            raise store.BrainError("design_not_approved",
                                   f"Design {design_id} is {row['status']}; approve it before building.", 409)
    task = _spawn(create.build(design_id, model=model()), f"brain build {design_id}")
    _building[design_id] = task
    task.add_done_callback(lambda t: _building.pop(design_id, None))
    task.add_done_callback(_log_failure)
    return {"design_id": design_id, "status": "building"}


def _experience_dict(r) -> dict:
    return {"id": r["id"], "goal": r["goal"], "design_id": r["design_id"], "source_id": r["source_id"],
            "result": r["result"], "lessons": r["lessons"], "pieces": store.loads(r["pieces"], []),
            "patterns": store.loads(r["patterns"], []), "created_at": r["created_at"]}


def list_experiences(limit: int = 50) -> list[dict]:
    with store.connect() as conn:
        return [_experience_dict(r) for r in conn.execute("SELECT * FROM experiences ORDER BY id DESC LIMIT ?",
                                                          (limit,))]


def add_experience(*, goal: str, result: str, lessons: str = "", design_id: Optional[int] = None,
                   source_id: Optional[str] = None, piece_ids: list[int] = (), pattern_ids: list[int] = ()) -> dict:
    if not isinstance(goal, str) or not goal.strip():
        raise store.BrainError("invalid_value", "goal is required.")
    if source_id is not None:
        source_id = _source_id(source_id)
    with store.write() as conn:
        eid = create.record_experience(conn, goal=goal.strip(), result=result, lessons=lessons, design_id=design_id,
                                       source_id=source_id, piece_ids=list(piece_ids), pattern_ids=list(pattern_ids),
                                       now=now_iso())
        return _experience_dict(conn.execute("SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone())

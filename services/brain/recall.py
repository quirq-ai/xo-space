"""Recall (Brain.md §5) and the strengthening that use brings (§6).

Given a cue and an optional context (the active source, a goal):

1. **match**: pieces whose meaning is closest to the cue, by IDF-weighted
   cosine plus how much of the piece's name the cue says (and by embedding
   when the model gives one). The strongest become the seeds.
2. **spread**: activation flows along links in both directions, a neighbour
   receiving ``parent × link weight × decay``, keeping the best path.
3. **context**: a piece the active source has a note for is boosted by that
   note's strength; the rest are halved. A goal boosts pieces close to it.
   Experience scales every piece by what it led to before.
4. **rank and explain**: each result carries its score, its note for the
   context, its evidence and the path that found it.
5. **gaps**: a cue nothing matches well is recorded as missing knowledge.

``final = match × Π(link weight) × decay^hops × context × experience``

Hypothesis links are only walked when ``explore`` is set, and whatever they
reach is returned apart, under ``hypotheses``: a guess is never a fact.

With no context given, a project the cue names ("tell me about forge ai")
becomes the context; a cue that is only that name starts from the project's
most central pieces. :func:`answer_material` packs a result, with numbered
evidence excerpts, for the model that writes an answer from it.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from services.brain import graph, store, vectors
from services.brain import text as tx

SEEDS = 5
MIN_MATCH = 0.12
GAP_BELOW = 0.2
MIN_ACTIVATION = 0.01
REINFORCE_TOP = 3
REINFORCE_AMOUNT = 0.05
USE_AMOUNT = 0.15
ABOUT_SOURCE_MATCH = 0.5   # seed strength when a cue names a whole project


@dataclass
class Step:
    link_id: int
    frm: int
    to: int
    phrase: str
    forward: bool
    kind: str
    weight: float


@dataclass
class Hit:
    activation: float
    match: float
    path: list[Step] = field(default_factory=list)

    @property
    def guess(self) -> bool:
        return any(s.kind == "hypothesis" for s in self.path)


def match(conn: sqlite3.Connection, cue: str, *, embedding: Optional[list[float]] = None,
          limit: int = 20) -> list[tuple[int, float]]:
    tf = vectors.text_vector(cue)
    cue_stems = set(tf)
    found: dict[int, float] = {}
    for pid, cos in vectors.nearest(conn, tf, limit=limit * 3):
        found[pid] = cos
    if embedding:
        for pid, cos in vectors.nearest_embedding(conn, embedding, limit=limit):
            found[pid] = max(found.get(pid, 0.0), cos)
    out = []
    for pid, cos in found.items():
        row = conn.execute("SELECT key FROM pieces WHERE id = ?", (pid,)).fetchone()
        name_stems = {s for s in row["key"].split() if tx.is_content(s)} or set(row["key"].split())
        overlap = len(name_stems & cue_stems) / len(name_stems) if name_stems else 0.0
        out.append((pid, round(0.6 * cos + 0.4 * overlap, 4)))
    out.sort(key=lambda x: (-x[1], x[0]))
    return out[:limit]


def spread(conn: sqlite3.Connection, seeds: list[tuple[int, float]], *, hops: int, decay: float,
           explore: bool) -> dict[int, Hit]:
    kinds = store.LINK_KINDS if explore else store.FACT_KINDS
    marks = ",".join("?" * len(kinds))
    hits: dict[int, Hit] = {pid: Hit(score, score) for pid, score in seeds}
    frontier = dict(hits)
    for _ in range(hops):
        nxt: dict[int, Hit] = {}
        for pid, hit in frontier.items():
            rows = conn.execute(
                f"SELECT l.id, l.src, l.dst, l.weight, l.kind, r.label FROM links l "
                f"JOIN relations r ON r.id = l.relation_id WHERE (l.src = ? OR l.dst = ?) AND l.kind IN ({marks})",
                (pid, pid, *kinds)).fetchall()
            for row in rows:
                other = row["dst"] if row["src"] == pid else row["src"]
                value = hit.activation * row["weight"] * decay
                if value < MIN_ACTIVATION:
                    continue
                current = hits.get(other)
                if current is not None and current.activation >= value:
                    continue
                step = Step(row["id"], pid, other, row["label"], row["src"] == pid, row["kind"], row["weight"])
                new = Hit(value, current.match if current else 0.0, hit.path + [step])
                hits[other] = new
                nxt[other] = new
        frontier = nxt
        if not frontier:
            break
    return hits


def context_factor(conn: sqlite3.Connection, pid: int, *, source: Optional[str],
                   goal_vec: Optional[dict[str, float]], note: Optional[sqlite3.Row]) -> tuple[float, list[str]]:
    factor, why = 1.0, []
    if source:
        if note is not None:
            factor *= 1.0 + note["strength"]
            why.append("boosted by the active source")
        else:
            factor *= 0.5
            why.append("not used by the active source")
    if goal_vec:
        pvec = vectors.piece_vectors(conn, [pid]).get(pid, {})
        sim = tx.cosine(vectors.weighted(conn, goal_vec), vectors.weighted(conn, pvec))
        factor *= 0.5 + min(1.0, sim * 2)
        if sim > 0.25:
            why.append("close to the goal")
    score = conn.execute("SELECT score FROM pieces WHERE id = ?", (pid,)).fetchone()[0]
    if score:
        factor *= 1.0 + 0.25 * math.tanh(score)
        why.append("worked before" if score > 0 else "failed before")
    return factor, why


def evidence(conn: sqlite3.Connection, pid: int, *, source: Optional[str] = None, limit: int = 3) -> list[dict]:
    rows = conn.execute(
        "SELECT c.source_id, s.name AS source_name, c.path, c.start_line, c.end_line, m.usage, c.extractor "
        "FROM mentions m JOIN chunks c ON c.id = m.chunk_id JOIN sources s ON s.id = c.source_id "
        "WHERE m.piece_id = ? ORDER BY (c.source_id = ?) DESC, m.defines DESC, m.weight DESC LIMIT ?",
        (pid, source or "", limit)).fetchall()
    return [{"source_id": r["source_id"], "source": r["source_name"], "path": r["path"],
             "start_line": r["start_line"], "end_line": r["end_line"], "quote": r["usage"],
             "extractor": r["extractor"]} for r in rows]


def piece_brief(conn: sqlite3.Connection, pid: int) -> dict:
    row = conn.execute("SELECT id, name, description, level, keywords, novel, score FROM pieces WHERE id = ?",
                       (pid,)).fetchone()
    return {"id": row["id"], "name": row["name"], "description": row["description"], "level": row["level"],
            "keywords": store.loads(row["keywords"], []), "novel": bool(row["novel"]), "score": row["score"]}


def notes_for(conn: sqlite3.Connection, pid: int) -> list[dict]:
    return [{"source_id": r["source_id"], "source": r["name"], "note": r["note"], "strength": r["strength"],
             "mentions": r["mentions"], "origin": r["origin"]}
            for r in conn.execute(
                "SELECT n.*, s.name FROM notes n JOIN sources s ON s.id = n.source_id WHERE n.piece_id = ? "
                "ORDER BY n.strength DESC", (pid,))]


def recall(conn: sqlite3.Connection, cue: str, *, now: str, context_source: Optional[str] = None,
           context: Optional[str] = None, limit: int = 10, hops: int = 2, decay: float = 0.5,
           explore: bool = False, reinforce: bool = True,
           cue_embedding: Optional[list[float]] = None) -> dict:
    cue = cue.strip()
    matched = match(conn, cue, embedding=cue_embedding)
    best = matched[0][1] if matched else 0.0
    seeds = [(pid, s) for pid, s in matched[:SEEDS] if s >= MIN_MATCH and s >= best * 0.4]
    source_name = None
    detected = about_source = False
    if context_source:
        row = conn.execute("SELECT name FROM sources WHERE id = ?", (context_source,)).fetchone()
        if row is None:
            raise store.BrainError("source_not_found", f"No source {context_source!r} to use as context.", 404)
        source_name = row["name"]
    else:
        named = mentioned_source(conn, cue)
        if named is not None:
            context_source, source_name, name_terms = named
            detected = True
            # "tell me about forge ai": the cue is the project itself. What its
            # name matches is kept; with nothing matched, start from what its
            # overview files talk about.
            if not set(tx.terms(cue)) - name_terms:
                about_source = True
                if not seeds:
                    seeds = overview_pieces(conn, context_source)
                    best = max(best, ABOUT_SOURCE_MATCH) if seeds else best

    gap = best < GAP_BELOW
    if gap and cue:
        store.upsert_finding(conn, kind="gap", ref=f"{tx.phrase_key(cue)[:200]}|{context_source or ''}",
                             title=f"Missing knowledge: {cue[:200]}",
                             body=f"Nothing known matched this cue well (best match {best:.2f}).",
                             source_id=context_source, now=now)
    hits = spread(conn, seeds, hops=hops, decay=decay, explore=explore) if seeds else {}
    goal_vec = vectors.text_vector(context) if context else None
    ids = list(hits)
    names = {r[0]: r[1] for r in conn.execute(
        f"SELECT id, name FROM pieces WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}

    facts, guesses = [], []
    for pid, hit in hits.items():
        note = conn.execute("SELECT * FROM notes WHERE piece_id = ? AND source_id = ?",
                            (pid, context_source)).fetchone() if context_source else None
        factor, why = context_factor(conn, pid, source=context_source, goal_vec=goal_vec, note=note)
        seed = hit.path[0].frm if hit.path else pid
        seed_match = next((s for p, s in seeds if p == seed), hit.match)
        item = {
            "piece": piece_brief(conn, pid),
            "score": round(hit.activation * factor, 4),
            "match": round(seed_match if not hit.path else 0.0, 4),
            "hops": len(hit.path),
            "path": [{"from": s.frm, "to": s.to, "relation": s.phrase, "forward": s.forward, "kind": s.kind,
                      "weight": s.weight, "link_id": s.link_id} for s in hit.path],
            "explanation": _explain(names, seed, seed_match, hit, why, source_name),
            "note": _note_dict(note),
            "notes": notes_for(conn, pid),
            "evidence": evidence(conn, pid, source=context_source),
        }
        (guesses if hit.guess else facts).append(item)
    facts.sort(key=lambda x: (-x["score"], x["piece"]["id"]))
    guesses.sort(key=lambda x: (-x["score"], x["piece"]["id"]))
    facts, guesses = facts[:limit], guesses[:limit]

    if reinforce and facts:
        top = [f["piece"]["id"] for f in facts[:REINFORCE_TOP]]
        for i, a in enumerate(top):
            for b in top[i + 1:]:
                graph.strengthen(conn, a, b, amount=REINFORCE_AMOUNT, now=now)
        graph.touch(conn, [s["link_id"] for f in facts for s in f["path"]], now)

    return {"cue": cue, "context_source": context_source, "context_name": source_name,
            "context_detected": detected, "about_source": about_source, "context": context, "gap": gap,
            "best_match": round(best, 4), "results": facts, "hypotheses": guesses}


def mentioned_source(conn: sqlite3.Connection, cue: str) -> Optional[tuple[str, str, set[str]]]:
    """The source a cue names: every content word of its name appears in the
    cue ("forge ai" names FORGE-AI-0.3). The longest such name wins."""
    cue_terms = set(tx.terms(cue))
    best: Optional[tuple[str, str, set[str]]] = None
    for row in conn.execute("SELECT id, name FROM sources WHERE enabled = 1"):
        # Folder names join words with - _ . and versions: FORGE-AI-0.3 → forge, ai.
        name_terms = set(tx.terms(re.sub(r"[^A-Za-z]+", " ", row["name"])))
        if name_terms and name_terms <= cue_terms and (best is None or len(name_terms) > len(best[2])):
            best = (row["id"], row["name"], name_terms)
    return best


def overview_pieces(conn: sqlite3.Connection, source_id: str, limit: int = SEEDS) -> list[tuple[int, float]]:
    """What a source says it is about: the strongest pieces of its overview
    files (README, package metadata, entry point). A source's best-connected
    pieces are no substitute: in code they are words like "list" or "field"."""
    ids = [r["id"] for r in overview_chunks(conn, source_id)]
    if not ids:
        return []
    rows = conn.execute(
        f"SELECT piece_id, MAX(weight) AS w FROM mentions WHERE chunk_id IN ({','.join('?' * len(ids))}) "
        "GROUP BY piece_id ORDER BY w DESC, piece_id LIMIT ?", (*ids, limit)).fetchall()
    return [(r["piece_id"], ABOUT_SOURCE_MATCH) for r in rows]


# What a file name says about how much of a project it describes.
_OVERVIEW_ORDER = (("readme", 0), (".md", 1), ("pyproject.toml", 2), ("package.json", 2), ("cargo.toml", 2),
                   ("go.mod", 2), ("setup.py", 2), ("main.", 3), ("app.", 3), ("index.", 3), ("__init__.py", 4))


def overview_chunks(conn: sqlite3.Connection, source_id: str, limit: int = 3) -> list[sqlite3.Row]:
    """The opening chunk of a source's top-level overview files (README,
    package metadata, entry point), best first."""
    def rank(path: str) -> int:
        low = path.lower()
        return next((r for key, r in _OVERVIEW_ORDER if key in low), 9)

    firsts: dict[str, sqlite3.Row] = {}
    for row in conn.execute("SELECT id, source_id, path, start_line, end_line, text FROM chunks "
                            "WHERE source_id = ? AND path NOT LIKE '%/%' ORDER BY path, start_line", (source_id,)):
        firsts.setdefault(row["path"], row)
    ordered = sorted(firsts.values(), key=lambda r: (rank(r["path"]), r["path"]))
    return [r for r in ordered if rank(r["path"]) < 9][:limit]


def answer_material(conn: sqlite3.Connection, result: dict, *, pieces: int = 8, evidence_max: int = 12,
                    excerpt_chars: int = 700) -> dict:
    """What a model needs to answer from a recall: the top pieces, how each
    was found and used, and numbered evidence excerpts to cite. Overview
    files come first when the cue named a whole project."""
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM sources")}
    numbered: list[dict] = []
    seen: dict[int, int] = {}

    def add(row: sqlite3.Row) -> int:
        if row["id"] not in seen and len(numbered) < evidence_max:
            seen[row["id"]] = len(numbered) + 1
            numbered.append({"n": seen[row["id"]], "source": names.get(row["source_id"], row["source_id"]),
                             "source_id": row["source_id"], "path": row["path"], "start_line": row["start_line"],
                             "end_line": row["end_line"], "excerpt": row["text"][:excerpt_chars]})
        return seen.get(row["id"], 0)

    context = result.get("context_source")
    if result.get("about_source") and context:
        for row in overview_chunks(conn, context):
            add(row)
    items = []
    for r in result["results"][:pieces]:
        pid = r["piece"]["id"]
        rows = conn.execute(
            "SELECT c.id, c.source_id, c.path, c.start_line, c.end_line, c.text FROM mentions m "
            "JOIN chunks c ON c.id = m.chunk_id WHERE m.piece_id = ? "
            "ORDER BY (c.source_id = ?) DESC, m.defines DESC, m.weight DESC LIMIT 2", (pid, context or "")).fetchall()
        refs = [n for n in (add(row) for row in rows) if n]
        items.append({"name": r["piece"]["name"], "description": r["piece"]["description"][:300],
                      "how_found": r["explanation"][:300],
                      "used_by": [f"{n['source']}: {n['note'][:200]}" for n in r["notes"][:3]],
                      "evidence": refs})
    guesses = [{"name": g["piece"]["name"], "why": g["explanation"][:200]} for g in result["hypotheses"][:5]]
    return {"context": result.get("context_name"), "pieces": items, "guesses": guesses, "evidence": numbered}


def _explain(names: dict[int, str], seed: int, seed_match: float, hit: Hit, why: list[str],
             source_name: Optional[str]) -> str:
    parts = [f"cue matched {names.get(seed, seed)} ({seed_match:.2f})"]
    for s in hit.path:
        a, b = names.get(s.frm, s.frm), names.get(s.to, s.to)
        parts.append(f"{a} {s.phrase} {b}" if s.forward else f"{b} {s.phrase} {a}")
        if s.kind == "hypothesis":
            parts[-1] += " (hypothesis)"
    for w in why:
        parts.append(w.replace("the active source", f"the active source ({source_name})") if source_name else w)
    return " → ".join(parts)


def _note_dict(note: Optional[sqlite3.Row]) -> Optional[dict]:
    if note is None:
        return None
    return {"source_id": note["source_id"], "note": note["note"], "strength": note["strength"],
            "mentions": note["mentions"], "origin": note["origin"]}


def use_together(conn: sqlite3.Connection, piece_ids: list[int], *, now: str) -> int:
    """Pieces used together (a person or an agent says so): every pair's
    links grow by :data:`USE_AMOUNT`, a missing link is created as learned."""
    ids = list(dict.fromkeys(piece_ids))
    known = {r[0] for r in conn.execute(
        f"SELECT id FROM pieces WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}
    missing = [i for i in ids if i not in known]
    if missing:
        raise store.BrainError("piece_not_found", f"No piece with id {missing[0]}.", 404)
    pairs = 0
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            graph.strengthen(conn, a, b, amount=USE_AMOUNT, now=now, phrase=graph.USED_TOGETHER)
            pairs += 1
    return pairs

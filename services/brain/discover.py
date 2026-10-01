"""Discover (Brain.md §7): recurring patterns, missing links, analogies.

**Patterns.** In every source, each well-connected piece and its strongest
neighbours form a candidate group. Its signature is the sum of its members'
meaning vectors, so it describes what the group is *about* rather than what
its members are called. Groups from different sources whose signatures are
close are the same pattern, even when every member is named differently;
matching groups are merged, and a pattern exists once it appears in two or
more sources. A pattern keeps its id (and what experience taught about it)
while its members still overlap what was found before.

**Missing links.** Two pieces that are not linked but share neighbours
(Adamic–Adar: a rare shared neighbour counts more than a hub) and mean
similar things probably relate. They are stored as ``hypothesis`` links,
explained by the neighbours that suggested them, walked only when a recall
explores, and removed once real evidence links the pair. A model, when
connected, names the relation and drops the implausible ones.

**Analogies.** For each pattern and each pair of its sources: which member
corresponds to which, and what the first source connects to the pattern that
the second has no counterpart for, "B could do this the way A does".

Novelty is flagged while learning (:mod:`services.brain.learn`).
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from services.brain import graph, learn, reasoning, store, vectors
from services.brain import text as tx
from services.brain.model import BrainModel, ModelUnavailable
from services.timestamps import now_iso

logger = logging.getLogger(__name__)

EGO_NEIGHBOURS = 5
EGOS_PER_SOURCE = 150
LINK_FLOOR = 0.2
PATTERN_SIMILARITY = 0.35
PATTERN_OVERLAP = 0.5
HYPOTHESES_PER_RUN = 20
HYPOTHESES_KEPT = 200
HUB_DEGREE = 60
MAPPING_FLOOR = 0.2
COUNTERPART = 0.5
MODEL_EXPLANATIONS = 5


# ── shared graph reads ───────────────────────────────────────────────────────


def _source_graph(conn: sqlite3.Connection, source_id: str) -> dict[int, dict[int, float]]:
    """Fact links between pieces this source uses, with evidence in it
    (learned and generated links count too: use is evidence of a kind)."""
    members = {r[0] for r in conn.execute("SELECT piece_id FROM notes WHERE source_id = ?", (source_id,))}
    adj: dict[int, dict[int, float]] = defaultdict(dict)
    rows = conn.execute(
        "SELECT l.id, l.src, l.dst, l.weight, l.kind FROM links l WHERE l.kind IN ('evidence','learned','generated') "
        "AND l.weight >= ?", (LINK_FLOOR,)).fetchall()
    for row in rows:
        if row["src"] not in members or row["dst"] not in members:
            continue
        if row["kind"] == "evidence" and not conn.execute(
                "SELECT 1 FROM link_evidence e JOIN chunks c ON c.id = e.chunk_id WHERE e.link_id = ? "
                "AND c.source_id = ? LIMIT 1", (row["id"], source_id)).fetchone():
            continue
        a, b, w = row["src"], row["dst"], row["weight"]
        adj[a][b] = max(adj[a].get(b, 0.0), w)
        adj[b][a] = max(adj[b].get(a, 0.0), w)
    return adj


def _fact_graph(conn: sqlite3.Connection) -> dict[int, dict[int, float]]:
    adj: dict[int, dict[int, float]] = defaultdict(dict)
    for row in conn.execute("SELECT src, dst, weight FROM links WHERE kind IN ('evidence','learned','generated') "
                            "AND weight >= ?", (LINK_FLOOR,)):
        a, b, w = row
        adj[a][b] = max(adj[a].get(b, 0.0), w)
        adj[b][a] = max(adj[b].get(a, 0.0), w)
    return adj


# ── patterns ─────────────────────────────────────────────────────────────────


def _groups(conn: sqlite3.Connection, source_id: str, n_docs: int, df_cache: dict) -> list[dict]:
    adj = _source_graph(conn, source_id)
    ranked = sorted(adj, key=lambda p: (-sum(adj[p].values()), p))[:EGOS_PER_SOURCE]
    groups = []
    for pid in ranked:
        near = sorted(adj[pid].items(), key=lambda kv: (-kv[1], kv[0]))[:EGO_NEIGHBOURS]
        if len(near) < 2:
            continue
        members = {pid, *(q for q, _ in near)}
        vecs = vectors.piece_vectors(conn, members)
        sig: Counter = Counter()
        for vec in vecs.values():
            missing = [t for t in vec if t not in df_cache]
            if missing:
                df_cache.update(vectors.dfs(conn, missing))
            weighted = tx.weigh(vec, df_cache, n_docs)
            norm = math.sqrt(sum(w * w for w in weighted.values())) or 1.0
            for t, w in weighted.items():
                sig[t] += w / norm
        groups.append({"source": source_id, "hub": pid, "members": members, "sig": dict(sig),
                       "top": set(tx.top_terms(dict(sig), 10))})
    return groups


def find_patterns(conn: sqlite3.Connection, now: str) -> list[int]:
    """Recompute cross-source patterns. Returns the ids of new patterns."""
    sources = [r[0] for r in conn.execute("SELECT id FROM sources WHERE enabled = 1 ORDER BY id")]
    n_docs = store.chunk_count(conn)
    df_cache: dict[str, int] = {}
    groups = [g for s in sources for g in _groups(conn, s, n_docs, df_cache)]

    by_term: dict[str, list[int]] = defaultdict(list)
    for i, g in enumerate(groups):
        for t in g["top"]:
            by_term[t].append(i)
    parent = list(range(len(groups)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    matched: set[int] = set()
    for i, g in enumerate(groups):
        shared_terms: Counter = Counter(j for t in g["top"] for j in by_term[t] if j > i)
        for j, n in shared_terms.items():
            h = groups[j]
            if h["source"] == g["source"]:
                continue
            same = len(g["members"] & h["members"]) / len(g["members"] | h["members"])
            if n < 2 and same == 0:
                continue
            sim = 0.8 * tx.cosine(g["sig"], h["sig"]) + 0.2 * same
            if sim >= PATTERN_SIMILARITY:
                parent[find(i)] = find(j)
                matched |= {i, j}

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in matched:
        clusters[find(i)].append(i)
    found: list[dict] = []
    for idxs in clusters.values():
        per_source: dict[str, set[int]] = defaultdict(set)
        sig: Counter = Counter()
        for i in idxs:
            per_source[groups[i]["source"]] |= groups[i]["members"]
            sig.update(groups[i]["sig"])
        if len(per_source) < 2:
            continue
        found.append({"members": per_source, "sig": dict(sig)})
    found = _dedupe(found)
    return _store_patterns(conn, found, now)


def _pairs(members: dict[str, set[int]]) -> set[tuple[str, int]]:
    return {(s, p) for s, ps in members.items() for p in ps}


def _dedupe(found: list[dict]) -> list[dict]:
    out: list[dict] = []
    for f in sorted(found, key=lambda f: -len(_pairs(f["members"]))):
        pairs = _pairs(f["members"])
        if any(tx.jaccard(pairs, _pairs(o["members"])) >= PATTERN_OVERLAP for o in out):
            continue
        out.append(f)
    return out


def _store_patterns(conn: sqlite3.Connection, found: list[dict], now: str) -> list[int]:
    existing: dict[int, set[tuple[str, int]]] = defaultdict(set)
    for row in conn.execute("SELECT pattern_id, source_id, piece_id FROM pattern_members"):
        existing[row[0]].add((row[1], row[2]))
    for row in conn.execute("SELECT id FROM patterns"):
        existing.setdefault(row[0], set())
    kept, new = set(), []
    for f in found:
        pairs = _pairs(f["members"])
        best = max(((pid, tx.jaccard(pairs, old)) for pid, old in existing.items() if pid not in kept),
                   key=lambda x: x[1], default=(None, 0.0))
        name = " · ".join(tx.top_terms(f["sig"], 3))
        if best[0] is not None and best[1] >= PATTERN_OVERLAP:
            pid = best[0]
            conn.execute("UPDATE patterns SET support = ?, updated_at = ? WHERE id = ?",
                         (len(f["members"]), now, pid))
            conn.execute("UPDATE patterns SET name = ? WHERE id = ? AND description = ''", (name, pid))
            conn.execute("DELETE FROM pattern_members WHERE pattern_id = ?", (pid,))
        else:
            pid = int(conn.execute("INSERT INTO patterns(name, support, created_at, updated_at) VALUES (?,?,?,?)",
                                   (name, len(f["members"]), now, now)).lastrowid)
            new.append(pid)
        kept.add(pid)
        conn.executemany("INSERT OR IGNORE INTO pattern_members(pattern_id, source_id, piece_id) VALUES (?,?,?)",
                         [(pid, s, p) for s, p in pairs])
    for pid in existing:
        if pid not in kept:
            conn.execute("DELETE FROM patterns WHERE id = ? AND score = 0", (pid,))
    for pid in new:
        names = _source_names(conn, pid)
        row = conn.execute("SELECT name FROM patterns WHERE id = ?", (pid,)).fetchone()
        store.upsert_finding(conn, kind="pattern", ref=str(pid), title=f"Recurring pattern: {row[0]}",
                             body=f"Appears in {', '.join(names)}.", now=now, reopen=False)
    return new


def _source_names(conn: sqlite3.Connection, pattern_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT s.name FROM pattern_members m JOIN sources s ON s.id = m.source_id "
        "WHERE m.pattern_id = ? ORDER BY s.name", (pattern_id,))]


def pattern_members(conn: sqlite3.Connection, pattern_id: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for row in conn.execute(
            "SELECT m.source_id, p.id, p.name FROM pattern_members m JOIN pieces p ON p.id = m.piece_id "
            "WHERE m.pattern_id = ? ORDER BY m.source_id, p.name", (pattern_id,)):
        out[row[0]].append({"id": row[1], "name": row[2]})
    return dict(out)


# ── missing links ────────────────────────────────────────────────────────────


def predict_links(conn: sqlite3.Connection, now: str) -> list[dict]:
    """Candidate missing links, best first; nothing is written here."""
    adj = _fact_graph(conn)
    deg = {p: len(n) for p, n in adj.items()}
    scores: dict[tuple[int, int], float] = defaultdict(float)
    via: dict[tuple[int, int], list[int]] = defaultdict(list)
    for n, neigh in adj.items():
        if deg[n] < 2 or deg[n] > HUB_DEGREE:
            continue
        ids = sorted(neigh)
        bonus = 1.0 / math.log(deg[n] + 1)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if b in adj[a]:
                    continue
                scores[(a, b)] += bonus
                via[(a, b)].append(n)
    candidates = [(pair, s) for pair, s in scores.items() if len(via[pair]) >= 2]
    candidates.sort(key=lambda x: (-x[1], x[0]))
    candidates = candidates[:HYPOTHESES_PER_RUN * 3]
    vecs = vectors.piece_vectors(conn, {p for pair, _ in candidates for p in pair})
    n_docs = store.chunk_count(conn)
    df_all = vectors.dfs(conn, {t for v in vecs.values() for t in v})
    out = []
    for (a, b), s in candidates:
        sim = tx.cosine(tx.weigh(vecs.get(a, {}), df_all, n_docs), tx.weigh(vecs.get(b, {}), df_all, n_docs))
        out.append({"a": a, "b": b, "score": round(s + sim, 4), "via": via[(a, b)][:4]})
    out.sort(key=lambda x: (-x["score"], x["a"], x["b"]))
    return out[:HYPOTHESES_PER_RUN]


def store_hypotheses(conn: sqlite3.Connection, predicted: list[dict], judged: dict[int, tuple[str, float]],
                     now: str) -> int:
    names = lambda ids: [r[0] for r in conn.execute(  # noqa: E731
        f"SELECT name FROM pieces WHERE id IN ({','.join('?' * len(ids))})", ids)]
    written = 0
    top = max((p["score"] for p in predicted), default=1.0) or 1.0
    for i, p in enumerate(predicted):
        phrase, weight = graph.MAY_RELATE, round(0.45 * p["score"] / top, 4)
        if judged:
            if i not in judged or judged[i][1] < 0.5 or not judged[i][0]:
                continue
            phrase, weight = judged[i][0], round(0.5 * judged[i][1], 4)
        graph.upsert_link(conn, p["a"], p["b"], phrase, kind="hypothesis", signal="predicted", now=now,
                          weight=max(0.05, weight),
                          explanation="shares neighbours: " + ", ".join(names(p["via"])))
        written += 1
    # A hypothesis that evidence now supports is no longer a guess.
    conn.execute(
        "DELETE FROM links WHERE kind = 'hypothesis' AND EXISTS (SELECT 1 FROM links f WHERE f.kind = 'evidence' "
        "AND ((f.src = links.src AND f.dst = links.dst) OR (f.src = links.dst AND f.dst = links.src)))")
    conn.execute(
        "DELETE FROM links WHERE kind = 'hypothesis' AND id NOT IN (SELECT id FROM links WHERE kind = 'hypothesis' "
        f"ORDER BY weight DESC LIMIT {HYPOTHESES_KEPT})")
    return written


# ── analogies ────────────────────────────────────────────────────────────────


def find_analogies(conn: sqlite3.Connection, now: str) -> list[int]:
    """Read every pattern across each pair of its sources. Returns the ids
    of analogies that suggest something."""
    n_docs = store.chunk_count(conn)
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM sources")}
    fresh: list[int] = []
    seen: set[tuple[int, str, str]] = set()
    for (pattern_id,) in conn.execute("SELECT id FROM patterns").fetchall():
        members: dict[str, set[int]] = defaultdict(set)
        for row in conn.execute("SELECT source_id, piece_id FROM pattern_members WHERE pattern_id = ?", (pattern_id,)):
            members[row[0]].add(row[1])
        for a in members:
            for b in members:
                if a == b:
                    continue
                found = _analogy(conn, members[a], members[b], a, b, n_docs)
                if found is None:
                    continue
                mapping, suggestions, score = found
                text = _statistical_explanation(conn, names[a], names[b], mapping, suggestions)
                prior = conn.execute("SELECT id, explained_by FROM analogies WHERE pattern_id = ? AND source_a = ? "
                                     "AND source_b = ?", (pattern_id, a, b)).fetchone()
                if prior is None:
                    aid = int(conn.execute(
                        "INSERT INTO analogies(pattern_id, source_a, source_b, mapping, suggestions, explanation, "
                        "score, created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (pattern_id, a, b, store.dumps(mapping), store.dumps(suggestions), text, score, now)).lastrowid)
                    fresh.append(aid)
                else:
                    aid = prior["id"]
                    conn.execute(
                        "UPDATE analogies SET mapping = ?, suggestions = ?, score = ?, "
                        "explanation = CASE WHEN explained_by = 'model' THEN explanation ELSE ? END WHERE id = ?",
                        (store.dumps(mapping), store.dumps(suggestions), score, text, aid))
                seen.add((pattern_id, a, b))
                if suggestions:
                    store.upsert_finding(
                        conn, kind="analogy", ref=f"{pattern_id}:{a}:{b}",
                        title=f"{names[b]} could do this the way {names[a]} does",
                        body=text, source_id=b, now=now, reopen=False)
    for row in conn.execute("SELECT id, pattern_id, source_a, source_b FROM analogies").fetchall():
        if (row[1], row[2], row[3]) not in seen:
            conn.execute("DELETE FROM analogies WHERE id = ?", (row[0],))
    return fresh


def _analogy(conn: sqlite3.Connection, a_members: set[int], b_members: set[int], a: str, b: str,
             n_docs: int) -> Optional[tuple[list[dict], list[dict], float]]:
    vecs = vectors.piece_vectors(conn, a_members | b_members)
    df_all = vectors.dfs(conn, {t for v in vecs.values() for t in v})
    weighted = {p: tx.weigh(v, df_all, n_docs) for p, v in vecs.items()}
    pairs = sorted(((1.0 if x == y else tx.cosine(weighted[x], weighted[y]), x, y)
                    for x in a_members for y in b_members), reverse=True)
    used_a, used_b, mapping = set(), set(), []
    for sim, x, y in pairs:
        if sim < MAPPING_FLOOR or x in used_a or y in used_b:
            continue
        used_a.add(x)
        used_b.add(y)
        mapping.append({"a": x, "b": y, "similarity": round(sim, 4)})
    if not mapping:
        return None
    # What A has around the shared structure that B has no counterpart for:
    # pattern members left unmapped, then A's neighbours of mapped members.
    adj = _source_graph(conn, a)
    b_pieces = {r[0] for r in conn.execute("SELECT piece_id FROM notes WHERE source_id = ?", (b,))}
    b_vecs = {p: tx.weigh(v, df_all, n_docs) for p, v in vectors.piece_vectors(conn, b_pieces).items()}
    mapped_a = {m["a"] for m in mapping}
    candidates: list[tuple[int, int, float]] = []
    for x in sorted(a_members - mapped_a):
        via = max(((m, adj.get(x, {}).get(m, 0.0)) for m in mapped_a), key=lambda kv: kv[1])
        candidates.append((x, via[0], via[1]))
    for m in mapping:
        for x, w in sorted(adj.get(m["a"], {}).items(), key=lambda kv: (-kv[1], kv[0])):
            if x not in a_members:
                candidates.append((x, m["a"], w))
    own_names = learn.source_name_keys(conn)
    suggestions, offered = [], set()
    for x, via, w in candidates:
        if x in offered or x in b_pieces:
            continue
        if conn.execute("SELECT key FROM pieces WHERE id = ?", (x,)).fetchone()[0] in own_names:
            continue
        xv = tx.weigh(vectors.piece_vectors(conn, [x]).get(x, {}), df_all, n_docs)
        if any(tx.cosine(xv, v) >= COUNTERPART for v in b_vecs.values()):
            continue
        offered.add(x)
        suggestions.append({"piece": x, "via": via, "weight": round(w, 4)})
        if len(suggestions) >= 5:
            break
    score = sum(m["similarity"] for m in mapping) / len(mapping) * min(1.0, 0.4 + 0.2 * len(suggestions))
    return mapping, suggestions, round(score, 4)


def _statistical_explanation(conn: sqlite3.Connection, a: str, b: str, mapping: list[dict],
                             suggestions: list[dict]) -> str:
    name = lambda pid: conn.execute("SELECT name FROM pieces WHERE id = ?", (pid,)).fetchone()[0]  # noqa: E731
    # Correspondences under different names say more than shared pieces.
    ordered = sorted(mapping, key=lambda m: (m["a"] == m["b"], -m["similarity"]))
    pairs = [f"{name(m['b'])} ↔ {name(m['a'])}" if m["a"] != m["b"] else name(m["a"]) for m in ordered[:4]]
    text = f"{b} and {a} share a structure ({'; '.join(pairs)})."
    if suggestions:
        extra = ", ".join(f"{name(s['piece'])} (with {name(s['via'])})" for s in suggestions[:3])
        text += f" {a} also connects {extra}; {b} has no counterpart yet."
    return text


# ── the run ──────────────────────────────────────────────────────────────────


async def discover(model: BrainModel, *, db: Optional[Path] = None) -> dict:
    now = now_iso()

    def structural() -> tuple[int, list[int], list[dict]]:
        with store.write(db) as conn:
            run_id = int(conn.execute("INSERT INTO runs(kind, status, started_at) VALUES ('discover', 'running', ?)",
                                      (now,)).lastrowid)
            new_patterns = find_patterns(conn, now)
            predicted = predict_links(conn, now)
            return run_id, new_patterns, [dict(p, a_name=_name(conn, p["a"]), b_name=_name(conn, p["b"]),
                                               via_names=[_name(conn, v) for v in p["via"]]) for p in predicted]

    run_id, new_patterns, predicted = await asyncio.to_thread(structural)
    errors: list[str] = []
    judged: dict[int, tuple[str, float]] = {}
    named: dict[int, tuple[str, str]] = {}
    if model.can_reason:
        try:
            if predicted:
                judged = await reasoning.judge_hypotheses(
                    model, [{"a": p["a_name"], "b": p["b_name"], "via": p["via_names"]} for p in predicted])
            for pid in new_patterns[:MODEL_EXPLANATIONS]:
                members = await asyncio.to_thread(_member_names, db, pid)
                named[pid] = await reasoning.name_pattern(model, members)
            await graph.consolidate_relations(db, model)
        except (ModelUnavailable, reasoning.BadAnswer) as exc:
            errors.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - discovery keeps its statistical results
            errors.append(f"{type(exc).__name__}: {exc}")

    def finish() -> tuple[dict, list[int]]:
        with store.write(db) as conn:
            for pid, (name, description) in named.items():
                if name:
                    conn.execute("UPDATE patterns SET name = ?, description = ? WHERE id = ?", (name, description, pid))
                    conn.execute("UPDATE findings SET title = ? WHERE kind = 'pattern' AND ref = ?",
                                 (f"Recurring pattern: {name}", str(pid)))
            hypotheses = store_hypotheses(conn, predicted, judged, now)
            analogies = find_analogies(conn, now)
            stats = {"patterns": conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0],
                     "patterns_new": len(new_patterns), "hypotheses_written": hypotheses,
                     "analogies": conn.execute("SELECT COUNT(*) FROM analogies").fetchone()[0],
                     "analogies_new": len(analogies), "model_errors": errors[:5]}
            conn.execute("UPDATE runs SET status = 'done', finished_at = ?, stats = ? WHERE id = ?",
                         (now_iso(), store.dumps(stats), run_id))
            return stats, analogies

    stats, fresh = await asyncio.to_thread(finish)
    if model.can_reason and fresh:
        await _explain_analogies(db, model, fresh[:MODEL_EXPLANATIONS], errors)
    return stats


def _name(conn: sqlite3.Connection, pid: int) -> str:
    row = conn.execute("SELECT name FROM pieces WHERE id = ?", (pid,)).fetchone()
    return row[0] if row else str(pid)


def _member_names(db: Optional[Path], pattern_id: int) -> dict[str, list[str]]:
    with store.connect(db) as conn:
        names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM sources")}
        return {names.get(s, s): [m["name"] for m in ms] for s, ms in pattern_members(conn, pattern_id).items()}


async def _explain_analogies(db: Optional[Path], model: BrainModel, ids: list[int], errors: list[str]) -> None:
    def load(aid: int) -> Optional[tuple]:
        with store.connect(db) as conn:
            row = conn.execute("SELECT a.*, sa.name AS na, sb.name AS nb FROM analogies a "
                               "JOIN sources sa ON sa.id = a.source_a JOIN sources sb ON sb.id = a.source_b "
                               "WHERE a.id = ?", (aid,)).fetchone()
            if row is None:
                return None
            mapping = [(_name(conn, m["a"]), _name(conn, m["b"])) for m in store.loads(row["mapping"], [])]
            suggestions = [_name(conn, s["piece"]) for s in store.loads(row["suggestions"], [])]
            return row["na"], row["nb"], mapping, suggestions

    for aid in ids:
        loaded = await asyncio.to_thread(load, aid)
        if loaded is None:
            continue
        try:
            text = await reasoning.explain_analogy(model, *loaded)
        except Exception as exc:  # noqa: BLE001 - the statistical explanation stays
            errors.append(f"analogy: {type(exc).__name__}: {exc}")
            return
        if text:
            def save() -> None:
                with store.write(db) as conn:
                    conn.execute("UPDATE analogies SET explanation = ?, explained_by = 'model' WHERE id = ?", (text, aid))
            await asyncio.to_thread(save)

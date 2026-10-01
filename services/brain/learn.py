"""Learn → Remember (Brain.md §4): a source becomes chunks, pieces and links.

One learn run over one source, in three phases so the event loop is never
blocked and the database is never held across a model call:

1. **prepare** (thread): list the readable files, compare them with what was
   learned before (size and mtime, then content hash), drop the chunks of
   changed and removed files and chunk what changed. Only changed files are
   read again: a new source merges into the graph without a rebuild (§4.7).
2. **understand** (async): each new chunk's concepts and stated relations,
   from the model while its call budget lasts, statistically for the rest.
   With a model, borderline "is this the same concept?" questions are asked
   here too.
3. **remember** (thread, one transaction): merge concepts into pieces by
   meaning (§4.3), so a concept a second source uses becomes one *shared*
   piece with a note per source; write the links from stated relations,
   structure and co-occurrence (§4.5); organise pieces into kinds (§4.4);
   refresh notes and meaning vectors; drop what lost all its evidence; flag
   what fits nothing known as novel (§7).
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from services.brain import chunker, files, graph, reasoning, store, vectors
from services.brain import extract as ex
from services.brain import text as tx
from services.brain.chunker import Chunk
from services.brain.model import BrainModel, ModelUnavailable
from services.timestamps import now_iso

logger = logging.getLogger(__name__)

NOVELTY_MIN_PIECES = 30      # below this, everything would look novel
NOVELTY_MAX_SIMILARITY = 0.12
NOVELTY_PER_RUN = 5
COOCCURRENCE_MIN_COUNT = 2
COOCCURRENCE_MIN_NPMI = 0.2
CONTAINS_WEIGHT = 0.35
USES_WEIGHT = 0.5
KIND_OF_WEIGHT = 0.6
EMBED_MERGE = 0.92
EMBED_ASK = 0.8
LEXICAL_MERGE = 0.82
LEXICAL_ASK = 0.6


@dataclass
class Prepared:
    source_id: str
    root: Path
    signature: str
    files_seen: int
    changed: list[str]
    removed: list[str]
    new_chunks: list[tuple[int, str, Chunk]]
    touched: set[int]
    dfs: dict[str, int]
    n_docs: int
    run_id: int


@dataclass
class Understood:
    by_chunk: dict[int, ex.Understanding]
    embeddings: dict[str, list[float]] = field(default_factory=dict)
    decisions: dict[str, int] = field(default_factory=dict)
    model_calls: int = 0
    model_errors: list[str] = field(default_factory=list)


# ── sources ──────────────────────────────────────────────────────────────────


def register_source(conn: sqlite3.Connection, *, source_id: str, kind: str, name: str,
                    location: str, now: str) -> dict:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO sources(id, kind, name, location, created_at) VALUES (?,?,?,?,?)",
                     (source_id, kind, name, location, now))
    else:
        conn.execute("UPDATE sources SET name = ?, location = ?, enabled = 1 WHERE id = ?",
                     (name, location, source_id))
    return dict(conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone())


def forget_source(conn: sqlite3.Connection, source_id: str, now: str) -> dict:
    """Remove a source and everything only it supported. Shared pieces stay,
    with the other sources' notes."""
    touched = {r[0] for r in conn.execute(
        "SELECT DISTINCT m.piece_id FROM mentions m JOIN chunks c ON c.id = m.chunk_id WHERE c.source_id = ?",
        (source_id,))}
    for row in conn.execute("SELECT title, text FROM chunks WHERE source_id = ?", (source_id,)).fetchall():
        _sub_df(conn, set(tx.terms(f"{row['title']}\n{row['text']}")))
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    touched = {p for p in touched if conn.execute("SELECT 1 FROM pieces WHERE id = ?", (p,)).fetchone()}
    _cooccurrence(conn, touched, now)
    _refresh_notes(conn, touched, now)
    for pid in touched:
        vectors.rebuild_piece_vector(conn, pid)
    return {"source_id": source_id, "removed": _cleanup(conn)}


# ── document frequencies ─────────────────────────────────────────────────────


def _add_df(conn: sqlite3.Connection, terms: set[str]) -> None:
    conn.executemany("INSERT INTO terms(term, df) VALUES (?, 1) ON CONFLICT(term) DO UPDATE SET df = df + 1",
                     [(t,) for t in terms])


def _sub_df(conn: sqlite3.Connection, terms: set[str]) -> None:
    conn.executemany("UPDATE terms SET df = df - 1 WHERE term = ?", [(t,) for t in terms])


# ── phase 1: prepare ─────────────────────────────────────────────────────────


def prepare(db: Optional[Path], source_id: str, *, force: bool = False) -> Prepared:
    now = now_iso()
    with store.write(db) as conn:
        src = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if src is None:
            raise store.BrainError("source_not_found", f"No source {source_id!r}; add the project first.", 404)
        root = Path(src["location"])
        if not root.is_dir():
            raise store.BrainError("source_missing", f"{root} is gone; remove the source or restore the folder.", 409)
        conn.execute("UPDATE sources SET status = 'learning', error = NULL WHERE id = ?", (source_id,))
        run_id = int(conn.execute("INSERT INTO runs(kind, source_id, status, started_at) VALUES "
                                  "('learn', ?, 'running', ?)", (source_id, now)).lastrowid)
        known = {r["path"]: (r["sha"], r["size"], r["mtime"])
                 for r in conn.execute("SELECT path, sha, size, mtime FROM files WHERE source_id = ?", (source_id,))}

    signature = files.signature(root)
    listed = files.list_files(root)
    read: dict[str, tuple[str, str, int, float]] = {}
    unchanged: set[str] = set()
    for rel in listed:
        try:
            st = (root / rel).stat()
        except OSError:
            continue
        prev = known.get(rel)
        if prev and not force and prev[1] == st.st_size and abs(prev[2] - st.st_mtime) < 1e-6:
            unchanged.add(rel)
            continue
        got = files.read_text(root, rel)
        if got is None:
            continue
        text, sha, size = got
        if prev and not force and prev[0] == sha:
            unchanged.add(rel)
            read[rel] = (text, sha, size, st.st_mtime)   # refresh mtime only
            continue
        read[rel] = (text, sha, size, st.st_mtime)
    changed = sorted(rel for rel in read if rel not in unchanged)
    removed = sorted(rel for rel in known if rel not in read and rel not in unchanged)

    new_chunks: list[tuple[int, str, Chunk]] = []
    touched: set[int] = set()
    with store.write(db) as conn:
        for rel in changed + removed:
            touched |= _drop_file(conn, source_id, rel)
        for rel in unchanged & set(read):
            _, sha, size, mtime = read[rel]
            conn.execute("UPDATE files SET mtime = ? WHERE source_id = ? AND path = ?", (mtime, source_id, rel))
        all_terms: set[str] = set()
        for rel in changed:
            text, sha, size, mtime = read[rel]
            for chunk in chunker.chunk_file(rel, text):
                cid = int(conn.execute(
                    "INSERT INTO chunks(source_id, path, start_line, end_line, kind, title, text) VALUES (?,?,?,?,?,?,?)",
                    (source_id, rel, chunk.start_line, chunk.end_line, chunk.kind, chunk.title, chunk.text)).lastrowid)
                chunk_terms = set(tx.terms(f"{chunk.title}\n{chunk.text}"))
                _add_df(conn, chunk_terms)
                all_terms |= chunk_terms
                new_chunks.append((cid, rel, chunk))
            conn.execute(
                "INSERT INTO files(source_id, path, sha, size, mtime, learned_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(source_id, path) DO UPDATE SET sha = excluded.sha, size = excluded.size, "
                "mtime = excluded.mtime, learned_at = excluded.learned_at",
                (source_id, rel, sha, size, mtime, now))
        conn.execute("DELETE FROM terms WHERE df <= 0")
        dfs = vectors.dfs(conn, all_terms)
        n_docs = store.chunk_count(conn)
    return Prepared(source_id, root, signature, len(listed), changed, removed, new_chunks,
                    touched, dfs, n_docs, run_id)


def _drop_file(conn: sqlite3.Connection, source_id: str, rel: str) -> set[int]:
    rows = conn.execute("SELECT id, title, text FROM chunks WHERE source_id = ? AND path = ?",
                        (source_id, rel)).fetchall()
    touched: set[int] = set()
    for row in rows:
        _sub_df(conn, set(tx.terms(f"{row['title']}\n{row['text']}")))
        touched |= {r[0] for r in conn.execute("SELECT piece_id FROM mentions WHERE chunk_id = ?", (row["id"],))}
    conn.execute("DELETE FROM chunks WHERE source_id = ? AND path = ?", (source_id, rel))
    conn.execute("DELETE FROM files WHERE source_id = ? AND path = ?", (source_id, rel))
    return touched


# ── phase 2: understand ──────────────────────────────────────────────────────


async def understand(db: Optional[Path], prep: Prepared, model: BrainModel, *, budget: int) -> Understood:
    out = Understood(by_chunk={})
    if model.can_reason and budget > 0 and prep.new_chunks:
        for batch in reasoning.batches(prep.new_chunks):
            if out.model_calls >= budget:
                break
            out.model_calls += 1
            try:
                out.by_chunk.update(await reasoning.extract(model, batch))
            except ModelUnavailable as exc:
                out.model_errors.append(str(exc))
                break
            except Exception as exc:  # noqa: BLE001 - that batch is read statistically instead
                out.model_errors.append(f"{type(exc).__name__}: {exc}")
    df = lambda term: prep.dfs.get(term, 0)  # noqa: E731
    for cid, rel, chunk in prep.new_chunks:
        if cid not in out.by_chunk:
            out.by_chunk[cid] = ex.understand(chunk, rel, df, prep.n_docs)

    concepts: dict[str, ex.Concept] = {}
    for u in out.by_chunk.values():
        for c in u.concepts:
            concepts.setdefault(c.key, c)
    if model.can_embed and concepts:
        keys = list(concepts)
        texts = [f"{concepts[k].name}: {concepts[k].description or concepts[k].usage}" for k in keys]
        try:
            vecs = []
            for i in range(0, len(texts), 64):
                got = await model.embed(texts[i:i + 64])
                if not got or len(got) != len(texts[i:i + 64]):
                    raise ValueError("embed returned a different number of vectors")
                vecs.extend(got)
            out.embeddings = {k: [float(x) for x in v] for k, v in zip(keys, vecs)}
        except Exception as exc:  # noqa: BLE001 - meaning falls back to TF-IDF
            out.model_errors.append(f"embed: {type(exc).__name__}: {exc}")

    candidates = await asyncio.to_thread(_merge_candidates, db, concepts, out.embeddings)
    for key, (pid, score, ask, other) in candidates.items():
        if not ask:
            out.decisions[key] = pid
            continue
        if not model.can_reason or out.model_calls >= budget:
            continue
        out.model_calls += 1
        c = concepts[key]
        try:
            if await reasoning.same_concept(model, (c.name, c.description or c.usage), other):
                out.decisions[key] = pid
        except Exception as exc:  # noqa: BLE001 - unsure means separate pieces
            out.model_errors.append(f"same: {type(exc).__name__}: {exc}")
    return out


def _merge_candidates(db: Optional[Path], concepts: dict[str, ex.Concept],
                      embeddings: dict[str, list[float]]) -> dict[str, tuple[int, float, bool, tuple[str, str]]]:
    """For each concept with no exact piece: the closest existing piece and
    whether it is a sure merge (``ask`` False) or a question for the model."""
    out: dict[str, tuple[int, float, bool, tuple[str, str]]] = {}
    with store.connect(db) as conn:
        for key, c in concepts.items():
            if conn.execute("SELECT 1 FROM pieces WHERE key = ?", (key,)).fetchone():
                continue
            best: Optional[tuple[int, float]] = None
            lexical = _near_piece(conn, key)
            if lexical:
                best = lexical
            emb = embeddings.get(key)
            if emb:
                near = vectors.nearest_embedding(conn, emb, limit=1)
                if near and (best is None or near[0][1] > best[1]):
                    best = near[0]
                if best and best[1] >= EMBED_MERGE:
                    out[key] = (*best, False, ("", ""))
                    continue
                if best and best[1] >= EMBED_ASK:
                    row = conn.execute("SELECT name, description FROM pieces WHERE id = ?", (best[0],)).fetchone()
                    out[key] = (*best, True, (row["name"], row["description"]))
                continue
            if best and best[1] >= LEXICAL_MERGE:
                out[key] = (*best, False, ("", ""))
            elif best and best[1] >= LEXICAL_ASK:
                row = conn.execute("SELECT name, description FROM pieces WHERE id = ?", (best[0],)).fetchone()
                out[key] = (*best, True, (row["name"], row["description"]))
    return out


def _near_piece(conn: sqlite3.Connection, key: str) -> Optional[tuple[int, float]]:
    """A piece whose name is the same words, allowing inflection
    (``spread``/``spreading``) or a close spelling."""
    words = key.split()
    if not words:
        return None
    head = words[0][:4]
    best: Optional[tuple[int, float]] = None
    grams = tx.char_grams(key)
    for row in conn.execute("SELECT id, key FROM pieces WHERE key LIKE ?", (head + "%",)):
        other = row["key"].split()
        if len(other) != len(words):
            continue
        if all(a == b or _inflection(a, b) for a, b in zip(words, other)):
            return int(row["id"]), 1.0
        score = tx.jaccard(grams, tx.char_grams(row["key"]))
        if best is None or score > best[1]:
            best = (int(row["id"]), score)
    return best


def _inflection(a: str, b: str) -> bool:
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 5 and long_.startswith(short) and len(long_) - len(short) <= 3


# ── phase 3: remember ────────────────────────────────────────────────────────


def remember(db: Optional[Path], prep: Prepared, und: Understood) -> dict:
    now = now_iso()
    with store.write(db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM pieces").fetchone()[0]
        touched = {p for p in prep.touched if conn.execute("SELECT 1 FROM pieces WHERE id = ?", (p,)).fetchone()}
        created: list[tuple[int, float]] = []
        stated: list[tuple[int, str, int, int]] = []
        chunk_pieces: dict[int, dict[str, int]] = {}

        for cid, rel, chunk in prep.new_chunks:
            u = und.by_chunk.get(cid) or ex.Understanding([], [])
            conn.execute("UPDATE chunks SET extractor = ? WHERE id = ?", (u.extractor, cid))
            by_key: dict[str, int] = {}
            for c in u.concepts:
                pid, is_new = _resolve_piece(conn, c, u.extractor, und, now)
                by_key[c.key] = pid
                touched.add(pid)
                if is_new:
                    created.append((pid, c.weight))
                conn.execute(
                    "INSERT INTO mentions(piece_id, chunk_id, weight, usage, defines) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(piece_id, chunk_id) DO UPDATE SET weight = MAX(weight, excluded.weight), "
                    "defines = MAX(defines, excluded.defines), "
                    "usage = CASE WHEN usage = '' THEN excluded.usage ELSE usage END",
                    (pid, cid, c.weight, c.usage, int(c.defines)))
            chunk_pieces[cid] = by_key
            for r in u.relations:
                a, b = by_key.get(r.subject), by_key.get(r.object)
                if a and b and a != b:
                    stated.append((a, r.phrase, b, cid))

        for a, phrase, b, cid in stated:
            graph.upsert_link(conn, a, b, phrase, kind="evidence", signal="stated", now=now, chunk_ids=[cid])
        _structure(conn, prep, chunk_pieces, now)
        _cooccurrence(conn, touched, now)
        _hierarchy(conn, touched, now)
        _refresh_notes(conn, touched, now)
        for pid in touched:
            if conn.execute("SELECT 1 FROM pieces WHERE id = ?", (pid,)).fetchone():
                vectors.rebuild_piece_vector(conn, pid)
        removed = _cleanup(conn)
        novel = _flag_novel(conn, created, before, prep.source_id, now)

        stats = source_stats(conn, prep.source_id)
        stats.update(files_seen=prep.files_seen, files_changed=len(prep.changed), files_removed=len(prep.removed),
                     chunks_new=len(prep.new_chunks), pieces_new=len(created), novel=novel,
                     model_calls=und.model_calls, model_errors=und.model_errors[:5], removed=removed)
        conn.execute("UPDATE sources SET status = 'ready', error = NULL, learned_at = ?, signature = ?, stats = ? "
                     "WHERE id = ?", (now, prep.signature, store.dumps(stats), prep.source_id))
        conn.execute("UPDATE runs SET status = 'done', finished_at = ?, stats = ? WHERE id = ?",
                     (now, store.dumps(stats), prep.run_id))
    return stats


def fail_run(db: Optional[Path], source_id: str, run_id: Optional[int], error: str) -> None:
    now = now_iso()
    with store.write(db) as conn:
        conn.execute("UPDATE sources SET status = 'error', error = ? WHERE id = ?", (error[:500], source_id))
        if run_id:
            conn.execute("UPDATE runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                         (now, error[:500], run_id))


def _resolve_piece(conn: sqlite3.Connection, c: ex.Concept, extractor: str, und: Understood,
                   now: str) -> tuple[int, bool]:
    row = conn.execute("SELECT id, origin, description, level, keywords FROM pieces WHERE key = ?",
                       (c.key,)).fetchone()
    pid = int(row["id"]) if row else und.decisions.get(c.key)
    if pid is not None:
        if row is None:
            row = conn.execute("SELECT id, origin, description, level, keywords FROM pieces WHERE id = ?",
                               (pid,)).fetchone()
        if row is not None:
            if extractor == "model" and c.description and row["origin"] != "model":
                conn.execute("UPDATE pieces SET description = ?, origin = 'model' WHERE id = ?", (c.description, pid))
            if c.level > row["level"]:
                conn.execute("UPDATE pieces SET level = ? WHERE id = ?", (c.level, pid))
            if c.keywords:
                merged = list(dict.fromkeys(store.loads(row["keywords"], []) + c.keywords))[:12]
                conn.execute("UPDATE pieces SET keywords = ? WHERE id = ?", (store.dumps(merged), pid))
            return pid, False
    # Two chunks of this same run may name one concept two ways.
    near = _near_piece(conn, c.key)
    if near and near[1] >= LEXICAL_MERGE:
        return near[0], False
    emb = und.embeddings.get(c.key)
    cur = conn.execute(
        "INSERT INTO pieces(key, name, description, level, keywords, embedding, origin, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (c.key, tx.display_name(c.name)[:120], (c.description or "")[:600], c.level, store.dumps(c.keywords[:12]),
         store.dumps(emb) if emb else None, extractor, now, now))
    return int(cur.lastrowid), True


def _structure(conn: sqlite3.Connection, prep: Prepared, chunk_pieces: dict[int, dict[str, int]], now: str) -> None:
    """Links read from where things are: a definition *contains* what its
    body talks about, and *uses* whatever it names that another chunk of the
    same source defines (an import or a call, in any language)."""
    source_id = prep.source_id
    definers: dict[int, int] = {}
    defined: dict[str, int] = {}
    for row in conn.execute(
            "SELECT m.piece_id, c.id AS chunk_id, c.title FROM mentions m JOIN chunks c ON c.id = m.chunk_id "
            "WHERE c.source_id = ? AND m.defines = 1", (source_id,)):
        definers[row["chunk_id"]] = row["piece_id"]
        if row["title"] and len(row["title"]) >= 3:
            defined.setdefault(row["title"], row["piece_id"])

    for cid, rel, chunk in prep.new_chunks:
        owner = definers.get(cid)
        if owner is None:
            continue
        for key, pid in chunk_pieces.get(cid, {}).items():
            if pid != owner:
                graph.upsert_link(conn, owner, pid, graph.CONTAINS, kind="evidence", signal="structure",
                                  now=now, weight=CONTAINS_WEIGHT, chunk_ids=[cid])

    if not defined:
        return
    # Every definition in the source is checked, not only new ones: a new
    # definition can be what an unchanged chunk was already calling.
    for row in conn.execute("SELECT id, text, title FROM chunks WHERE source_id = ? AND kind = 'definition'",
                            (source_id,)).fetchall():
        owner = definers.get(row["id"])
        if owner is None:
            continue
        for ident in tx.identifiers(row["text"]) & defined.keys():
            target = defined[ident]
            if target != owner and ident != row["title"]:
                graph.upsert_link(conn, owner, target, graph.USES, kind="evidence", signal="structure",
                                  now=now, weight=USES_WEIGHT, chunk_ids=[row["id"]])


def _cooccurrence(conn: sqlite3.Connection, touched: set[int], now: str) -> None:
    """Statistics: two pieces that keep appearing in the same chunks, scored
    by normalised pointwise mutual information (−1 … 1)."""
    n = store.chunk_count(conn)
    if n < 2 or not touched:
        return
    counts: dict[int, int] = {}

    def count(pid: int) -> int:
        if pid not in counts:
            counts[pid] = conn.execute("SELECT COUNT(*) FROM mentions WHERE piece_id = ?", (pid,)).fetchone()[0]
        return counts[pid]

    for p in touched:
        keep: set[int] = set()
        rows = conn.execute(
            "SELECT m2.piece_id AS q, COUNT(*) AS n FROM mentions m1 JOIN mentions m2 "
            "ON m1.chunk_id = m2.chunk_id AND m2.piece_id != m1.piece_id WHERE m1.piece_id = ? "
            "GROUP BY m2.piece_id HAVING COUNT(*) >= ?", (p, COOCCURRENCE_MIN_COUNT)).fetchall()
        for row in rows:
            q, nab = row["q"], row["n"]
            pa, pb, pab = count(p) / n, count(q) / n, nab / n
            npmi = 1.0 if pab >= 1.0 else math.log(pab / (pa * pb)) / -math.log(pab)
            if npmi < COOCCURRENCE_MIN_NPMI:
                continue
            a, b = sorted((p, q))
            keep.add(b if a == p else a)
            shared = [r[0] for r in conn.execute(
                "SELECT m1.chunk_id FROM mentions m1 JOIN mentions m2 ON m1.chunk_id = m2.chunk_id "
                "WHERE m1.piece_id = ? AND m2.piece_id = ? LIMIT 5", (a, b))]
            graph.upsert_link(conn, a, b, graph.APPEARS_WITH, kind="evidence", signal="cooccurrence", now=now,
                              weight=round(0.3 + 0.5 * npmi, 4), chunk_ids=shared)
        # A pair that no longer co-occurs enough loses its statistical link.
        for row in conn.execute("SELECT id, src, dst FROM links WHERE signal = 'cooccurrence' AND (src = ? OR dst = ?)",
                                (p, p)).fetchall():
            other = row["dst"] if row["src"] == p else row["src"]
            if other not in keep:
                conn.execute("DELETE FROM links WHERE id = ?", (row["id"],))


def _hierarchy(conn: sqlite3.Connection, touched: set[int], now: str) -> None:
    """Organise (§4.4): a specific phrase whose last words name another
    piece is a kind of it ("sqlite store" → "store"). Only for pieces used in
    prose, where a noun phrase is a noun phrase; function names like
    ``learn_source`` are not kinds of "source"."""
    def prose_chunks(pid: int) -> list[int]:
        return [r[0] for r in conn.execute(
            "SELECT m.chunk_id FROM mentions m JOIN chunks c ON c.id = m.chunk_id "
            "WHERE m.piece_id = ? AND c.kind IN ('section', 'paragraph') LIMIT 3", (pid,))]

    def link_general(pid: int, key: str) -> None:
        words = key.split()
        for i in range(1, len(words)):
            general = conn.execute("SELECT id FROM pieces WHERE key = ?", (" ".join(words[i:]),)).fetchone()
            if general:
                evidence = prose_chunks(pid)
                if evidence:
                    graph.upsert_link(conn, pid, general[0], graph.KIND_OF, kind="evidence", signal="hierarchy",
                                      now=now, weight=KIND_OF_WEIGHT, chunk_ids=evidence)
                return

    for pid in touched:
        row = conn.execute("SELECT key FROM pieces WHERE id = ?", (pid,)).fetchone()
        if row is None:
            continue
        link_general(pid, row["key"])
        for spec in conn.execute("SELECT id, key FROM pieces WHERE key LIKE ?", ("% " + row["key"],)).fetchall():
            link_general(spec["id"], spec["key"])
    # Level of abstraction: at least one above the most abstract kind below it.
    for _ in range(3):
        rows = conn.execute(
            "SELECT l.dst, MAX(p.level) AS below FROM links l JOIN pieces p ON p.id = l.src "
            "WHERE l.signal = 'hierarchy' GROUP BY l.dst").fetchall()
        changed = 0
        for row in rows:
            changed += conn.execute("UPDATE pieces SET level = ? WHERE id = ? AND level < ?",
                                    (row["below"] + 1, row["dst"], row["below"] + 1)).rowcount
        if not changed:
            break


def _refresh_notes(conn: sqlite3.Connection, touched: set[int], now: str) -> None:
    """A source note per (piece, source): the source's own usage sentence,
    how often it mentions the piece, and a strength in 0..1."""
    for pid in touched:
        per_source = conn.execute(
            "SELECT c.source_id, COUNT(*) AS n, MAX(m.weight) AS w FROM mentions m "
            "JOIN chunks c ON c.id = m.chunk_id WHERE m.piece_id = ? GROUP BY c.source_id", (pid,)).fetchall()
        seen = set()
        for row in per_source:
            seen.add(row["source_id"])
            usage = conn.execute(
                "SELECT m.usage FROM mentions m JOIN chunks c ON c.id = m.chunk_id WHERE m.piece_id = ? "
                "AND c.source_id = ? AND m.usage != '' ORDER BY m.defines DESC, m.weight DESC LIMIT 1",
                (pid, row["source_id"])).fetchone()
            strength = round(1.0 - math.exp(-row["n"] / 3.0), 4)
            conn.execute(
                "INSERT INTO notes(piece_id, source_id, note, strength, mentions, origin, updated_at) "
                "VALUES (?,?,?,?,?,'learned',?) ON CONFLICT(piece_id, source_id) DO UPDATE SET "
                "note = CASE WHEN notes.origin = 'experience' AND excluded.note = '' THEN notes.note "
                "ELSE excluded.note END, strength = MAX(excluded.strength, "
                "CASE WHEN notes.origin = 'experience' THEN notes.strength ELSE 0 END), "
                "mentions = excluded.mentions, updated_at = excluded.updated_at",
                (pid, row["source_id"], usage[0] if usage else "", strength, row["n"], now))
        placeholders = ",".join("?" * len(seen)) or "''"
        conn.execute(f"DELETE FROM notes WHERE piece_id = ? AND origin = 'learned' AND source_id NOT IN ({placeholders})",
                     (pid, *seen))
        conn.execute(f"UPDATE notes SET mentions = 0 WHERE piece_id = ? AND origin = 'experience' "
                     f"AND source_id NOT IN ({placeholders})", (pid, *seen))
        desc = conn.execute("SELECT description FROM pieces WHERE id = ?", (pid,)).fetchone()
        if desc is not None and not desc[0]:
            first = conn.execute(
                "SELECT m.usage FROM mentions m WHERE m.piece_id = ? AND m.usage != '' "
                "ORDER BY m.defines DESC, m.weight DESC LIMIT 1", (pid,)).fetchone()
            if first:
                conn.execute("UPDATE pieces SET description = ?, updated_at = ? WHERE id = ?", (first[0], now, pid))


def _cleanup(conn: sqlite3.Connection) -> dict:
    links = conn.execute(
        "DELETE FROM links WHERE kind = 'evidence' AND id NOT IN (SELECT link_id FROM link_evidence)").rowcount
    pieces = conn.execute(
        "DELETE FROM pieces WHERE id NOT IN (SELECT piece_id FROM mentions) "
        "AND id NOT IN (SELECT piece_id FROM notes) AND id NOT IN (SELECT piece_id FROM pattern_members) "
        "AND score = 0 AND origin != 'generated'").rowcount
    conn.execute("DELETE FROM relations WHERE id NOT IN (SELECT relation_id FROM links)")
    conn.execute("DELETE FROM terms WHERE df <= 0")
    return {"pieces": pieces, "links": links}


def _flag_novel(conn: sqlite3.Connection, created: list[tuple[int, float]], before: int,
                source_id: str, now: str) -> int:
    """New knowledge that resembles nothing already known (§7). Only once the
    graph holds enough to compare with, and only the strongest few per run."""
    if before < NOVELTY_MIN_PIECES or not created:
        return 0
    new_ids = {pid for pid, _ in created}
    own_names = source_name_keys(conn)
    flagged = 0
    for pid, _weight in sorted(created, key=lambda x: -x[1])[:NOVELTY_PER_RUN * 4]:
        if conn.execute("SELECT key FROM pieces WHERE id = ?", (pid,)).fetchone()[0] in own_names:
            continue   # a project's own name is new to the graph, not a new idea
        vec = vectors.piece_vectors(conn, [pid]).get(pid, {})
        near = vectors.nearest(conn, vec, limit=1, exclude=new_ids)
        if near and near[0][1] >= NOVELTY_MAX_SIMILARITY:
            continue
        row = conn.execute("SELECT name, description FROM pieces WHERE id = ?", (pid,)).fetchone()
        conn.execute("UPDATE pieces SET novel = 1 WHERE id = ?", (pid,))
        store.upsert_finding(conn, kind="novel", ref=str(pid), title=f"New concept: {row['name']}",
                             body=row["description"], source_id=source_id, now=now, reopen=False)
        flagged += 1
        if flagged >= NOVELTY_PER_RUN:
            break
    return flagged


def source_name_keys(conn: sqlite3.Connection) -> set[str]:
    """The sources' own names as piece keys: a README titled after its
    project yields a piece that says which project, not what it knows."""
    return {tx.phrase_key(r[0]) for r in conn.execute("SELECT name FROM sources")}


def source_stats(conn: sqlite3.Connection, source_id: str) -> dict:
    one = lambda sql: conn.execute(sql, (source_id,)).fetchone()[0]  # noqa: E731
    return {
        "files": one("SELECT COUNT(*) FROM files WHERE source_id = ?"),
        "chunks": one("SELECT COUNT(*) FROM chunks WHERE source_id = ?"),
        "pieces": one("SELECT COUNT(*) FROM notes WHERE source_id = ?"),
        "shared": one("SELECT COUNT(*) FROM notes n WHERE n.source_id = ? AND "
                      "(SELECT COUNT(*) FROM notes n2 WHERE n2.piece_id = n.piece_id) > 1"),
    }


# ── the run ──────────────────────────────────────────────────────────────────


async def learn_source(source_id: str, *, model: BrainModel, budget: int, force: bool = False,
                       db: Optional[Path] = None) -> dict:
    prep: Optional[Prepared] = None
    try:
        prep = await asyncio.to_thread(prepare, db, source_id, force=force)
        und = await understand(db, prep, model, budget=budget)
        return await asyncio.to_thread(remember, db, prep, und)
    except store.BrainError as exc:
        if exc.code != "source_not_found":
            await asyncio.to_thread(fail_run, db, source_id, prep.run_id if prep else None, exc.message)
        raise
    except Exception as exc:
        logger.exception("brain: learning %s failed", source_id)
        await asyncio.to_thread(fail_run, db, source_id, prep.run_id if prep else None,
                                f"{type(exc).__name__}: {exc}")
        raise


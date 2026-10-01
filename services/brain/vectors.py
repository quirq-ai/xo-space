"""Meaning vectors over the store: lookup, weighting, nearest pieces.

A piece's vector lives in ``piece_terms`` (term → raw weight). It is
weighted by IDF over ``terms`` when compared, and candidates are found
through the ``piece_terms(term)`` index, so a lookup reads only pieces that
share a term with the query. When the connected model embeds, a dense
``pieces.embedding`` is compared as well and the better of the two wins.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Iterable, Optional

from services.brain import store
from services.brain import text as tx

CANDIDATES = 400
VECTOR_TERMS = 48


def dfs(conn: sqlite3.Connection, terms: Iterable[str]) -> dict[str, int]:
    terms = list(set(terms))
    out: dict[str, int] = {}
    for i in range(0, len(terms), 500):
        part = terms[i:i + 500]
        marks = ",".join("?" * len(part))
        for row in conn.execute(f"SELECT term, df FROM terms WHERE term IN ({marks})", part):
            out[row[0]] = row[1]
    return out


def weighted(conn: sqlite3.Connection, tf: dict[str, float], n_docs: Optional[int] = None) -> dict[str, float]:
    n = store.chunk_count(conn) if n_docs is None else n_docs
    return tx.weigh(tf, dfs(conn, tf), n)


def piece_vectors(conn: sqlite3.Connection, ids: Iterable[int]) -> dict[int, dict[str, float]]:
    ids = list(set(ids))
    out: dict[int, dict[str, float]] = {i: {} for i in ids}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        marks = ",".join("?" * len(part))
        for row in conn.execute(f"SELECT piece_id, term, tf FROM piece_terms WHERE piece_id IN ({marks})", part):
            out[row[0]][row[1]] = row[2]
    return out


def nearest(conn: sqlite3.Connection, query_tf: dict[str, float], *, limit: int = 10,
            exclude: Iterable[int] = (), restrict: Optional[set[int]] = None) -> list[tuple[int, float]]:
    """Pieces by cosine similarity (IDF-weighted) to ``query_tf``, best first."""
    if not query_tf:
        return []
    n = store.chunk_count(conn)
    query = tx.weigh(query_tf, dfs(conn, query_tf), n)
    terms = list(query_tf)
    marks = ",".join("?" * len(terms))
    rows = conn.execute(
        f"SELECT piece_id, SUM(tf) s FROM piece_terms WHERE term IN ({marks}) "
        f"GROUP BY piece_id ORDER BY s DESC LIMIT {CANDIDATES}", terms).fetchall()
    skip = set(exclude)
    ids = [r[0] for r in rows if r[0] not in skip and (restrict is None or r[0] in restrict)]
    vectors = piece_vectors(conn, ids)
    all_terms = {t for v in vectors.values() for t in v}
    df_all = dfs(conn, all_terms)
    scored = [(pid, tx.cosine(query, tx.weigh(vec, df_all, n))) for pid, vec in vectors.items()]
    scored = [(pid, s) for pid, s in scored if s > 0]
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:limit]


def nearest_embedding(conn: sqlite3.Connection, vector: list[float], *, limit: int = 10,
                      exclude: Iterable[int] = ()) -> list[tuple[int, float]]:
    skip = set(exclude)
    scored = []
    for row in conn.execute("SELECT id, embedding FROM pieces WHERE embedding IS NOT NULL"):
        if row[0] in skip:
            continue
        emb = store.loads(row[1], None)
        if isinstance(emb, list):
            scored.append((row[0], tx.dense_cosine(vector, emb)))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:limit]


def text_vector(text: str) -> dict[str, float]:
    return dict(Counter(tx.terms(text)))


def rebuild_piece_vector(conn: sqlite3.Connection, piece_id: int) -> None:
    """A piece's meaning: its name (heaviest), description and keywords,
    the sentences that use it, and the vocabulary around its mentions, so
    two differently named pieces used the same way end up close."""
    row = conn.execute("SELECT name, description, keywords FROM pieces WHERE id = ?", (piece_id,)).fetchone()
    if row is None:
        return
    tf: Counter = Counter()
    for t in tx.terms(row["name"]):
        tf[t] += 3.0
    for t in tx.terms(row["description"]):
        tf[t] += 1.0
    for kw in store.loads(row["keywords"], []):
        for t in tx.terms(str(kw)):
            tf[t] += 1.0
    mentions = conn.execute(
        "SELECT m.usage, c.text FROM mentions m JOIN chunks c ON c.id = m.chunk_id "
        "WHERE m.piece_id = ? ORDER BY m.defines DESC, m.weight DESC LIMIT 12", (piece_id,)).fetchall()
    for m in mentions:
        for t in tx.terms(m["usage"]):
            tf[t] += 0.75
        context = Counter(tx.terms(m["text"]))
        for t, _ in context.most_common(15):
            tf[t] += 0.5
    keep = dict(sorted(tf.items(), key=lambda kv: (-kv[1], kv[0]))[:VECTOR_TERMS])
    conn.execute("DELETE FROM piece_terms WHERE piece_id = ?", (piece_id,))
    conn.executemany("INSERT INTO piece_terms(piece_id, term, tf) VALUES (?, ?, ?)",
                     [(piece_id, t, round(w, 4)) for t, w in keep.items()])

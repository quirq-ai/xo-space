"""Links and relation types: how the graph is written and how it changes
with use (Brain.md §4.6, §6).

A link's weight has two parts. ``base_weight`` is what the evidence says and
is recomputed whenever the source is learned again; ``weight`` is that plus
whatever use added or time took away. Re-learning moves ``weight`` by the
change in ``base_weight`` only, so learning never erases what use taught.

Relation types are learned: a phrase joins the type whose phrases share
its content words, otherwise it starts a new type. A type is named by its
most frequent phrase. A connected model can merge types that mean the same
thing in different words (``consolidate_relations``).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

from services.brain import store
from services.brain import text as tx
from services.timestamps import parse_ts

# Phrases the brain uses for links it reads from structure or use rather
# than from a sentence. They join relation types like any written phrase.
USES = "uses"
CONTAINS = "contains"
APPEARS_WITH = "appears with"
KIND_OF = "is a kind of"
RECALLED_WITH = "recalled with"
USED_TOGETHER = "used together in"
MAY_RELATE = "may relate to"

_STATED_BASE, _STATED_STEP = 0.6, 0.1
MIN_LEARNED_WEIGHT = 0.02


def _content_stems(phrase: str) -> set[str]:
    return {tx.stem(w) for w in tx.words(phrase) if tx.is_content(w)}


def normalize_phrase(phrase: str) -> str:
    return " ".join(tx.words(phrase))[:60]


def relation_for(conn: sqlite3.Connection, phrase: str) -> int:
    norm = normalize_phrase(phrase) or phrase.strip().lower()
    key = tx.phrase_key(norm)
    stems = _content_stems(norm)
    best: Optional[tuple[float, sqlite3.Row]] = None
    for row in conn.execute("SELECT id, label, phrases FROM relations"):
        phrases = store.loads(row["phrases"], {})
        for p in phrases:
            if tx.phrase_key(p) == key:
                score = 1.0
            else:
                other = _content_stems(p)
                score = tx.jaccard(stems, other) if stems and other else 0.0
            if score >= 0.5 and (best is None or score > best[0]):
                best = (score, row)
    if best is None:
        cur = conn.execute("INSERT INTO relations(label, phrases, uses) VALUES (?, ?, 1)",
                           (norm, store.dumps({norm: 1})))
        return int(cur.lastrowid)
    row = best[1]
    phrases = store.loads(row["phrases"], {})
    phrases[norm] = phrases.get(norm, 0) + 1
    label = max(phrases.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
    if label != row["label"] and conn.execute(
            "SELECT 1 FROM relations WHERE label = ? AND id != ?", (label, row["id"])).fetchone():
        label = row["label"]
    conn.execute("UPDATE relations SET phrases = ?, label = ?, uses = uses + 1 WHERE id = ?",
                 (store.dumps(phrases), label, row["id"]))
    return int(row["id"])


def upsert_link(conn: sqlite3.Connection, src: int, dst: int, phrase: str, *, kind: str, signal: str,
                now: str, weight: Optional[float] = None, chunk_ids: Iterable[int] = (),
                explanation: str = "") -> int:
    """Create or refresh a link. ``weight`` given: that is the new base
    (statistics, structure). ``weight`` omitted: the base grows with the
    number of evidence chunks (stated relations)."""
    if src == dst:
        raise ValueError("a link joins two different pieces")
    rel = relation_for(conn, phrase)
    row = conn.execute(
        "SELECT id, weight, base_weight FROM links WHERE src = ? AND dst = ? AND relation_id = ? "
        "AND kind = ? AND signal = ?", (src, dst, rel, kind, signal)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO links(src, dst, relation_id, phrase, kind, signal, weight, base_weight, support, "
            "explanation, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,0,?,?,?)",
            (src, dst, rel, normalize_phrase(phrase), kind, signal, _STATED_BASE if weight is None else weight,
             _STATED_BASE if weight is None else weight, explanation, now, now))
        link_id, old_base, old_weight = int(cur.lastrowid), None, None
    else:
        link_id, old_base, old_weight = int(row["id"]), row["base_weight"], row["weight"]
    conn.executemany("INSERT OR IGNORE INTO link_evidence(link_id, chunk_id) VALUES (?, ?)",
                     [(link_id, c) for c in chunk_ids])
    support = conn.execute("SELECT COUNT(*) FROM link_evidence WHERE link_id = ?", (link_id,)).fetchone()[0]
    base = weight if weight is not None else min(0.95, _STATED_BASE + _STATED_STEP * max(0, support - 1))
    new_weight = base if old_base is None else max(0.01, min(1.0, old_weight + (base - old_base)))
    conn.execute(
        "UPDATE links SET base_weight = ?, weight = ?, support = ?, updated_at = ?, "
        "explanation = CASE WHEN ? != '' THEN ? ELSE explanation END WHERE id = ?",
        (round(base, 4), round(new_weight, 4), support, now, explanation, explanation, link_id))
    return link_id


def strengthen(conn: sqlite3.Connection, a: int, b: int, *, amount: float, now: str,
               phrase: str = RECALLED_WITH, kind: str = "learned", signal: str = "corecall") -> None:
    """Pieces used together: every fact link between them gains
    ``amount × (1 − weight)``; with no link at all, a weak learned one is
    created (Brain.md §6)."""
    if a == b:
        return
    rows = conn.execute(
        "SELECT id, weight FROM links WHERE ((src = ? AND dst = ?) OR (src = ? AND dst = ?)) "
        f"AND kind IN ({','.join('?' * len(store.FACT_KINDS))})", (a, b, b, a, *store.FACT_KINDS)).fetchall()
    if not rows:
        src, dst = sorted((a, b))
        rel = relation_for(conn, phrase)
        conn.execute(
            "INSERT OR IGNORE INTO links(src, dst, relation_id, phrase, kind, signal, weight, base_weight, "
            "support, uses, created_at, updated_at, last_used) VALUES (?,?,?,?,?,?,?,?,0,1,?,?,?)",
            (src, dst, rel, phrase, kind, signal, round(0.1 + amount, 4), 0.0, now, now, now))
        return
    for row in rows:
        w = row["weight"] + amount * (1.0 - row["weight"])
        conn.execute("UPDATE links SET weight = ?, uses = uses + 1, last_used = ? WHERE id = ?",
                     (round(min(1.0, w), 4), now, row["id"]))


def touch(conn: sqlite3.Connection, link_ids: Iterable[int], now: str) -> None:
    conn.executemany("UPDATE links SET uses = uses + 1, last_used = ? WHERE id = ?",
                     [(now, i) for i in set(link_ids)])


def fade(conn: sqlite3.Connection, now: str, per_day: float) -> dict:
    """Unused links lose ``per_day`` of their weight per day since the last
    fade. Evidence never falls below half its base; learned and hypothesis
    links that fade under :data:`MIN_LEARNED_WEIGHT` are removed."""
    last_text = store.meta_get(conn, "faded_at")
    store.meta_set(conn, "faded_at", now)
    last = parse_ts(last_text)
    current = parse_ts(now) or datetime.now(timezone.utc)
    if last is None or per_day <= 0:
        return {"faded": 0, "removed": 0}
    days = max(0.0, (current - last).total_seconds() / 86400.0)
    if days <= 0:
        return {"faded": 0, "removed": 0}
    factor = (1.0 - per_day) ** days
    # A link used since the last fade keeps its weight this time.
    faded = conn.execute(
        "UPDATE links SET weight = MAX(CASE WHEN kind = 'evidence' THEN base_weight * 0.5 ELSE 0 END, "
        "ROUND(weight * ?, 4)) WHERE last_used IS NULL OR last_used <= ?",
        (factor, last_text)).rowcount
    removed = conn.execute(
        "DELETE FROM links WHERE kind IN ('learned', 'hypothesis') AND weight < ?",
        (MIN_LEARNED_WEIGHT,)).rowcount
    return {"faded": faded, "removed": removed}


async def consolidate_relations(db, model) -> int:
    """Merge relation types a model says mean the same (``speeds up`` /
    ``accelerates``). Returns how many types were merged away."""
    import asyncio

    from services.brain import reasoning

    def labels() -> list[str]:
        with store.connect(db) as conn:
            return [r[0] for r in conn.execute("SELECT label FROM relations ORDER BY uses DESC LIMIT 120")]

    names = await asyncio.to_thread(labels)
    if len(names) < 2:
        return 0
    groups = await reasoning.group_relations(model, names)

    def merge() -> int:
        merged = 0
        with store.write(db) as conn:
            for group in groups:
                ids = []
                for label in group:
                    row = conn.execute("SELECT id FROM relations WHERE label = ?", (label,)).fetchone()
                    if row:
                        ids.append(row[0])
                if len(ids) < 2:
                    continue
                keep, rest = ids[0], ids[1:]
                phrases = store.loads(conn.execute("SELECT phrases FROM relations WHERE id = ?",
                                                   (keep,)).fetchone()[0], {})
                for rid in rest:
                    for p, n in store.loads(conn.execute("SELECT phrases FROM relations WHERE id = ?",
                                                         (rid,)).fetchone()[0], {}).items():
                        phrases[p] = phrases.get(p, 0) + n
                    for link in conn.execute("SELECT * FROM links WHERE relation_id = ?", (rid,)).fetchall():
                        twin = conn.execute(
                            "SELECT id FROM links WHERE src = ? AND dst = ? AND relation_id = ? AND kind = ? "
                            "AND signal = ?", (link["src"], link["dst"], keep, link["kind"], link["signal"])).fetchone()
                        if twin:
                            conn.execute("INSERT OR IGNORE INTO link_evidence(link_id, chunk_id) "
                                         "SELECT ?, chunk_id FROM link_evidence WHERE link_id = ?", (twin[0], link["id"]))
                            conn.execute("UPDATE links SET weight = MAX(weight, ?) WHERE id = ?", (link["weight"], twin[0]))
                            conn.execute("DELETE FROM links WHERE id = ?", (link["id"],))
                        else:
                            conn.execute("UPDATE links SET relation_id = ? WHERE id = ?", (keep, link["id"]))
                    conn.execute("DELETE FROM relations WHERE id = ?", (rid,))
                    merged += 1
                conn.execute("UPDATE relations SET phrases = ?, label = ? WHERE id = ?",
                             (store.dumps(phrases), group[0], keep))
        return merged

    return await asyncio.to_thread(merge)

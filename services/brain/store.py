"""The brain's store: one SQLite file, ``~/.quirq/brain/brain.db``.

SQLite (the standard library's ``sqlite3``) rather than the JSON documents
the other stores keep, because every learn run and every recall changes a
few links and counts in a graph of thousands: a JSON store would rewrite the
whole document each time. The file is machine-local state like the rest of
the state root and holds nothing that is not rebuilt from the projects,
except what use and experience taught it (link weights, experiences).

The data rule "every data file carries a ``schema`` number" holds as
``PRAGMA user_version`` plus a ``meta`` row. A file written by a newer
release (a higher number) is refused, never rewritten.

Tables, by building block (Brain.md §3):

=============  ==========================================================
sources        a project the brain learns from (``id`` is its pid)
files          what was read from each source, by content hash
chunks         a small unit of raw content, with its file and lines
terms          how many chunks each word stem appears in (for TF-IDF)
pieces         one unit of knowledge; ``piece_terms`` is its meaning vector
mentions       a piece seen in a chunk: its evidence
notes          how one source uses a piece (the source note)
relations      a learned relation type and the phrases grouped into it
links          a typed, directed, weighted relationship between pieces;
               ``link_evidence`` holds the chunks that support it
patterns       a recurring group of pieces, ``pattern_members`` per source
analogies      a pattern read across two sources
findings       what needs a person: gaps, novelty, patterns, designs
goals/designs  the Create step; ``experiences`` hold what came of it
runs           one row per learn or discovery run
=============  ==========================================================
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from services.errors import ServiceError
from services.storage.layout import brain_dir

logger = logging.getLogger(__name__)

SCHEMA = 1
DB_NAME = "brain.db"
LINK_KINDS = ("evidence", "hypothesis", "learned", "generated")
FACT_KINDS = ("evidence", "learned", "generated")

# One writer at a time inside this process; SQLite's own lock covers others.
_write_lock = threading.RLock()

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  location TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'new',
  error TEXT,
  signature TEXT,
  created_at TEXT NOT NULL,
  learned_at TEXT,
  stats TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS files (
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  sha TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL DEFAULT 0,
  learned_at TEXT NOT NULL,
  PRIMARY KEY (source_id, path)
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  extractor TEXT NOT NULL DEFAULT 'statistical'
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(source_id, path);
CREATE TABLE IF NOT EXISTS terms (term TEXT PRIMARY KEY, df INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pieces (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  level INTEGER NOT NULL DEFAULT 0,
  keywords TEXT NOT NULL DEFAULT '[]',
  embedding TEXT,
  origin TEXT NOT NULL,
  novel INTEGER NOT NULL DEFAULT 0,
  score REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS piece_terms (
  piece_id INTEGER NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
  term TEXT NOT NULL,
  tf REAL NOT NULL,
  PRIMARY KEY (piece_id, term)
);
CREATE INDEX IF NOT EXISTS piece_terms_term ON piece_terms(term);
CREATE TABLE IF NOT EXISTS mentions (
  piece_id INTEGER NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
  chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  weight REAL NOT NULL,
  usage TEXT NOT NULL DEFAULT '',
  defines INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (piece_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS mentions_chunk ON mentions(chunk_id);
CREATE TABLE IF NOT EXISTS notes (
  piece_id INTEGER NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  note TEXT NOT NULL DEFAULT '',
  strength REAL NOT NULL DEFAULT 0,
  mentions INTEGER NOT NULL DEFAULT 0,
  origin TEXT NOT NULL DEFAULT 'learned',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (piece_id, source_id)
);
CREATE INDEX IF NOT EXISTS notes_source ON notes(source_id);
CREATE TABLE IF NOT EXISTS relations (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL UNIQUE,
  phrases TEXT NOT NULL DEFAULT '{}',
  uses INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY,
  src INTEGER NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
  dst INTEGER NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
  relation_id INTEGER NOT NULL REFERENCES relations(id),
  phrase TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL CHECK (kind IN ('evidence','hypothesis','learned','generated')),
  signal TEXT NOT NULL,
  weight REAL NOT NULL,
  base_weight REAL NOT NULL,
  support INTEGER NOT NULL DEFAULT 0,
  uses INTEGER NOT NULL DEFAULT 0,
  explanation TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_used TEXT,
  UNIQUE (src, dst, relation_id, kind, signal)
);
CREATE INDEX IF NOT EXISTS links_src ON links(src);
CREATE INDEX IF NOT EXISTS links_dst ON links(dst);
CREATE TABLE IF NOT EXISTS link_evidence (
  link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
  chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  PRIMARY KEY (link_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS link_evidence_chunk ON link_evidence(chunk_id);
CREATE TABLE IF NOT EXISTS patterns (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  support INTEGER NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pattern_members (
  pattern_id INTEGER NOT NULL REFERENCES patterns(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  piece_id INTEGER NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
  PRIMARY KEY (pattern_id, source_id, piece_id)
);
CREATE TABLE IF NOT EXISTS analogies (
  id INTEGER PRIMARY KEY,
  pattern_id INTEGER NOT NULL REFERENCES patterns(id) ON DELETE CASCADE,
  source_a TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  source_b TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  mapping TEXT NOT NULL DEFAULT '[]',
  suggestions TEXT NOT NULL DEFAULT '[]',
  explanation TEXT NOT NULL DEFAULT '',
  explained_by TEXT NOT NULL DEFAULT 'statistical',
  score REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (pattern_id, source_a, source_b)
);
CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  ref TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  source_id TEXT,
  count INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (kind, ref)
);
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  context_source TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS designs (
  id INTEGER PRIMARY KEY,
  goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  plan TEXT NOT NULL DEFAULT '{}',
  scores TEXT NOT NULL DEFAULT '{}',
  score REAL NOT NULL DEFAULT 0,
  rank INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  project TEXT,
  build TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiences (
  id INTEGER PRIMARY KEY,
  goal TEXT NOT NULL,
  design_id INTEGER REFERENCES designs(id) ON DELETE SET NULL,
  source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
  result TEXT NOT NULL CHECK (result IN ('worked','failed','partial')),
  lessons TEXT NOT NULL DEFAULT '',
  pieces TEXT NOT NULL DEFAULT '[]',
  patterns TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  source_id TEXT,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  stats TEXT NOT NULL DEFAULT '{}',
  error TEXT
);
"""


class BrainError(ServiceError):
    """Typed failure the router maps to ``HTTPException(status, {code, message})``."""


def db_path() -> Path:
    return brain_dir() / DB_NAME


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def _ensure_schema(conn: sqlite3.Connection, path: Path) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA:
        raise BrainError(
            "store_newer",
            f"{path} was written by a newer release (schema {version}, this one reads {SCHEMA}). "
            "Update XO Space; the file is left as it is.",
            status=409,
        )
    if version == SCHEMA:
        return
    with _write_lock:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_DDL)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA),))
        conn.execute(f"PRAGMA user_version = {SCHEMA}")
        conn.commit()


@contextmanager
def connect(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """A connection with the schema in place. Commits on a clean exit and
    rolls back on an exception; writes should go through :func:`write`."""
    target = path or db_path()
    try:
        conn = _open(target)
    except sqlite3.Error as exc:
        raise BrainError("store_unavailable", f"Could not open {target}: {exc}", status=503) from exc
    try:
        _ensure_schema(conn, target)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """:func:`connect`, holding this process's writer lock for the whole
    transaction so two learn runs never interleave their counts."""
    with _write_lock:
        with connect(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn


# ── small shared helpers ─────────────────────────────────────────────────────


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(text: Optional[str], default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def chunk_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


def upsert_finding(conn: sqlite3.Connection, *, kind: str, ref: str, title: str, body: str = "",
                   source_id: Optional[str] = None, now: str, reopen: bool = True) -> int:
    """One finding per ``(kind, ref)``: a repeat bumps ``count`` and
    ``updated_at`` (the Inbox feeder's cursor), and reopens a finding a
    person closed only when ``reopen``."""
    row = conn.execute("SELECT id, status FROM findings WHERE kind = ? AND ref = ?", (kind, ref)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO findings(kind, ref, title, body, source_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (kind, ref, title[:300], body[:4000], source_id, now, now))
        return int(cur.lastrowid)
    status = "open" if reopen else row["status"]
    conn.execute(
        "UPDATE findings SET title = ?, body = ?, count = count + 1, status = ?, updated_at = ? WHERE id = ?",
        (title[:300], body[:4000], status, now, row["id"]))
    return int(row["id"])

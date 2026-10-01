"""Break a file into chunks along its natural boundaries (Brain.md §4.1).

A chunk is about one idea: a Markdown section, a paragraph group, or one
top-level definition in code. A boundary is never placed inside a paragraph
or inside an indented block, so an idea is not cut in half; only a single
block larger than :data:`HARD_MAX_CHARS` is split, at a line boundary.

Code is split without a parser per language: a new chunk starts at a line
with no indentation that follows a blank line or closes the previous block.
That holds for Python, JavaScript, Go, Rust, shell and most config formats,
and costs nothing for the ones it fits less well.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from services.brain import text as tx

TARGET_CHARS = 1600      # grow a chunk up to about this size
HARD_MAX_CHARS = 6000    # a single block past this is split at a line
MIN_CHARS = 40           # smaller chunks carry no idea worth keeping

PROSE_SUFFIXES = {".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc", ".org", ""}
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SETEXT_RE = re.compile(r"^(=+|-+)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# Words that open a definition in most languages. Syntax, not subject
# matter: they only help pick the defined name out of the first line.
_DEF_WORDS = frozenset({
    "def", "class", "function", "func", "fn", "fun", "sub", "proc", "procedure", "method",
    "const", "let", "var", "val", "type", "interface", "struct", "enum", "trait", "impl",
    "module", "namespace", "object", "record", "export", "default", "pub", "public", "private",
    "protected", "static", "async", "abstract", "final", "override", "virtual", "extern", "inline",
    "data", "newtype", "macro", "package", "import", "from", "use", "include", "require",
})


@dataclass
class Chunk:
    start_line: int      # 1-based, inclusive
    end_line: int        # 1-based, inclusive
    kind: str            # section | paragraph | definition | block
    title: str
    text: str


def is_prose(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in PROSE_SUFFIXES


def chunk_file(path: str, content: str) -> list[Chunk]:
    lines = content.splitlines()
    if not lines:
        return []
    raw = _prose_chunks(lines) if is_prose(path) else _code_chunks(lines)
    out: list[Chunk] = []
    for chunk in raw:
        for piece in _enforce_max(chunk):
            if len(piece.text.strip()) >= MIN_CHARS:
                out.append(piece)
    return out


# ── prose ────────────────────────────────────────────────────────────────────


def _prose_chunks(lines: list[str]) -> list[Chunk]:
    """Sections by heading; a long section becomes paragraph groups that
    carry the section's heading as their title."""
    sections: list[tuple[int, int, str]] = []   # (start, end, heading) 0-based inclusive
    start, heading, in_fence = 0, "", False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        setext = (i + 1 < len(lines) and _SETEXT_RE.match(lines[i + 1]) and line.strip()
                  and not _HEADING_RE.match(line))
        if m or setext:
            if i > start:
                sections.append((start, i - 1, heading))
            start, heading = i, (m.group(2) if m else line.strip())
    sections.append((start, len(lines) - 1, heading))

    out: list[Chunk] = []
    for s, e, title in sections:
        body = "\n".join(lines[s:e + 1])
        if len(body) <= TARGET_CHARS:
            out.append(Chunk(s + 1, e + 1, "section" if title else "paragraph", title, body))
            continue
        for ps, pe in _group(_paragraphs(lines, s, e), lines):
            out.append(Chunk(ps + 1, pe + 1, "paragraph", title, "\n".join(lines[ps:pe + 1])))
    return out


def _paragraphs(lines: list[str], s: int, e: int) -> list[tuple[int, int]]:
    """Blank-line separated blocks; a fenced block is one paragraph."""
    out, cur, in_fence = [], None, False
    for i in range(s, e + 1):
        line = lines[i]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        blank = not line.strip() and not in_fence
        if blank:
            if cur is not None:
                out.append((cur, i - 1))
                cur = None
        elif cur is None:
            cur = i
    if cur is not None:
        out.append((cur, e))
    return out


def _group(blocks: list[tuple[int, int]], lines: list[str]) -> list[tuple[int, int]]:
    """Merge neighbouring blocks until a group reaches :data:`TARGET_CHARS`."""
    out: list[tuple[int, int]] = []
    cur_s, cur_e, size = None, None, 0
    for bs, be in blocks:
        bsize = sum(len(lines[i]) + 1 for i in range(bs, be + 1))
        if cur_s is not None and size + bsize > TARGET_CHARS:
            out.append((cur_s, cur_e))
            cur_s, size = None, 0
        if cur_s is None:
            cur_s = bs
        cur_e, size = be, size + bsize
    if cur_s is not None:
        out.append((cur_s, cur_e))
    return out


# ── code ─────────────────────────────────────────────────────────────────────


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _code_chunks(lines: list[str]) -> list[Chunk]:
    """Top-level blocks: a block starts at an unindented line that follows a
    blank line or a closing line; a comment or decorator directly above it
    stays with it."""
    starts = [0]
    for i in range(1, len(lines)):
        line = lines[i]
        if not line.strip() or _indent(line) > 0:
            continue
        prev = lines[i - 1]
        closes = prev.strip() in {"}", "};", ")", "];", "end", "fi", "done", "esac"}
        if not prev.strip() or closes:
            # keep a leading comment/decorator run attached
            j = i
            while j - 1 > starts[-1] and lines[j - 1].strip() and _indent(lines[j - 1]) == 0 \
                    and _is_lead(lines[j - 1]):
                j -= 1
            if j > starts[-1]:
                starts.append(j)
    starts.append(len(lines))

    blocks = [(starts[k], starts[k + 1] - 1) for k in range(len(starts) - 1)]
    out: list[Chunk] = []
    pending: list[tuple[int, int]] = []

    def flush() -> None:
        if not pending:
            return
        s, e = pending[0][0], pending[-1][1]
        out.append(Chunk(s + 1, e + 1, "block", "", "\n".join(lines[s:e + 1])))
        pending.clear()

    for s, e in blocks:
        while e > s and not lines[e].strip():
            e -= 1
        title = definition_name(lines[s:e + 1])
        body = "\n".join(lines[s:e + 1])
        if title:
            flush()
            out.append(Chunk(s + 1, e + 1, "definition", title, body))
            continue
        # Undefined top-level code (imports, constants) groups with its neighbours.
        if pending and sum(len("\n".join(lines[a:b + 1])) for a, b in pending) + len(body) > TARGET_CHARS:
            flush()
        pending.append((s, e))
    flush()
    return out


def _is_lead(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(("#", "//", "/*", "*", "@", "--", ";", '"""', "'''"))


_DEF_LINE_RE = re.compile(
    r"^([A-Za-z_][\w.]*(?:[\s*&]+[A-Za-z_][\w.]*)*)\s*(?:\(|=|\{|:|<|\bextends\b|\bimplements\b)")
# Statements that open a block without defining anything.
_CONTROL_WORDS = frozenset({
    "if", "elif", "else", "for", "while", "do", "return", "try", "with", "switch", "case", "match",
    "when", "unless", "until", "assert", "raise", "throw", "print", "echo", "yield", "await",
})


def definition_name(block: list[str]) -> str:
    """The name a block defines, from its first code line: the identifier
    right before ``(``, ``=``, ``{``, ``:`` or ``<``, skipping definition
    words. ``""`` when the block defines nothing nameable (a statement, a
    bare import, a constant in capitals)."""
    for line in block:
        stripped = line.strip()
        if not stripped or _is_lead(line):
            continue
        m = _DEF_LINE_RE.match(stripped)
        if not m:
            return ""
        tokens = re.findall(r"[A-Za-z_]\w*", m.group(1))
        if not tokens or tokens[0].lower() in _CONTROL_WORDS:
            return ""
        names = [t for t in tokens if t.lower() not in _DEF_WORDS]
        if not names:
            return ""
        name = names[-1].split(".")[-1]
        if len(name) < 2 or name.isupper():
            return ""
        return name
    return ""


def _enforce_max(chunk: Chunk) -> list[Chunk]:
    if len(chunk.text) <= HARD_MAX_CHARS:
        return [chunk]
    out, buf, start = [], [], chunk.start_line
    size = 0
    for offset, line in enumerate(chunk.text.splitlines()):
        if size + len(line) > HARD_MAX_CHARS and buf:
            out.append(Chunk(start, start + len(buf) - 1, chunk.kind, chunk.title, "\n".join(buf)))
            start, buf, size = chunk.start_line + offset, [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        out.append(Chunk(start, start + len(buf) - 1, chunk.kind, chunk.title, "\n".join(buf)))
    return out


def title_phrase(chunk: Chunk) -> str:
    """The chunk's title as a concept name (identifiers split)."""
    return tx.display_name(chunk.title) if chunk.title else ""

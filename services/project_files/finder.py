"""Finding files in a project: every path (``GET /paths``, for ⌘P and the
tree's find) and every line that matches (``GET /search``, for ⌘⇧F),
contract §4.6 and §4.7.

The Files tree loads one folder per request, so a name in a closed folder
can't be found from what the tree holds: these answer for the whole root at
once. A root is the project's checkout or one thread worktree
(``.xo/worktrees/<name>``), and paths are relative to it (``root`` says
which). In a git repository git decides what is listed and searched
(tracked plus untracked, minus what is ignored), through XO's hardened git
(``services.git_safe``); elsewhere a walk that never follows a link stands
in for it. Either way the Space's ignore set (``address.is_noise``: XO's own
``.xo/`` records, build and install folders, Space's temp files) is left
out, so ⌘P never offers ``.xo/project.json``.

A search's pattern reaches git on stdin (``-f -``), never its command line,
so it is in no process list or command log, and can't be read as an
option. Its output is read as it comes, so the limit and the time budget
stop it early (``truncated``).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.git_safe import git_bytes, git_streaming
from services.project_files.address import FileRefused, is_noise, noise_pathspecs, project_root
from services.xo_structure import WORKTREES_DIRNAME, XO_DIRNAME

PATHS_MAX = 50_000
SEARCH_BUDGET_S = 5.0
QUERY_MAX = 500
LIMIT_DEFAULT = 500
LIMIT_MAX = 2_000
#: How much of a matching line is sent, around its first match.
TEXT_MAX = 400
#: How much of the line before the first match a cut line keeps.
TEXT_LEAD = 120
#: Outside git, larger files aren't searched.
WALK_FILE_MAX = 8 * 1024 * 1024
#: The repository settings a search must not take from the project: how a
#: pattern is read, how paths are printed, colours, a fallback outside it.
_GREP_PINNED = ("-c", "grep.patternType=default", "-c", "grep.extendedRegexp=false", "-c", "grep.fullName=false",
                "-c", "grep.fallbackToNoIndex=false", "-c", "grep.lineNumber=false", "-c", "grep.column=false",
                "-c", "color.grep=never", "-c", "core.quotePath=false")


@dataclass(frozen=True)
class Root:
    project_id: str
    project: Path
    folder: Path
    #: Project-relative: ``""`` or ``.xo/worktrees/<name>``.
    rel: str

    def project_rel(self, inner: str) -> str:
        return f"{self.rel}/{inner}" if self.rel else inner


def root_of(project_id: str, worktree: str | None) -> Root:
    pid, project = project_root(project_id)
    if worktree is None or worktree == "":
        return Root(pid, project, project, "")
    if "/" in worktree or "\\" in worktree or worktree in (".", "..") or "\x00" in worktree:
        raise FileRefused("invalid_path", "worktree must be the name of one of this project's worktrees.",
                          status=400)
    folder = project / XO_DIRNAME / WORKTREES_DIRNAME / worktree
    try:
        st = os.lstat(folder)
    except OSError:
        st = None
    if st is None or not stat.S_ISDIR(st.st_mode):
        raise FileRefused("not_found", "There is no such worktree in this project (it may have been removed "
                                       "when its thread finished).", status=404)
    return Root(pid, project, folder, f"{XO_DIRNAME}/{WORKTREES_DIRNAME}/{worktree}")


def _env(root: Root) -> dict[str, str]:
    return {"GIT_CEILING_DIRECTORIES": str(root.folder.parent)}


async def _is_repo(root: Root) -> bool:
    """Whether the root is the top of a git checkout (not just inside one)."""
    code, out = await git_bytes(root.folder, "rev-parse", "--show-toplevel", env=_env(root))
    return code == 0 and Path(os.fsdecode(out.strip())).resolve() == root.folder.resolve()


# ── /paths ────────────────────────────────────────────────────────────────────


async def paths(project_id: str, worktree: str | None = None, stamp: str | None = None) -> dict[str, Any]:
    root = root_of(project_id, worktree)
    if await _is_repo(root):
        listed, truncated = await _git_paths(root)
        listed = await asyncio.to_thread(lambda: [p for p in listed if os.path.lexists(root.folder / p)])
    else:
        listed, truncated = await asyncio.to_thread(_walk_paths, root)
    listed = sorted(set(listed))[:PATHS_MAX]
    new_stamp = hashlib.sha256("\0".join(listed).encode("utf-8", errors="surrogateescape")).hexdigest()[:32]
    if truncated:
        new_stamp += "+"
    if stamp is not None and stamp == new_stamp:
        return {"unchanged": True, "stamp": new_stamp}
    dirs = sorted({d for p in listed for d in _parents(p)})
    return {"root": root.rel, "paths": listed, "dirs": dirs, "truncated": truncated, "stamp": new_stamp}


def _parents(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


async def _git_paths(root: Root) -> tuple[list[str], bool]:
    out: list[str] = []
    truncated = False
    async with git_streaming(root.folder, "ls-files", "-c", "-o", "--exclude-standard", "--deduplicate", "-z",
                             "--", *noise_pathspecs(), env=_env(root)) as proc:
        assert proc.stdout is not None
        rest = b""
        while chunk := await proc.stdout.read(1 << 16):
            items = (rest + chunk).split(b"\0")
            rest = items.pop()
            for item in items:
                name = os.fsdecode(item)
                if not name or name.endswith("/") or is_noise(root.project_rel(name)):
                    continue                         # a nested repository (listed as its folder), or noise
                out.append(name)
                if len(out) > PATHS_MAX:
                    truncated = True
                    break
            if truncated:
                break
        if not truncated:
            code = await proc.wait()
            if code != 0:
                raise FileRefused("paths_unavailable", "git couldn't list this folder's files.", status=500)
    return out[:PATHS_MAX], truncated


def _walk_paths(root: Root) -> tuple[list[str], bool]:
    out: list[str] = []
    for inner in _walk(root):
        out.append(inner)
        if len(out) > PATHS_MAX:
            return out[:PATHS_MAX], True
    return out, False


def _walk(root: Root):
    """Every file under the root, relative to it, never through a link
    folder, without what ``is_noise`` names."""
    for current, dirnames, filenames in os.walk(root.folder, followlinks=False):
        base = Path(current).relative_to(root.folder).as_posix()
        prefix = "" if base == "." else base + "/"
        dirnames[:] = sorted(d for d in dirnames if not is_noise(root.project_rel(prefix + d))
                             and not os.path.islink(os.path.join(current, d)))
        for name in sorted(filenames):
            inner = prefix + name
            if not is_noise(root.project_rel(inner)):
                yield inner


# ── /search ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Query:
    q: str
    regex: bool = False
    case: bool = False
    word: bool = False
    globs: tuple[str, ...] = ()
    limit: int = LIMIT_DEFAULT


def parse_query(q: str | None, *, regex: bool, case: bool, word: bool, glob: str | None, limit: int) -> Query:
    if q is None or not q.strip():
        raise FileRefused("query_empty", "Type something to search for.", status=400)
    if len(q) > QUERY_MAX:
        raise FileRefused("invalid_query", f"A search is at most {QUERY_MAX} characters.", status=400)
    if "\n" in q or "\r" in q or "\x00" in q:
        raise FileRefused("invalid_query", "Search matches one line at a time, so the query can't hold a line "
                                           "break.", status=400)
    globs = tuple(g.strip() for g in (glob or "").split(",") if g.strip())
    return Query(q, regex, case, word, globs, max(1, min(limit, LIMIT_MAX)))


def _python_pattern(query: Query) -> re.Pattern[str] | None:
    """The query as a Python pattern, to find each match's place in its
    line (git says only where the first starts); None when Python can't
    read a pattern git can."""
    body = query.q if query.regex else re.escape(query.q)
    if query.word:
        body = rf"(?<!\w)(?:{body})(?!\w)"
    try:
        return re.compile(body, 0 if query.case else re.IGNORECASE)
    except re.error:
        return None


def _ranges(pattern: re.Pattern[str] | None, text: str) -> list[list[int]]:
    if pattern is None:
        return []
    return [[m.start(), m.end()] for m in pattern.finditer(text) if m.end() > m.start()]


def _cut(text: str, ranges: list[list[int]], first: int) -> tuple[str, list[list[int]]]:
    if len(text) <= TEXT_MAX:
        return text, ranges
    start = max(0, min(first - TEXT_LEAD, len(text) - TEXT_MAX))
    end = start + TEXT_MAX
    kept = [[max(a, start) - start, min(b, end) - start] for a, b in ranges if b > start and a < end]
    return text[start:end], kept


def _match(path: str, line: int, raw: bytes, byte_col: int | None, pattern: re.Pattern[str] | None
           ) -> dict[str, Any]:
    raw = raw[:-1] if raw.endswith(b"\r") else raw
    text = raw.decode("utf-8", errors="replace")
    ranges = _ranges(pattern, text)
    if byte_col is not None and byte_col >= 1:
        first = len(raw[: byte_col - 1].decode("utf-8", errors="replace"))
    else:
        first = ranges[0][0] if ranges else 0
    text, ranges = _cut(text, ranges, first)
    return {"path": path, "line": line, "col": first + 1, "text": text, "ranges": ranges}


_PCRE: bool | None = None


async def _perl_regex() -> bool:
    """Whether this git reads Perl-style patterns (``-P``, closest to what an
    editor's regex search takes); else ``-E``. Asked once, in an empty
    folder of its own."""
    global _PCRE
    if _PCRE is None:
        with tempfile.TemporaryDirectory() as empty:
            code, _ = await git_bytes(Path(empty), "grep", "--no-index", "-P", "-q", "-e", "x")
        _PCRE = code in (0, 1)
    return _PCRE


async def search(project_id: str, query: Query, worktree: str | None = None) -> dict[str, Any]:
    root = root_of(project_id, worktree)
    started = time.monotonic()
    pattern = _python_pattern(query)
    if await _is_repo(root):
        matches, files, truncated = await _git_search(root, query, pattern, started)
    else:
        if pattern is None:
            raise FileRefused("bad_regex", "This pattern isn't a valid regular expression.", status=400)
        matches, files, truncated = await asyncio.to_thread(_walk_search, root, query, pattern, started)
    return {"root": root.rel, "matches": matches, "files": files, "truncated": truncated,
            "took_ms": int((time.monotonic() - started) * 1000)}


async def _git_search(root: Root, query: Query, pattern: re.Pattern[str] | None, started: float
                      ) -> tuple[list[dict[str, Any]], int, bool]:
    args = [*_GREP_PINNED, "grep", "-n", "--column", "-I", "-z", "--no-color", "--no-textconv", "--untracked",
            "--exclude-standard"]
    if query.regex:
        args.append("-P" if await _perl_regex() else "-E")
    else:
        args.append("-F")
    if not query.case:
        args.append("-i")
    if query.word:
        args.append("-w")
    args += ["-f", "-", "--", *query.globs, *noise_pathspecs()]
    matches: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    truncated = False
    deadline = started + SEARCH_BUDGET_S
    async with git_streaming(root.folder, *args, env=_env(root), input=query.q.encode("utf-8") + b"\n") as proc:
        assert proc.stdout is not None and proc.stderr is not None
        errors = asyncio.ensure_future(proc.stderr.read())
        buffer = b""
        done = False
        try:
            while not done:
                left = deadline - time.monotonic()
                if left <= 0:
                    truncated = True
                    break
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(1 << 16), timeout=left)
                except asyncio.TimeoutError:
                    truncated = True
                    break
                if not chunk:
                    break
                buffer += chunk
                while True:
                    record = _next_record(buffer)
                    if record is None:
                        break
                    (path, line, col, raw), used = record
                    buffer = buffer[used:]
                    if is_noise(root.project_rel(path)):
                        continue
                    if len(matches) >= query.limit:
                        truncated = done = True
                        break
                    seen_files.add(path)
                    matches.append(_match(path, line, raw, col, pattern))
            if not truncated:
                code = await proc.wait()
                if code not in (0, 1):
                    why = (await errors).decode("utf-8", errors="replace").strip().splitlines()
                    message = why[-1].removeprefix("fatal: ") if why else "git couldn't search here."
                    if query.regex:
                        raise FileRefused("bad_regex", message, status=400)
                    raise FileRefused("search_failed", message, status=500)
        finally:
            errors.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await errors
    return matches, len(seen_files), truncated


def _next_record(buffer: bytes) -> tuple[tuple[str, int, int | None, bytes], int] | None:
    """One ``path\\0line\\0col\\0text\\n`` record from the buffer's start."""
    a = buffer.find(b"\0")
    b = buffer.find(b"\0", a + 1) if a >= 0 else -1
    c = buffer.find(b"\0", b + 1) if b >= 0 else -1
    end = buffer.find(b"\n", c + 1) if c >= 0 else -1
    if end < 0:
        return None
    try:
        line = int(buffer[a + 1:b])
    except ValueError:
        line = 0
    try:
        col: int | None = int(buffer[b + 1:c])
    except ValueError:
        col = None
    return (os.fsdecode(buffer[:a]), line, col, buffer[c + 1:end]), end + 1


def _walk_search(root: Root, query: Query, pattern: re.Pattern[str], started: float
                 ) -> tuple[list[dict[str, Any]], int, bool]:
    matches: list[dict[str, Any]] = []
    files = 0
    for inner in _walk(root):
        if time.monotonic() - started > SEARCH_BUDGET_S:
            return matches, files, True
        if query.globs and not any(fnmatch.fnmatchcase(inner, g) for g in query.globs):
            continue
        path = root.folder / inner
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > WALK_FILE_MAX:
                continue
            with os.fdopen(os.dup(fd), "rb") as handle:
                data = handle.read(WALK_FILE_MAX + 1)
        except OSError:
            continue
        finally:
            os.close(fd)
        if b"\x00" in data[:8192]:
            continue
        hit = False
        for number, raw in enumerate(data.split(b"\n"), start=1):
            text = raw.decode("utf-8", errors="replace")
            if pattern.search(text) is None:
                continue
            if len(matches) >= query.limit:
                return matches, files + (1 if hit else 0), True
            hit = True
            matches.append(_match(inner, number, raw, None, pattern))
        files += 1 if hit else 0
    return matches, files, False

"""One project file's text, for preview and for the editor (``GET /file``).

Text is judged by content, not by name (owner, 5 Nov 2026: every text file
is editable): a file is text when its first 8 KB hold no NUL byte and decode
as UTF-8. The name's suffix still picks how it is shown (markdown, html,
text), and the suffixes the previewer always showed keep their preview.

Every answer for the working tree carries the file's **version** (the
SHA-256 of its exact bytes) when it shows the whole file and the file is
small enough to edit (a cut preview has none, so no save can start from a
cut copy), and says whether the editor may open it and, if not, why. A preview is cut at ``PREVIEW_MAX_BYTES``; ``purpose="edit"`` reads up
to ``EDIT_MAX_BYTES`` whole, or refuses (413), so an editor never starts
from a cut copy. A BOM is reported (``bom``) and left out of ``content``; a
save puts it back.
"""

from __future__ import annotations

import codecs
import os
from dataclasses import dataclass
from pathlib import PurePosixPath

from services.project_files.address import (
    EDIT_MAX_BYTES,
    FileRefused,
    Place,
    file_not_found,
    locate,
    open_file,
    parent_dir,
    read_fd,
    version_of,
)

PREVIEW_MAX_BYTES = 256 * 1024
#: How much of a file decides whether it is text.
SNIFF_BYTES = 8 * 1024
BOM = codecs.BOM_UTF8
#: The suffixes the previewer always showed, whatever their bytes.
PREVIEW_SUFFIXES = frozenset(
    {
        ".md", ".markdown", ".mdx",
        ".html", ".htm",
        ".txt", ".text", ".rst", ".log",
        ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env.example",
        ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".css", ".sh", ".sql",
        ".go", ".rs", ".java", ".c", ".h", ".cpp",
    }
)

PURPOSES = ("preview", "edit")


def suffix(rel: str) -> str:
    return PurePosixPath(rel).suffix.lower()


def kind_of(name: str) -> str:
    lower = name.lower()
    if lower.endswith((".md", ".markdown", ".mdx")):
        return "markdown"
    if lower.endswith((".html", ".htm")):
        return "html"
    return "text"


def looks_like_text(sample: bytes) -> bool:
    """No NUL, and valid UTF-8 as far as it goes (a character cut at the
    sample's end doesn't count against it)."""
    if b"\x00" in sample:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        return False
    return True


def is_utf8_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def line_endings(data: bytes) -> str:
    """``lf``, ``crlf``, ``mixed``, or ``none`` (a single line)."""
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    if crlf and lf:
        return "mixed"
    return "crlf" if crlf else "lf" if lf else "none"


def previewable(rel: str, sample: bytes) -> bool:
    return suffix(rel) in PREVIEW_SUFFIXES or looks_like_text(sample)


def unsupported(rel: str) -> FileRefused:
    s = suffix(rel)
    return FileRefused("preview_unsupported", f"No text preview for {s or 'this file'}: it isn't text.",
                       status=415)


@dataclass(frozen=True)
class FileText:
    project_id: str
    relative_path: str
    name: str
    kind: str
    size_bytes: int
    modified_at: float | None
    truncated: bool
    content: str
    version: str | None
    editable: bool
    not_editable_reason: str | None
    place: str
    bom: bool
    line_endings: str


def read_text(project_id: str, relative_path: str, *, purpose: str = "preview") -> FileText:
    """The working tree's version of one file (see the module docstring).
    ``FileRefused``: 404 project or file, 400 path, 403 outside the project,
    415 not text, 413 too large to edit (``purpose="edit"``)."""
    target = locate(project_id, relative_path)
    with parent_dir(target) as folder:
        fd = open_file(folder, target.real_rel.rsplit("/", 1)[-1])
        if fd is None:
            raise file_not_found()
        try:
            st = os.fstat(fd)
            if purpose == "edit" and st.st_size > EDIT_MAX_BYTES:
                raise FileRefused("too_large_to_edit", f"Files over {EDIT_MAX_BYTES // (1024 * 1024)} MB can't "
                                                       "be edited here.", status=413)
            whole = st.st_size <= EDIT_MAX_BYTES
            cap = EDIT_MAX_BYTES if purpose == "edit" else PREVIEW_MAX_BYTES
            data = read_fd(fd, (EDIT_MAX_BYTES if whole else cap) + 1)
        except FileNotFoundError:
            raise file_not_found() from None
        finally:
            os.close(fd)
    if not previewable(target.rel, data[:SNIFF_BYTES]):
        raise unsupported(target.rel)

    # The file may have grown between fstat and read: only bytes actually
    # read whole, within the edit cap, make a version.
    complete = whole and len(data) <= EDIT_MAX_BYTES
    # A cut preview carries no version: a save must start from the whole file.
    version = version_of(data) if complete and len(data) <= cap else None
    bom = data.startswith(BOM)
    shown = data[:cap]
    truncated = len(data) > cap
    reason = _not_editable(target.place, complete, data if complete else None, truncated)
    body = shown[len(BOM):] if bom else shown
    return FileText(
        project_id=target.project_id,
        relative_path=target.rel,
        name=target.name,
        kind=kind_of(target.name),
        size_bytes=len(data) if complete else st.st_size,
        modified_at=st.st_mtime,
        truncated=truncated,
        content=body.decode("utf-8", errors="replace"),
        version=version,
        editable=reason is None,
        not_editable_reason=reason,
        place=target.place.label,
        bom=bom,
        line_endings=line_endings(shown),
    )


def _not_editable(place: Place, complete: bool, data: bytes | None, truncated: bool) -> str | None:
    if place is Place.READ_ONLY:
        return "read_only_place"
    if not complete or data is None:
        return "too_large"
    if b"\x00" in data:
        return "not_text"
    if not is_utf8_text(data):
        return "not_utf8"
    if truncated:
        return "truncated"
    return None

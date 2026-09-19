"""One JSON document on disk, read and changed the way every store must.

Every store that owns a document used to write its own version of the same
five rules. They live here once:

1. An absent file reads as the empty document; nothing is created by a read.
2. A file that is not JSON is never rewritten: :meth:`read` answers
   ``ok=False`` and :meth:`modify` raises :class:`CorruptDocument` (a 409
   whose wire message names the document kind, never the path; the path
   goes to the log).
3. A document stamped with a newer ``schema`` is refused the same way
   (:class:`UnsupportedSchema`), so a key a newer version added is never
   dropped by an older one.
4. A change is a locked read-modify-write: :meth:`modify` takes the
   advisory lock, reads, normalises, hands the document to ``fn``, and
   writes only when ``fn`` says something changed, stamping ``schema`` and
   ``updated_at``.
5. Unknown keys survive: the normaliser is asked to keep what it does not
   know, and the write is atomic.

::

    doc = Document(path, schema=1, empty=lambda: {"jobs": {}}, normalize=normalize, name="jobs.json")
    document, ok = doc.read()
    doc.modify(lambda d: d["jobs"].pop(job_id, None) is not None)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

from services.errors import Conflict
from services.storage.atomic_write import read_stamped_document, write_json_atomic
from services.storage.flock import locked
from services.timestamps import now_iso

logger = logging.getLogger(__name__)

Normalizer = Callable[[dict], dict]


#: The wire wording for a refused document, shared with the visualizer
#: stores that keep their own error types: ``name`` is the document as a
#: person knows it ("todos.json"), never its path.
CORRUPT_DOCUMENT_MESSAGE = (
    "{name} is not a readable document of its kind. It is refused rather "
    "than read as empty or overwritten, so nothing it holds is discarded. "
    "Repair or move the file."
)
UNSUPPORTED_SCHEMA_MESSAGE = (
    "{name} declares a schema version this Space does not write. It is "
    "refused rather than rewritten, so no key a newer version added is "
    "silently dropped. Upgrade the Space, or restore the document."
)


class CorruptDocument(Conflict):
    """The file exists and is not a readable document of its kind."""

    def __init__(self, name: str, path: Path, reason: str) -> None:
        super().__init__(
            "corrupt_document",
            CORRUPT_DOCUMENT_MESSAGE.format(name=name),
            log=f"{path}: {reason}",
        )
        self.path = path
        self.reason = reason


class UnsupportedSchema(Conflict):
    """The file declares a schema this Space does not write."""

    def __init__(self, name: str, path: Path, found: Any, expected: int) -> None:
        super().__init__(
            "unsupported_schema",
            UNSUPPORTED_SCHEMA_MESSAGE.format(name=name),
            log=f"{path}: schema {found!r}, this Space writes {expected}",
        )
        self.path = path
        self.found = found


class Document:
    """A schema-stamped JSON object at ``path``.

    ``empty`` builds the document a first write starts from (without the
    ``schema`` key; it is stamped on write). ``normalize`` takes a parsed
    dict, fills defaults and repairs shapes, and must keep keys it does not
    know. ``name`` is how the document is called in a message a person may
    see ("jobs.json", "config.json for gmail"). ``private`` makes the file
    owner-only, for documents that may hold a credential.
    """

    def __init__(
        self,
        path: Path,
        *,
        schema: int,
        empty: Callable[[], dict],
        normalize: Optional[Normalizer] = None,
        name: Optional[str] = None,
        private: bool = False,
    ) -> None:
        self.path = Path(path)
        self.schema = int(schema)
        self._empty = empty
        self._normalize = normalize or (lambda doc: doc)
        self.name = name or self.path.name
        self.private = private

    # ── reads ────────────────────────────────────────────────────────────

    def _load(self) -> tuple[str, Any]:
        return read_stamped_document(self.path, schema=self.schema)

    def read(self) -> tuple[dict, bool]:
        """``(document, ok)``. Absent reads as empty with ``ok`` true; a file
        that is not JSON reads as empty with ``ok`` false (and a warning);
        a newer schema raises :class:`UnsupportedSchema`."""
        state, value = self._load()
        if state == "absent":
            return self._normalize(self._empty()), True
        if state == "fault":
            logger.warning("%s: %s is not readable (%s); serving it empty, never rewriting it",
                           self.name, self.path, value)
            return self._normalize(self._empty()), False
        if state == "schema":
            raise UnsupportedSchema(self.name, self.path, value, self.schema)
        return self._normalize(dict(value)), True

    def require(self) -> dict:
        """:meth:`read`, but a corrupt file raises :class:`CorruptDocument`."""
        state, value = self._load()
        if state == "absent":
            return self._normalize(self._empty())
        if state == "fault":
            raise CorruptDocument(self.name, self.path, str(value))
        if state == "schema":
            raise UnsupportedSchema(self.name, self.path, value, self.schema)
        return self._normalize(dict(value))

    def exists(self) -> bool:
        return self.path.is_file()

    # ── writes ───────────────────────────────────────────────────────────

    def _write(self, document: dict, *, stamp: bool) -> None:
        document["schema"] = self.schema
        if stamp:
            document["updated_at"] = now_iso()
        write_json_atomic(self.path, document)
        if self.private:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def modify(self, fn: Callable[[dict], Any], *, stamp: bool = True) -> dict:
        """Locked read-modify-write. ``fn`` edits the document in place and
        returns whether anything changed; the file is written only then, so
        a read-only pass never creates it. Returns the document."""
        with locked(self.path):
            document = self.require()
            if fn(document):
                self._write(document, stamp=stamp)
        return document

    def write(self, document: dict, *, stamp: bool = True) -> dict:
        """Replace the whole document (a writer that owns it entirely)."""
        with locked(self.path):
            document = self._normalize(dict(document))
            self._write(document, stamp=stamp)
        return document

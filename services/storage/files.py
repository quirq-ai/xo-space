"""The file table: how a module declares every file it writes.

A module's ``store.py`` lists a :class:`File` per path pattern it owns. The
registry unions the tables; from them the sample state root's README, the
layout test and the "delete it and you lose" column are derived instead of
kept by hand.

::

    FILES = [
        File("connections/<toolkit>/config.json", role="decision", schema="connections-config"),
        File("connections/<toolkit>/events.jsonl", role="record", log=True, rotate="2 MB, keep 3"),
    ]

``pattern`` is relative to the state root (``tier="local"``) or to a
project's ``.xo/`` (``tier="committed"``); ``<name>`` segments match one
path component. ``role`` says what the file is:

* ``record``   what happened; append-only; nothing rebuilds it
* ``fact``     a copy of state that lives elsewhere and can be fetched again
* ``decision`` what a person chose
* ``cache``    derived from other files here; delete freely
* ``secret``   credentials
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

ROLES = ("record", "fact", "decision", "cache", "secret")
TIERS = ("local", "committed")

LOSE = {
    "record": "history nothing rebuilds",
    "fact": "nothing; it is fetched again",
    "decision": "choices you would enter again",
    "cache": "nothing; rebuilt automatically",
    "secret": "credentials",
}


@dataclass(frozen=True)
class File:
    pattern: str
    role: str
    schema: Optional[str] = None
    tier: str = "local"
    log: bool = False
    rotate: Optional[str] = None
    keep: Optional[str] = None
    note: Optional[str] = None

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"File({self.pattern!r}): role must be one of {ROLES}, not {self.role!r}")
        if self.tier not in TIERS:
            raise ValueError(f"File({self.pattern!r}): tier must be one of {TIERS}, not {self.tier!r}")
        if not self.pattern or self.pattern.startswith("/") or ".." in self.pattern.split("/"):
            raise ValueError(f"File pattern must be relative: {self.pattern!r}")

    @property
    def folder(self) -> str:
        """The top-level folder the pattern lives in."""
        return self.pattern.split("/", 1)[0]

    def regex(self) -> "re.Pattern[str]":
        """``<name>`` matches one path component or a part of one
        (``runs/<id>.jsonl``), ``*`` one component, ``**`` any depth."""
        parts = []
        for segment in self.pattern.split("/"):
            if segment == "*":
                parts.append(r"[^/]+")
            elif segment == "**":
                parts.append(r".+")
            else:
                pieces = re.split(r"(<[^<>/]+>)", segment)
                parts.append("".join(r"[^/]+" if piece.startswith("<") else re.escape(piece)
                                     for piece in pieces if piece))
        return re.compile("^" + "/".join(parts) + "$")

    def matches(self, relative_path: str) -> bool:
        return self.regex().match(relative_path.replace("\\", "/")) is not None

    @property
    def lose(self) -> str:
        return LOSE[self.role]

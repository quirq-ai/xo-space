"""XO's own message tags in an agent's output (the plan's W8).

XO hands an agent its inputs in envelopes: ``<wake>``, ``<relay>``,
``<handoff>``, ``<continuation>``, ``<cited>``, ``<review-answered>``. An
agent has no reason to open one itself, and a model that does is writing
itself a request nobody sent. ``EnvelopeScanner`` finds such a tag at the
start of a line of the root turn's text as it streams, however the text is
cut into deltas; the engine then refuses the turn's further calls and
interrupts it (``LiveSession``, ``Engine.stop_forged_turn``), and the chats
layer tells the founder (``chats/authority``).

Only the tag's name is kept as evidence. The scanner holds a bounded prefix
of the line it is on, never the model's output.
"""

from __future__ import annotations

import re

#: The envelopes XO writes around an agent's input.
TAGS = ("wake", "relay", "handoff", "continuation", "cited", "review-answered")
#: One opened at the start of a line; a mention inside a sentence isn't one.
_OPENED = re.compile(rf"^[ \t]*<({'|'.join(TAGS)})(?=[\s>])")
#: How much of a line is looked at: a tag opens within its first characters.
_PREFIX = 128


class EnvelopeScanner:
    """Fed a text block's deltas in order, says the first XO tag a line
    opens with."""

    def __init__(self) -> None:
        self._prefix = ""
        self._past = False              # the line is longer than the prefix: nothing more to see on it

    def feed(self, text: str) -> str | None:
        """The XO tag ``text`` completes at the start of a line, or None."""
        for piece in text.splitlines(keepends=True):
            if not self._past:
                self._prefix = (self._prefix + piece)[:_PREFIX]
                if (found := _OPENED.match(self._prefix)) is not None:
                    return found[1]
                self._past = len(self._prefix) >= _PREFIX
            if piece.endswith(("\n", "\r")):
                self._prefix, self._past = "", False
        return None

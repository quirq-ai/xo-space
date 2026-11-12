"""Recognize XO envelope tags in an agent's own output, across streamed chunks.

Only the tag name is retained as evidence. The scanner retains a bounded
prefix of the current line; it never stores complete model output.
"""

import re

TAG = re.compile(r"^[ \t]*<(wake|relay|handoff|continuation|cited|review-answered)(?=[\s>])")


class EnvelopeScanner:
    def __init__(self):
        self.prefix = ""
        self.full = False

    def feed(self, text: str) -> str | None:
        for piece in text.splitlines(keepends=True):
            if not self.full:
                self.prefix = (self.prefix + piece)[:128]
                match = TAG.match(self.prefix)
                if match:
                    return match[1]
                self.full = len(self.prefix) >= 128
            if piece.endswith(("\n", "\r")):
                self.prefix, self.full = "", False
        return None

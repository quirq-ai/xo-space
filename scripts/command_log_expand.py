#!/usr/bin/env python3
"""Expand a command log's "output unchanged" references back to the log written in full, byte for byte."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

#: An entry's header lines: ``\\n=== <ts>[ <label>] ===``, ``$ <command>``,
#: ``cwd: <folder>``, ``[<status>; <seconds>s]``.
HEAD = re.compile(rb"\n=== (\S+)( [^\n]*)? ===\n\$ ([^\n]*)\ncwd: ([^\n]*)\n\[[^\n]*\]\n")
MARKER = re.compile(rb"\[output unchanged: as this command's entry above; sha256:([0-9a-f]{64}), (\d+) bytes; "
                    rb"at (\d+)\]\n")


def _key(match: re.Match) -> tuple[bytes, bytes, bytes]:
    return (match.group(2) or b""), match.group(3), match.group(4)


def expand(data: bytes, notes: list[str] | None = None) -> tuple[bytes, list[str]]:
    """``data`` with references expanded, and the unresolved ones; ``notes`` gets reference-like lines left as is."""
    out = bytearray()
    problems: list[str] = []
    heads: dict[tuple, list[int]] = {}      # key -> ends of its headers in ``out``, in order
    scanned = 0                             # ``out`` is indexed for headers up to here

    def index() -> None:
        nonlocal scanned
        start = max(0, scanned - 8192)      # a header may straddle the last chunk's end
        for m in HEAD.finditer(out, start):
            if m.end() > scanned:
                heads.setdefault(_key(m), []).append(m.end())
        scanned = len(out)

    pos = 0
    for marker in MARKER.finditer(data):
        if int(marker.group(3)) != marker.start():
            if notes is not None:           # a copy inside some output, or a reference moved by another writer
                before = data[max(0, marker.start() - 65536):marker.start()]
                if any(m.end() == len(before) for m in HEAD.finditer(before)):
                    notes.append(f"at {marker.start()}: a reference-shaped line after a header names offset "
                                 f"{int(marker.group(3))}; left as it is (a copy inside an output, or moved)")
            continue
        out += data[pos:marker.start()]
        pos = marker.end()
        index()
        digest, size = marker.group(1).decode(), int(marker.group(2))
        head = HEAD.search(out, max(0, len(out) - 65536))
        while head is not None and head.end() != len(out):
            head = HEAD.search(out, head.start() + 1)
        if head is None:
            problems.append(f"at {marker.start()}: no entry header right before the reference")
            out += marker.group(0)
            continue
        found = None
        for end in reversed(heads.get(_key(head), [])):
            if end >= head.end():
                continue                    # the reference's own header
            candidate = bytes(out[end:end + size])
            if len(candidate) == size and hashlib.sha256(candidate).hexdigest() == digest:
                found = candidate
                break
        if found is None:
            problems.append(f"at {marker.start()}: no output above with sha256 {digest[:12]}… ({size} bytes)")
            out += marker.group(0)
            continue
        out += found
    out += data[pos:]
    return bytes(out), problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("log", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--check", action="store_true", help="resolve every reference; write nothing")
    args = parser.parse_args(argv)
    data = args.log.read_bytes()
    notes: list[str] = []
    expanded, problems = expand(data, notes)
    for line in (*problems, *notes):
        print(f"{args.log}: {line}", file=sys.stderr)
    if args.check:
        refs = sum(1 for m in MARKER.finditer(data) if int(m.group(3)) == m.start())
        print(f"{args.log}: {refs} reference(s), {len(problems)} unresolved, {len(notes)} note(s); "
              f"{len(data)} bytes -> {len(expanded)}")
    elif args.output:
        args.output.write_bytes(expanded)
    else:
        sys.stdout.buffer.write(expanded)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

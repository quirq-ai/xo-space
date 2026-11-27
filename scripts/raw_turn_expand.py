#!/usr/bin/env python3
"""Restore a raw turn log's split lines to the (redacted) lines storm wrote, byte for byte."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

PREFIX = b'{"xo.raw"'


def expand_lines(lines: Iterable[bytes], problems: list[str] | None = None) -> Iterator[bytes]:
    """Each input line (with its newline) as the CLI's own line(s)."""
    blocks: dict[str, bytes] = {}
    for number, line in enumerate(lines, 1):
        if not line.startswith(PREFIX):
            yield line
            continue
        try:
            record = json.loads(line)
            kind = record["xo.raw"]
        except (ValueError, KeyError, TypeError):
            yield line                      # not one of the writer's own
            continue
        if kind == "line":
            yield record["bytes"].encode("latin-1") + b"\n"
        elif kind == "block":
            data = record["bytes"].encode("latin-1")
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                if problems is not None:
                    problems.append(f"line {number}: a block's bytes don't match its sha256")
            blocks[record["sha256"]] = data
        elif kind == "diff":
            missing = [name for name in record["blocks"] if name not in blocks]
            if missing:
                if problems is not None:
                    problems.append(f"line {number}: {len(missing)} block(s) not defined above, e.g. {missing[0][:12]}…")
                yield line
                continue
            yield (record["pre"].encode("latin-1") + b"".join(blocks[name] for name in record["blocks"])
                   + record["post"].encode("latin-1") + b"\n")
        else:
            if problems is not None:
                problems.append(f"line {number}: unknown xo.raw kind {kind!r}")
            yield line


def expand(data: bytes) -> tuple[bytes, list[str]]:
    problems: list[str] = []
    return b"".join(expand_lines(io.BytesIO(data), problems)), problems     # lines end at b"\n" only


def _lines(f: BinaryIO) -> Iterator[bytes]:
    yield from f


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("log", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--check", action="store_true", help="resolve everything; write nothing")
    args = parser.parse_args(argv)
    problems: list[str] = []
    size_in = args.log.stat().st_size
    size_out = 0
    with args.log.open("rb") as f:
        sink = None if args.check else (args.output.open("wb") if args.output else sys.stdout.buffer)
        try:
            for line in expand_lines(_lines(f), problems):
                size_out += len(line)
                if sink is not None:
                    sink.write(line)
        finally:
            if sink is not None and args.output:
                sink.close()
    for problem in problems:
        print(f"{args.log}: {problem}", file=sys.stderr)
    if args.check:
        print(f"{args.log}: {len(problems)} problem(s); {size_in} bytes -> {size_out}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

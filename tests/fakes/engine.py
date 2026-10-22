"""The fake-CLI engine: plays a scripted scenario over newline-delimited JSON.

Run as ``python -I engine.py --cli NAME --scenario FILE --record FILE -- ARGV...``
(``tests.fakes.install`` writes a shim that does this). Stdlib only: it runs
outside the project's venv imports, as the real CLI would.

A scenario is a JSON object:

``argv``
    A list of ``{"argv": [...], "stdout": "...", "stderr": "...", "exit": N}``.
    An invocation whose arguments equal one of them prints it and exits, as a
    model-free CLI call does (``--version``, ``auth status --json``).
``steps``
    Played in order for any other invocation:

    ``{"emit": OBJ}``            print OBJ as one JSON line
    ``{"emit_raw": "TEXT"}``     print TEXT as a line as it is (malformed data)
    ``{"expect": OBJ, "bind": {"NAME": "dotted.path"}}``
                                 read one stdin line; every key in OBJ must
                                 match (recursively); bind values for later
    ``{"stderr": "TEXT"}``       write a stderr line
    ``{"sleep": SECONDS}``
    ``{"exit": CODE}``
    ``{"on_signal": "SIGINT", "steps": [...], "exit": CODE}``
                                 from now on, that signal plays those steps
                                 and exits

    A string value ``"$NAME"`` anywhere in an emitted object is replaced by the
    bound value. When the steps run out the fake waits for stdin to close,
    then exits 0, like a CLI with nothing left to do.

The record file gets one JSON line per thing that happened: the invocation
(``argv``, ``cwd``, ``env``), each stdin line, each signal, the exit. A step
that doesn't match writes ``mismatch`` there and on stderr, and exits 97.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

MISMATCH_EXIT = 97


class Fake:
    def __init__(self, cli: str, scenario: dict, record_path: str, argv: list[str]) -> None:
        self.cli = cli
        self.scenario = scenario
        self.record_path = record_path
        self.argv = argv
        self.bound: dict[str, object] = {}

    def record(self, **entry: object) -> None:
        # Every line names its process, so runs that overlap never interleave
        # in ``FakeCli.invocations()``; each line goes in one append, so two
        # processes' lines never mix.
        data = (json.dumps({"pid": os.getpid(), **entry}) + "\n").encode("utf-8")
        fd = os.open(self.record_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)

    def out(self, line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def substitute(self, value: object) -> object:
        if isinstance(value, str) and value.startswith("$") and value[1:] in self.bound:
            return self.bound[value[1:]]
        if isinstance(value, dict):
            return {k: self.substitute(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.substitute(v) for v in value]
        return value

    def finish(self, code: int) -> None:
        self.record(exit=code)
        sys.stdout.flush()
        os._exit(code)

    def fail(self, step: int, why: str) -> None:
        self.record(mismatch={"step": step, "why": why})
        sys.stderr.write(f"fake {self.cli}: step {step}: {why}\n")
        sys.stderr.flush()
        self.finish(MISMATCH_EXIT)

    def read_line(self) -> str | None:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.rstrip("\n")
        try:
            self.record(stdin=json.loads(line))
        except ValueError:
            self.record(stdin_raw=line)
        return line

    def run(self) -> None:
        self.record(argv=self.argv, cwd=os.getcwd(), env=dict(os.environ))
        for fixed in self.scenario.get("argv", []):
            if fixed["argv"] == self.argv:
                if fixed.get("stdout"):
                    self.out(fixed["stdout"])
                if fixed.get("stderr"):
                    sys.stderr.write(fixed["stderr"] + "\n")
                self.finish(int(fixed.get("exit", 0)))
        for index, step in enumerate(self.scenario.get("steps", [])):
            self.play(index, step)
        while self.read_line() is not None:
            pass
        self.finish(0)

    def play(self, index: int, step: dict) -> None:
        if "emit" in step:
            self.out(json.dumps(self.substitute(step["emit"])))
        elif "emit_raw" in step:
            self.out(step["emit_raw"])
        elif "expect" in step:
            line = self.read_line()
            if line is None:
                self.fail(index, f"expected {json.dumps(step['expect'])}, stdin closed")
            try:
                got = json.loads(line)
            except ValueError:
                self.fail(index, f"expected JSON, got {line[:200]!r}")
            if not matches(step["expect"], got):
                self.fail(index, f"expected {json.dumps(step['expect'])}, got {line[:400]}")
            for name, path in step.get("bind", {}).items():
                self.bound[name] = dig(got, path)
        elif "stderr" in step:
            sys.stderr.write(step["stderr"] + "\n")
            sys.stderr.flush()
        elif "sleep" in step:
            time.sleep(float(step["sleep"]))
        elif "on_signal" in step:
            signum = getattr(signal, step["on_signal"])
            then, code = step.get("steps", []), int(step.get("exit", 0))

            def handler(_signum: int, _frame: object) -> None:
                self.record(signal=step["on_signal"])
                for inner in then:
                    self.play(index, inner)
                self.finish(code)

            signal.signal(signum, handler)
        elif "exit" in step:
            self.finish(int(step["exit"]))
        else:
            self.fail(index, f"unknown step {json.dumps(step)}")


def matches(expected: object, got: object) -> bool:
    """Every key of ``expected`` is in ``got`` with a matching value; lists
    and scalars compare whole."""
    if isinstance(expected, dict):
        return isinstance(got, dict) and all(k in got and matches(v, got[k]) for k, v in expected.items())
    return expected == got


def dig(obj: object, path: str) -> object:
    for part in path.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--record", required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    with open(args.scenario, encoding="utf-8") as f:
        scenario = json.load(f)
    Fake(args.cli, scenario, args.record, argv).run()


if __name__ == "__main__":
    main()

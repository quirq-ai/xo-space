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
    Played in order for any other invocation; or ``by_prompt``, a list of
    ``{"contains": TEXT, "steps": [...]}``: the first whose text is in the
    system prompt the CLI was given (claude: ``--append-system-prompt-file``)
    is played, so one fake can be several sessions' agents; ``probe``,
    ``{"flag": ARG, "steps": [...]}``, is played by a process whose argv has
    ARG (a runtime's models probe):

    ``{"emit": OBJ}``            print OBJ as one JSON line
    ``{"emit_raw": "TEXT"}``     print TEXT as a line as it is (malformed data)
    ``{"expect": OBJ, "bind": {"NAME": "dotted.path"}}``
                                 read one stdin line; every key in OBJ must
                                 match (recursively); bind values for later;
                                 ``"bind_re": {"NAME": ["dotted.path", "regex"]}``
                                 binds the regex's first group in that value
    ``{"stderr": "TEXT"}``       write a stderr line
    ``{"mcp": {"server": NAME, "tool": TOOL, "arguments": {...}}, "bind": {"NAME": "dotted.path"}}``
                                 call an MCP tool the way the CLI would: the
                                 server's URL and headers come from the CLI's
                                 own MCP config (claude: ``--mcp-config``),
                                 with ``${VAR}`` expanded from the env; bind
                                 values of the tool's JSON result. The
                                 outcome is recorded as ``mcp``
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
import re
import signal
import sys
import time
import urllib.error
import urllib.request

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
        for index, step in enumerate(self.steps()):
            self.play(index, step)
        while self.read_line() is not None:
            pass
        self.finish(0)

    def steps(self) -> list:
        if "by_prompt" not in self.scenario:
            return self.scenario.get("steps", [])
        probe = self.scenario.get("probe")
        if probe is not None and probe["flag"] in self.argv:
            return probe["steps"]
        prompt = ""
        if "--append-system-prompt-file" in self.argv:
            with open(self.argv[self.argv.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
                prompt = f.read()
        for entry in self.scenario["by_prompt"]:
            if entry["contains"] in prompt:
                return entry["steps"]
        self.fail(0, "no scenario for this system prompt")
        return []

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
            for name, (path, pattern) in step.get("bind_re", {}).items():
                found = re.search(pattern, str(dig(got, path)))
                if found is None:
                    self.fail(index, f"no {pattern!r} in {str(dig(got, path))[:300]}")
                self.bound[name] = found.group(1)
        elif "stderr" in step:
            sys.stderr.write(step["stderr"] + "\n")
            sys.stderr.flush()
        elif "mcp" in step:
            outcome = self.mcp_call(**self.substitute(step["mcp"]))
            self.record(mcp=outcome)
            for name, path in step.get("bind", {}).items():
                self.bound[name] = dig(outcome["result"], path)
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


    def mcp_call(self, *, server: str, tool: str, arguments: dict) -> dict:
        """POST one ``tools/call`` to the server the CLI was given. The
        outcome: the HTTP status, and the tool's result (its JSON text
        parsed) or its error text."""
        config = json.load(open(self.argv[self.argv.index("--mcp-config") + 1], encoding="utf-8"))
        entry = config["mcpServers"][server]
        headers = {k: re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), v)
                   for k, v in entry.get("headers", {}).items()}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        request = urllib.request.Request(entry["url"], data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, answer = response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return {"tool": tool, "status": exc.code, "error": exc.read().decode(errors="replace"), "result": None}
        result = answer["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return {"tool": tool, "status": status, "error": text, "result": None}
        return {"tool": tool, "status": status, "result": json.loads(text)}


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

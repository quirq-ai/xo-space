"""``python -m quirq <module> <command> [args]``: a module's commands from a shell.

A module's ``commands.py`` exposes ``COMMANDS = {"poll": fn}``; ``fn`` takes
the remaining arguments as a list and returns an exit code or a JSON-able
value (printed as JSON). ``python -m quirq`` alone lists every module with
its commands; a module whose ``commands`` switch is off answers "off in
Setup" with exit code 3. The same ``QUIRQ_STATE_ROOT`` and ``.env`` apply
as for the server.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO / ".env")
    except Exception:  # noqa: BLE001 - the CLI works without python-dotenv
        pass


def _usage(commands: dict) -> str:
    lines = ["usage: python -m quirq <module> <command> [args]", ""]
    for module, names in sorted(commands.items()):
        lines.append(f"  {module}: {', '.join(sorted(names)) or '(none)'}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    _load_env()
    from services import modules as registry

    commands = registry.commands()
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(_usage(commands))
        return 0 if argv else 2
    module, *rest = argv
    if module not in commands:
        print(f"unknown module {module!r}\n\n{_usage(commands)}", file=sys.stderr)
        return 2
    if not rest:
        print(f"{module}: {', '.join(sorted(commands[module])) or '(no commands)'}")
        return 0
    name, *args = rest
    if name not in commands[module]:
        print(f"{module} has no command {name!r}; it has: {', '.join(sorted(commands[module]))}", file=sys.stderr)
        return 2
    if not registry.enabled(module, "commands"):
        print(f"{module} commands are off in Setup (Modules).", file=sys.stderr)
        return 3
    fn = commands[module][name]
    try:
        result = fn(list(args))
        if inspect.isawaitable(result):
            result = asyncio.run(result)
    except Exception as exc:  # noqa: BLE001 - one line for a person, not a traceback
        code = getattr(exc, "code", type(exc).__name__)
        message = getattr(exc, "message", str(exc))
        print(f"{module} {name}: {code}: {message}", file=sys.stderr)
        return 1
    if result is None:
        return 0
    if isinstance(result, bool):
        return 0 if result else 1
    if isinstance(result, int):
        return result
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

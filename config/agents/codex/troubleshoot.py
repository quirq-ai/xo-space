#!/usr/bin/env python3
"""Check the live codex install against its manifest.json paths.

Reads:
  - sibling manifest.json      (canonical codex paths)
  - ~/.codex/config.toml       (codex CLI config — TOML, NOT JSON)
  - ~/.codex/.env              (api keys / secrets, when present)

Reports OK / WARN / FAIL per check. WARN and FAIL rows are also echoed as a
timestamped line in the usage_sync format so they're greppable when stdout is
captured to /tmp/xo-space.log.

Exit codes: 0 all-ok, 1 any FAIL, 2 only WARNs.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMANDS_JSON = HERE / "manifest.json" if (HERE / "manifest.json").exists() else HERE / "commands.json"

OK, WARN, FAIL = "OK", "WARN", "FAIL"

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
COLORS = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}
RESET = "\033[0m"


def _paint(status: str) -> str:
    if not USE_COLOR:
        return status
    return f"{COLORS[status]}{status}{RESET}"


def _timestamp_prefix() -> str:
    tz_pref = (os.getenv("USAGE_SYNC_LOG_TZ", "UTC") or "UTC").strip().upper()
    if tz_pref == "IST":
        tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")
        tz_name = "IST"
    else:
        tz = datetime.timezone.utc
        tz_name = "UTC"
    ts = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    return f"[{ts} {tz_name}]"


class Report:
    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        print(f"  [{_paint(status)}] {name}" + (f" — {detail}" if detail else ""))
        if status in (WARN, FAIL):
            line = f"{_timestamp_prefix()} troubleshooting {self.agent}: {status} {name}"
            if detail:
                line += f" — {detail}"
            print(line)

    def exit_code(self) -> int:
        statuses = {s for s, _, _ in self.rows}
        if FAIL in statuses:
            return 1
        if WARN in statuses:
            return 2
        return 0

    def summary(self) -> str:
        counts = {OK: 0, WARN: 0, FAIL: 0}
        for s, _, _ in self.rows:
            counts[s] += 1
        return f"{counts[OK]} ok, {counts[WARN]} warn, {counts[FAIL]} fail"


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def load_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        return json.loads(path.read_text()), None
    except FileNotFoundError:
        return None, f"missing: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid JSON ({e.msg} at line {e.lineno})"
    except OSError as e:
        return None, f"unreadable: {e}"


# --------------------------------------------------------------------------- checks

def check_install(report: Report, binary: str, paths: dict[str, Path]) -> bool:
    home = paths["home_dir"]
    if not home.is_dir():
        report.add(FAIL, f"{binary} installed", f"{home} does not exist")
        return False
    report.add(OK, f"{binary} installed", str(home))

    cli = shutil.which(binary)
    if cli:
        report.add(OK, f"{binary} CLI on PATH", cli)
    else:
        report.add(WARN, f"{binary} CLI on PATH", "not found (install may be incomplete)")
    return True


def check_config_file(report: Report, paths: dict[str, Path]) -> None:
    # codex's config_file is TOML (~/.codex/config.toml), NOT JSON — never
    # json.load it. A fresh pod may have no config.toml yet (codex applies
    # defaults), so absent => WARN, present-but-unparseable => FAIL.
    cfg_path = paths["config_file"]
    if not cfg_path.is_file():
        report.add(WARN, "config.toml present", f"missing: {cfg_path} (codex will use defaults)")
        return
    try:
        with cfg_path.open("rb") as fh:
            tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        report.add(FAIL, "config.toml parseable", f"invalid TOML: {e}")
        return
    except OSError as e:
        report.add(FAIL, "config.toml readable", f"unreadable: {e}")
        return
    report.add(OK, "config.toml parseable", str(cfg_path))


def check_env_file(report: Report, paths: dict[str, Path]) -> None:
    env_path = paths["env_file"]
    if not env_path.is_file():
        report.add(WARN, ".env present", f"missing: {env_path}")
        return
    report.add(OK, ".env present", str(env_path))


# --------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Troubleshoot the codex agent install.")
    parser.parse_args()

    commands, err = load_json(COMMANDS_JSON)
    if err or commands is None:
        print(f"FATAL: cannot read {COMMANDS_JSON}: {err}", file=sys.stderr)
        return 1

    binary = commands.get("binary") or commands.get("name") or "codex"
    paths = {
        "home_dir":    expand(commands["home_dir"]),
        "config_file": expand(commands["config_file"]),
        "env_file":    expand(commands["env_file"]),
    }

    print(f"{binary} troubleshoot — home: {paths['home_dir']}\n")

    report = Report(agent=commands.get("name") or binary)

    print("Install:")
    installed = check_install(report, binary, paths)

    print("\nConfig files:")
    if installed:
        check_config_file(report, paths)
        check_env_file(report, paths)

    print(f"\nSummary: {report.summary()}")
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())

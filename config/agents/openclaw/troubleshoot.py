#!/usr/bin/env python3
"""Cross-check xo-project intent against the live OpenClaw install.

Reads:
  - sibling commands.json  (canonical openclaw paths + provider env keys)
  - <xo-project>/.xo/xo.json  (what the project says should be enabled)
  - ~/.openclaw/openclaw.json (what the CLI actually has configured)
  - ~/.openclaw/.env          (secrets/api keys)

Reports OK / WARN / FAIL per check. Exit codes: 0 all-ok, 1 any FAIL, 2 only WARNs.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

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


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE per line, strips single/double quotes."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key.strip()] = value
    return out


def dig(d: Any, *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def enabled_keys(section: dict | None) -> list[str]:
    """Return child keys whose `.enabled` is true, skipping the section's own `enabled` flag."""
    if not isinstance(section, dict):
        return []
    return [
        k for k, v in section.items()
        if k != "enabled" and isinstance(v, dict) and v.get("enabled") is True
    ]


# --------------------------------------------------------------------------- checks

def check_install(report: Report, paths: dict[str, Path]) -> bool:
    home = paths["home_dir"]
    if not home.is_dir():
        report.add(FAIL, "openclaw installed", f"{home} does not exist — run setup.sh")
        return False
    report.add(OK, "openclaw installed", str(home))

    cli = shutil.which("openclaw")
    if cli:
        report.add(OK, "openclaw CLI on PATH", cli)
    else:
        report.add(WARN, "openclaw CLI on PATH", "not found (install may be incomplete)")
    return True


def check_config_file(report: Report, paths: dict[str, Path]) -> dict | None:
    cfg, err = load_json(paths["config_file"])
    if err:
        report.add(FAIL, "openclaw.json readable", err)
        return None
    report.add(OK, "openclaw.json readable", str(paths["config_file"]))
    return cfg


def check_env_file(report: Report, paths: dict[str, Path]) -> dict[str, str]:
    env_path = paths["env_file"]
    if not env_path.is_file():
        report.add(WARN, ".env present", f"missing: {env_path}")
        return {}
    report.add(OK, ".env present", str(env_path))
    return parse_env_file(env_path)


def check_channels(report: Report, xo: dict, cfg: dict | None) -> None:
    xo_channels = dig(xo, "channels")
    if not isinstance(xo_channels, dict) or xo_channels.get("enabled") is not True:
        report.add(OK, "channels: section disabled in xo.json", "skipping channel checks")
        return

    wanted = enabled_keys(xo_channels)
    if not wanted:
        report.add(OK, "channels: no channels enabled in xo.json")
        return

    if cfg is None:
        report.add(FAIL, "channels alignment", "cannot verify — openclaw.json unreadable")
        return

    for name in wanted:
        ch_enabled = dig(cfg, "channels", name, "enabled") is True
        plugin_enabled = dig(cfg, "plugins", "entries", name, "enabled") is True
        if ch_enabled and plugin_enabled:
            report.add(OK, f"channel '{name}'", "channels.* and plugins.entries.* both enabled")
        elif not ch_enabled and not plugin_enabled:
            report.add(FAIL, f"channel '{name}'", "neither channels.* nor plugins.entries.* enabled in openclaw.json")
        else:
            missing = "plugins.entries" if ch_enabled else "channels"
            report.add(FAIL, f"channel '{name}'", f"{missing}.{name}.enabled is not true")


def check_api_keys(report: Report, xo: dict, env: dict[str, str], commands: dict) -> None:
    api_keys = dig(xo, "models", "api_keys")
    if not isinstance(api_keys, dict) or api_keys.get("enabled") is not True:
        report.add(OK, "models.api_keys: section disabled in xo.json", "skipping key checks")
        return

    wanted = enabled_keys(api_keys)
    providers = commands.get("providers", {}) if isinstance(commands.get("providers"), dict) else {}
    process_env = os.environ

    for name in wanted:
        env_key = dig(providers, name, "env_key")
        if not env_key:
            report.add(WARN, f"api key '{name}'", "no env_key mapping in commands.json")
            continue
        if env.get(env_key) or process_env.get(env_key):
            source = ".env" if env.get(env_key) else "process env"
            report.add(OK, f"api key '{name}'", f"{env_key} set ({source})")
        else:
            report.add(FAIL, f"api key '{name}'", f"{env_key} is empty/unset")


# --------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Troubleshoot the openclaw agent vs xo.json intent.")
    parser.add_argument(
        "--xo-json",
        default=os.environ.get("XO_JSON_PATH", "~/.xo/xo.json"),
        help="Path to xo.json (default: %(default)s)",
    )
    args = parser.parse_args()

    commands, err = load_json(COMMANDS_JSON)
    if err or commands is None:
        print(f"FATAL: cannot read {COMMANDS_JSON}: {err}", file=sys.stderr)
        return 1

    paths = {
        "home_dir":    expand(commands["home_dir"]),
        "config_file": expand(commands["config_file"]),
        "env_file":    expand(commands["env_file"]),
    }

    xo_path = expand(args.xo_json)
    xo, err = load_json(xo_path)
    if err or xo is None:
        print(f"FATAL: cannot read xo.json at {xo_path}: {err}", file=sys.stderr)
        return 1

    print(f"openclaw troubleshoot — xo.json: {xo_path}")
    print(f"                       openclaw home: {paths['home_dir']}\n")

    report = Report(agent=commands.get("name") or "openclaw")

    print("Install:")
    installed = check_install(report, paths)

    print("\nConfig files:")
    cfg = check_config_file(report, paths) if installed else None
    env = check_env_file(report, paths) if installed else {}

    print("\nChannels (xo.json ↔ openclaw.json):")
    check_channels(report, xo, cfg)

    print("\nAPI keys (xo.json ↔ .env):")
    check_api_keys(report, xo, env, commands)

    print(f"\nSummary: {report.summary()}")
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())

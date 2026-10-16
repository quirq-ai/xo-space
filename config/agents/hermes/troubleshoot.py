#!/usr/bin/env python3
"""Cross-check xo-project intent against the live Hermes install.

Reads:
  - sibling commands.json       (canonical hermes paths, providers, channels)
  - <xo-project>/.xo/xo.json    (what the project says should be enabled)
  - ~/.hermes/config.yaml       (what the hermes CLI actually has configured)
  - ~/.hermes/.env              (secrets/api keys)

For every channels.* / models.api_keys.* / data.* section enabled in xo.json,
confirm the matching env var(s) declared in commands.json are populated in
~/.hermes/.env or the process environment.

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
    """Minimal .env parser: KEY=VALUE per line, strips surrounding quotes."""
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
    """Child keys whose `.enabled` is true, skipping the section's own `enabled` flag."""
    if not isinstance(section, dict):
        return []
    return [
        k for k, v in section.items()
        if k != "enabled" and isinstance(v, dict) and v.get("enabled") is True
    ]


def env_var_present(name: str, env_file: dict[str, str]) -> tuple[bool, str]:
    """Return (present, source). Non-empty wins; .env preferred over process env for source label."""
    if env_file.get(name):
        return True, ".env"
    if os.environ.get(name):
        return True, "process env"
    return False, ""


# --------------------------------------------------------------------------- checks

def check_install(report: Report, binary: str, paths: dict[str, Path]) -> bool:
    home = paths["home_dir"]
    if not home.is_dir():
        report.add(FAIL, f"{binary} installed", f"{home} does not exist — run setup.sh")
        return False
    report.add(OK, f"{binary} installed", str(home))

    cli = shutil.which(binary)
    if cli:
        report.add(OK, f"{binary} CLI on PATH", cli)
    else:
        report.add(WARN, f"{binary} CLI on PATH", "not found (install may be incomplete)")
    return True


def check_config_file(report: Report, paths: dict[str, Path]) -> bool:
    cfg_path = paths["config_file"]
    if not cfg_path.is_file():
        report.add(FAIL, "config.yaml present", f"missing: {cfg_path}")
        return False
    try:
        text = cfg_path.read_text()
    except OSError as e:
        report.add(FAIL, "config.yaml readable", str(e))
        return False
    if not text.strip():
        report.add(WARN, "config.yaml readable", f"{cfg_path} is empty")
        return True
    report.add(OK, "config.yaml present", str(cfg_path))
    return True


def check_env_file(report: Report, paths: dict[str, Path]) -> dict[str, str]:
    env_path = paths["env_file"]
    if not env_path.is_file():
        report.add(WARN, ".env present", f"missing: {env_path}")
        return {}
    report.add(OK, ".env present", str(env_path))
    return parse_env_file(env_path)


def check_channels(report: Report, xo: dict, commands: dict, env: dict[str, str]) -> None:
    xo_channels = dig(xo, "channels")
    if not isinstance(xo_channels, dict) or xo_channels.get("enabled") is not True:
        report.add(OK, "channels: section disabled in xo.json", "skipping channel checks")
        return

    wanted = enabled_keys(xo_channels)
    if not wanted:
        report.add(OK, "channels: no channels enabled in xo.json")
        return

    chan_map = commands.get("channels", {}) if isinstance(commands.get("channels"), dict) else {}

    for name in wanted:
        decl = chan_map.get(name)
        if not isinstance(decl, dict):
            report.add(WARN, f"channel '{name}'", "no mapping in commands.json")
            continue
        fields = decl.get("fields") if isinstance(decl.get("fields"), dict) else {}
        defaults = decl.get("defaults") if isinstance(decl.get("defaults"), dict) else {}
        if not fields:
            report.add(WARN, f"channel '{name}'", "commands.json declares no fields")
            continue

        missing: list[str] = []
        sources: list[str] = []
        for field_name, env_key in fields.items():
            present, source = env_var_present(env_key, env)
            if present:
                sources.append(f"{env_key}({source})")
            elif field_name in defaults:
                sources.append(f"{env_key}=<default:{defaults[field_name]}>")
            else:
                missing.append(env_key)

        if missing:
            report.add(FAIL, f"channel '{name}'", f"unset env var(s): {', '.join(missing)}")
        else:
            report.add(OK, f"channel '{name}'", ", ".join(sources))


def check_api_keys(report: Report, xo: dict, env: dict[str, str], commands: dict) -> None:
    api_keys = dig(xo, "models", "api_keys")
    if not isinstance(api_keys, dict) or api_keys.get("enabled") is not True:
        report.add(OK, "models.api_keys: section disabled in xo.json", "skipping key checks")
        return

    wanted = enabled_keys(api_keys)
    if not wanted:
        report.add(OK, "models.api_keys: no providers enabled in xo.json")
        return

    providers = commands.get("providers", {}) if isinstance(commands.get("providers"), dict) else {}

    for name in wanted:
        env_key = dig(providers, name, "env_key")
        if not env_key:
            report.add(WARN, f"api key '{name}'", "no env_key mapping in commands.json")
            continue
        present, source = env_var_present(env_key, env)
        if present:
            report.add(OK, f"api key '{name}'", f"{env_key} set ({source})")
        else:
            report.add(FAIL, f"api key '{name}'", f"{env_key} is empty/unset")


def check_data(report: Report, xo: dict, env: dict[str, str], commands: dict) -> None:
    data = dig(xo, "data")
    if not isinstance(data, dict) or data.get("enabled") is not True:
        report.add(OK, "data: section disabled in xo.json", "skipping data checks")
        return

    wanted = enabled_keys(data)
    if not wanted:
        report.add(OK, "data: no integrations enabled in xo.json")
        return

    data_map = commands.get("data", {}) if isinstance(commands.get("data"), dict) else {}

    for name in wanted:
        decl = data_map.get(name)
        if not isinstance(decl, dict):
            report.add(WARN, f"data '{name}'", "no mapping in commands.json")
            continue
        env_key = decl.get("env_key")
        fields = decl.get("fields") if isinstance(decl.get("fields"), dict) else {}
        keys_to_check = [env_key] if env_key else list(fields.values())
        if not keys_to_check:
            report.add(WARN, f"data '{name}'", "commands.json declares no env keys")
            continue

        missing: list[str] = []
        sources: list[str] = []
        for key in keys_to_check:
            present, source = env_var_present(key, env)
            if present:
                sources.append(f"{key}({source})")
            else:
                missing.append(key)

        if missing:
            report.add(FAIL, f"data '{name}'", f"unset env var(s): {', '.join(missing)}")
        else:
            report.add(OK, f"data '{name}'", ", ".join(sources))


# --------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Troubleshoot the hermes agent vs xo.json intent.")
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

    binary = commands.get("binary") or commands.get("name") or "hermes"
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

    print(f"{binary} troubleshoot — xo.json: {xo_path}")
    print(f"                       {binary} home: {paths['home_dir']}\n")

    report = Report(agent=commands.get("name") or binary)

    print("Install:")
    installed = check_install(report, binary, paths)

    print("\nConfig files:")
    if installed:
        check_config_file(report, paths)
        env = check_env_file(report, paths)
    else:
        env = {}

    print("\nChannels (xo.json ↔ .env / commands.json):")
    check_channels(report, xo, commands, env)

    print("\nAPI keys (xo.json ↔ .env / commands.json):")
    check_api_keys(report, xo, env, commands)

    print("\nData integrations (xo.json ↔ .env / commands.json):")
    check_data(report, xo, env, commands)

    print(f"\nSummary: {report.summary()}")
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())

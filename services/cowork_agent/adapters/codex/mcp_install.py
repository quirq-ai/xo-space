"""
``mcp_install`` capability — point the codex CLI at the Composio MCP proxy.

Codex reads MCP servers from TOML tables in ``$CODEX_HOME/config.toml``
(``~/.codex/config.toml`` by default). A streamable-HTTP server is a table with
a ``url`` — no command, and no credential, because the URL is the loopback proxy
whose opaque token the backend resolves (``composio/mcp_proxy.py`` injects
COMPOSIO_API_KEY server-side):

    [mcp_servers.composio]
    url = "http://127.0.0.1:5002/mcp/composio-proxy/u/<token>"
    enabled = true

Two things make this installer differ from its claude_code / hermes / openclaw
siblings, both deliberate:

- **It edits text, not a parsed document.** ``config.toml`` is hand-maintained
  (model, approval policy, sandbox) and the stdlib ships a TOML *reader* only,
  so a parse-and-dump round trip would need a new dependency and would still
  discard every comment in the file. The composio table is spliced instead, and
  every other byte is preserved.
- **A missing config.toml is created**, where the claude_code installer refuses.
  Codex runs fine with no config file, so treating "absent" as an error would
  leave codex permanently unconnectable on a fresh box; ``~/.claude.json`` by
  contrast is owned by the claude CLI, and its absence means claude never ran.

The splice is guarded rather than trusted: the result is re-parsed and compared
against the original, and anything beyond the intended ``mcp_servers`` change
aborts the install. A config this module could not parse is never rewritten.

Core reaches this by capability name through the loader, so the agent literal
lives here in the adapter tree (DEVELOPING.md §6).
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any

SERVER_NAME = "composio"

# Names used by earlier iterations, removed on write so a stale table cannot
# shadow the current one — two tables pointing at the same proxy would list
# every tool twice. Mirrors the other three installers.
_LEGACY_NAMES = ("cowork", "xo_composio")

# A TOML table header alone on its line: `[x]`, `[[x]]`, either with a trailing
# comment. Used to find where a table's body ends. A line like `args = [` is not
# a header, and neither is a nested-array continuation `[1, 2],` (the comma).
_TABLE_HEADER = re.compile(r"^\s*\[\[?[^\[\]]*\]\]?\s*(?:#.*)?$")


def config_path() -> Path:
    """``$CODEX_HOME/config.toml``, resolved through the adapter's own root.

    Reusing ``paths.codex_home`` keeps this in step with the rollout reader and
    honours ``CODEX_HOME`` / the manifest instead of hardcoding ``~/.codex``.
    """
    from services.cowork_agent.adapters.codex.paths import codex_home

    return codex_home() / "config.toml"


def _toml_string(value: str) -> str:
    """Render ``value`` as a TOML basic string.

    Hand-rendered because there is no stdlib writer. The proxy URL is built by
    the backend, but escaping it anyway is what keeps a token carrying a quote
    or a backslash from breaking out of the string and corrupting the config.
    """
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _desired_entry(proxy_url: str) -> dict[str, Any]:
    return {"url": proxy_url, "enabled": True}


def _render_block(proxy_url: str) -> str:
    return (
        f"[mcp_servers.{SERVER_NAME}]\n"
        "# Managed by xo-space — rewritten by\n"
        "# POST /api/connectors/composio/refresh-gateway.\n"
        f"url = {_toml_string(proxy_url)}\n"
        "enabled = true\n"
    )


def _header_pattern(name: str) -> re.Pattern[str]:
    """Match the header of ``[mcp_servers.<name>]``, quoted keys included."""
    key = re.escape(name)
    seg = rf"(?:{key}|\"{key}\"|'{key}')"
    parent = r"(?:mcp_servers|\"mcp_servers\"|'mcp_servers')"
    return re.compile(rf"^\s*\[\s*{parent}\s*\.\s*{seg}\s*\]\s*(?:#.*)?$")


def _strip_tables(lines: list[str], patterns: list[re.Pattern[str]]) -> list[str]:
    """Drop each matching table header and the body lines that follow it."""
    kept: list[str] = []
    index = 0
    while index < len(lines):
        if any(pattern.match(lines[index]) for pattern in patterns):
            index += 1
            while index < len(lines) and not _TABLE_HEADER.match(lines[index]):
                index += 1
            continue
        kept.append(lines[index])
        index += 1
    return kept


def _without_managed(doc: dict[str, Any]) -> dict[str, Any]:
    """``doc`` minus the tables this installer owns.

    Comparing the before and after of this projection is what proves the splice
    touched nothing else — a mis-chopped line would show up as a lost or moved
    key here, and the install aborts instead of writing.
    """
    out = dict(doc)
    servers = out.get("mcp_servers")
    if isinstance(servers, dict):
        remaining = {
            name: entry
            for name, entry in servers.items()
            if name != SERVER_NAME and name not in _LEGACY_NAMES
        }
        if remaining:
            out["mcp_servers"] = remaining
        else:
            out.pop("mcp_servers", None)
    return out


def install(proxy_url: str) -> dict[str, Any]:
    """Idempotently point codex's ``mcp_servers.composio`` at ``proxy_url``.

    Re-call to refresh. The caller (the refresh-gateway route) is responsible
    for telling the user to restart codex; an already-current config reports
    ``changed: False`` so a boot-time install stays silent.
    """
    if not proxy_url:
        return {"ok": False, "error": "No proxy URL supplied."}

    path = config_path()
    existed = path.exists()
    original = ""
    before: dict[str, Any] = {}

    if existed:
        try:
            original = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": f"Failed to read codex config: {exc}"}
        try:
            before = tomllib.loads(original)
        except tomllib.TOMLDecodeError as exc:
            # Never rewrite a config we could not understand — that would
            # discard the user's real settings to install one MCP server.
            return {
                "ok": False,
                "error": (
                    f"Codex config at {path} is not valid TOML ({exc}); "
                    "refusing to rewrite it."
                ),
            }

    desired = _desired_entry(proxy_url)
    servers = before.get("mcp_servers")
    servers = servers if isinstance(servers, dict) else {}
    has_legacy = any(name in servers for name in _LEGACY_NAMES)
    if existed and servers.get(SERVER_NAME) == desired and not has_legacy:
        return {
            "ok": True,
            "config_path": str(path),
            "restart_required": False,
            "changed": False,
        }

    patterns = [_header_pattern(name) for name in (SERVER_NAME, *_LEGACY_NAMES)]
    kept = "\n".join(_strip_tables(original.splitlines(), patterns)).rstrip("\n")
    text = (kept + "\n\n" if kept else "") + _render_block(proxy_url)

    try:
        after = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return {
            "ok": False,
            "error": (
                f"Editing {path} would produce invalid TOML ({exc}). This happens "
                "when mcp_servers is written as an inline table; move it to "
                "[mcp_servers.<name>] tables and retry."
            ),
        }
    if _without_managed(after) != _without_managed(before):
        return {
            "ok": False,
            "error": (
                f"Refusing to write {path}: the edit would have changed settings "
                "outside mcp_servers."
            ),
        }
    if after.get("mcp_servers", {}).get(SERVER_NAME) != desired:
        return {
            "ok": False,
            "error": f"Refusing to write {path}: the composio entry did not apply.",
        }

    mode: int | None = None
    if existed:
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = None

    tmp_name = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=str(path.parent),
            delete=False,
            suffix=".xo-mcp.tmp",
            encoding="utf-8",
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        # NamedTemporaryFile is 0600; keep an existing config's own mode rather
        # than silently tightening it, and default a new file to 0600.
        os.chmod(tmp_name, mode if mode is not None else 0o600)
        os.replace(tmp_name, path)
    except OSError as exc:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return {"ok": False, "error": f"Failed to write codex config: {exc}"}

    return {
        "ok": True,
        "config_path": str(path),
        "restart_required": True,
        "changed": True,
        "created": not existed,
    }

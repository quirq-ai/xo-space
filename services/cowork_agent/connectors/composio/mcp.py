"""
Composio MCP install — one declarative writer, driven by the agent manifest.

Every agent that can talk to Composio declares *where* and *in what shape* its MCP
server entry lives, as an ``"mcp"`` block in ``config/agents/<name>/manifest.json``.
This module reads that block and writes the entry. There is no per-agent Python:
adding an agent is adding a block.

The manifest loader ignores keys it does not know and keeps the whole document on
``AgentManifest.raw`` (``registry/agent_registry.py``), which is the same seam the
``providers`` and ``channels`` recipes already use — so no registry change was
needed to introduce this one.

Block shape::

    "mcp": {
      "format": "json" | "yaml" | "toml",   # required
      "key_path": ["mcp_servers"],          # required — keys down to the server map
      "server_name": "composio",            # required — the leaf key under key_path
      "entry": {"url": "{proxy_url}"},      # required — must mention {proxy_url}
      "legacy_names": ["cowork"],           # optional — purged on every write
      "create_if_missing": false,           # optional — default false
      "prune": [                            # optional — extra keys to delete
        {"key_path": ["plugins", "entries"], "names": ["composio"]}
      ],

      # Path, in precedence order. All three forms are optional; the last one wins
      # by default because most agents keep MCP in their main config file.
      "path": "~/.claude.json",             # 1. explicit
      "home_env": "CODEX_HOME",             # 2. $CODEX_HOME (or manifest home_dir)
      "path_in_home": "config.toml"         #    joined with path_in_home
      #                                       3. else the manifest's config_file
    }

``entry`` is written verbatim with the literal token ``{proxy_url}`` replaced in every
string. No credential is ever written: the URL is a loopback proxy path whose opaque
token the backend resolves, and ``mcp_proxy.py`` injects COMPOSIO_API_KEY server-side.

Two invariants hold across every format, inherited from the per-adapter installers
this module replaces:

- **A config that failed to parse is never rewritten.** Overwriting it would discard
  the user's real settings to install one MCP server.
- **An existing file's permissions are preserved**, never widened or tightened. A new
  file is created 0600.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MANIFEST_KEY = "mcp"
FORMATS = ("json", "yaml", "toml")
PROXY_PLACEHOLDER = "{proxy_url}"


# ══════════════════════════════════════════════════════════════════════════════
# Part 1 — read and validate the manifest block
# ══════════════════════════════════════════════════════════════════════════════


class _Invalid(ValueError):
    """A manifest ``mcp`` block that cannot be used. Logged, never raised outward."""


@dataclass(frozen=True)
class _Prune:
    key_path: tuple[str, ...]
    names: tuple[str, ...]


@dataclass(frozen=True)
class McpTarget:
    """A validated install target — everything ``apply`` needs, nothing agent-shaped."""

    agent: str
    fmt: str
    path: Path
    key_path: tuple[str, ...]
    server_name: str
    legacy_names: tuple[str, ...]
    entry: dict[str, Any]
    create_if_missing: bool
    prune: tuple[_Prune, ...]

    @property
    def managed_names(self) -> tuple[str, ...]:
        """Every key under ``key_path`` this module owns — current plus legacy."""
        return (self.server_name, *self.legacy_names)


def _mentions_proxy(value: Any) -> bool:
    if isinstance(value, str):
        return PROXY_PLACEHOLDER in value
    if isinstance(value, dict):
        return any(_mentions_proxy(v) for v in value.values())
    if isinstance(value, list):
        return any(_mentions_proxy(v) for v in value)
    return False


def _str_list(block: dict, key: str) -> tuple[str, ...]:
    raw = block.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(n, str) and n for n in raw):
        raise _Invalid(f"'{key}' must be a list of non-empty strings")
    return tuple(raw)


def _key_path(raw: Any, where: str) -> tuple[str, ...]:
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(k, str) and k for k in raw)
    ):
        raise _Invalid(f"'{where}' must be a non-empty list of non-empty strings")
    return tuple(raw)


def _resolve_path(block: dict, manifest: Any) -> Path:
    """The config file to edit. See the module docstring for the precedence."""
    explicit = block.get("path")
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit:
            raise _Invalid("'path' must be a non-empty string")
        return Path(os.path.expanduser(explicit))

    in_home = block.get("path_in_home")
    if in_home is not None:
        if not isinstance(in_home, str) or not in_home:
            raise _Invalid("'path_in_home' must be a non-empty string")
        home_env = block.get("home_env")
        if not isinstance(home_env, str) or not home_env:
            raise _Invalid("'path_in_home' requires 'home_env' naming the override var")
        # Env first, then the manifest home: the same precedence an agent's own
        # CLI uses, so this writer and that agent's readers never disagree on
        # the root when the home is relocated.
        override = (os.getenv(home_env, "") or "").strip()
        home = Path(os.path.expanduser(override)) if override else manifest.home_dir
        return home / in_home

    config_file = getattr(manifest, "config_file", None)
    if config_file is None:
        raise _Invalid("no 'path' or 'path_in_home', and the manifest has no config_file")
    return config_file


def _validated(agent: str, manifest: Any, block: Any) -> McpTarget:
    if not isinstance(block, dict):
        raise _Invalid("'mcp' must be a JSON object")

    fmt = block.get("format")
    if fmt not in FORMATS:
        raise _Invalid(f"'format' must be one of {list(FORMATS)}, got {fmt!r}")

    server_name = block.get("server_name")
    if not isinstance(server_name, str) or not server_name:
        raise _Invalid("'server_name' must be a non-empty string")

    entry = block.get("entry")
    if not isinstance(entry, dict) or not entry:
        raise _Invalid("'entry' must be a non-empty JSON object")
    if not _mentions_proxy(entry):
        raise _Invalid(f"'entry' contains no {PROXY_PLACEHOLDER} placeholder")

    create_if_missing = block.get("create_if_missing", False)
    if not isinstance(create_if_missing, bool):
        raise _Invalid("'create_if_missing' must be a boolean")

    prune_raw = block.get("prune", [])
    if not isinstance(prune_raw, list):
        raise _Invalid("'prune' must be a list of {key_path, names} objects")
    prune: list[_Prune] = []
    for rule in prune_raw:
        if not isinstance(rule, dict):
            raise _Invalid("each 'prune' entry must be an object")
        prune.append(
            _Prune(
                _key_path(rule.get("key_path"), "prune[].key_path"),
                _str_list(rule, "names"),
            )
        )

    return McpTarget(
        agent=agent,
        fmt=fmt,
        path=_resolve_path(block, manifest),
        key_path=_key_path(block.get("key_path"), "key_path"),
        server_name=server_name,
        legacy_names=_str_list(block, "legacy_names"),
        entry=entry,
        create_if_missing=create_if_missing,
        prune=tuple(prune),
    )


def load_target(agent: str) -> McpTarget | None:
    """The validated ``mcp`` block for ``agent``, or ``None``.

    ``None`` for three distinct, all-normal cases: the agent has no manifest, the
    manifest declares no ``mcp`` block (it simply does not support Composio), or the
    block is malformed. The last one is logged. Never raises — this runs on the boot
    path, where a bad config must not take the server down.
    """
    try:
        from services.cowork_agent.registry.agent_registry import get_agent

        manifest = get_agent(agent)
    except Exception as exc:
        log.warning("composio mcp: no manifest for agent %r (%s)", agent, exc)
        return None

    block = manifest.raw.get(MANIFEST_KEY)
    if block is None:
        return None

    try:
        return _validated(agent, manifest, block)
    except _Invalid as exc:
        log.warning(
            "composio mcp: config/agents/%s/manifest.json has an unusable 'mcp' block: %s",
            agent, exc,
        )
        return None


def agents_with_targets() -> list[str]:
    """Every registered agent whose manifest declares a usable ``mcp`` block."""
    try:
        from services.cowork_agent.registry.agent_registry import all_agents

        names = [a.name for a in all_agents()]
    except Exception as exc:
        log.warning("composio mcp: agent registry unavailable (%s)", exc)
        return []
    return sorted(name for name in names if load_target(name) is not None)


# ══════════════════════════════════════════════════════════════════════════════
# Part 2 — the writer
# ══════════════════════════════════════════════════════════════════════════════


class _Refuse(Exception):
    """Abort the install without writing. The message reaches the user verbatim."""


def _err(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _render(value: Any, proxy_url: str) -> Any:
    """``entry`` with ``{proxy_url}`` substituted in every string.

    A plain replace, deliberately not ``str.format`` — a value carrying a stray brace
    must not raise, and no other placeholder is supported.
    """
    if isinstance(value, str):
        return value.replace(PROXY_PLACEHOLDER, proxy_url)
    if isinstance(value, dict):
        return {k: _render(v, proxy_url) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, proxy_url) for v in value]
    return value


def _peek(doc: dict, key_path: tuple[str, ...]) -> dict | None:
    """The map at ``key_path``, or ``None`` if any level is absent or not a map."""
    node: Any = doc
    for key in key_path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, dict) else None


def _descend(doc: dict, key_path: tuple[str, ...]) -> dict:
    """The map at ``key_path``, creating missing levels. Refuses to overwrite a scalar."""
    node = doc
    for depth, key in enumerate(key_path):
        current = node.get(key)
        if current is None:
            current = {}
            node[key] = current
        elif not isinstance(current, dict):
            trail = ".".join(key_path[: depth + 1])
            raise _Refuse(f"'{trail}' exists but is not a table; refusing to replace it.")
        node = current
    return node


# ── structured formats (json, yaml) ───────────────────────────────────────────


def _parse_structured(fmt: str, text: str) -> dict:
    if not text.strip():
        return {}
    if fmt == "json":
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _Refuse(f"not valid JSON ({exc}); refusing to rewrite it.") from exc
    else:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise _Refuse("PyYAML is not installed; cannot edit a YAML config.") from exc
        try:
            doc = yaml.safe_load(text)
        except Exception as exc:
            raise _Refuse(f"not valid YAML ({exc}); refusing to rewrite it.") from exc
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise _Refuse("top level is not an object; refusing to rewrite it.")
    return doc


def _dump_structured(fmt: str, doc: dict) -> str:
    if fmt == "json":
        # ensure_ascii=False, matching visualizer/atomic_write.write_json_atomic.
        # These configs are the agent's own and full of prose: escaping every em
        # dash would rewrite thousands of untouched lines on each token refresh,
        # burying the one line that actually changed.
        return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    import yaml  # type: ignore

    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _apply_structured(target: McpTarget, entry: dict, original: str, existed: bool) -> str | None:
    """The edited document text, or ``None`` when the file is already current."""
    doc = _parse_structured(target.fmt, original) if existed else {}

    servers = _peek(doc, target.key_path)
    current_ok = bool(servers) and servers.get(target.server_name) == entry
    has_legacy = bool(servers) and any(n in servers for n in target.legacy_names)
    if existed and current_ok and not has_legacy and not _has_prunable(doc, target):
        return None

    servers = _descend(doc, target.key_path)
    servers[target.server_name] = entry
    for name in target.legacy_names:
        servers.pop(name, None)
    _apply_prune(doc, target)
    return _dump_structured(target.fmt, doc)


def _has_prunable(doc: dict, target: McpTarget) -> bool:
    for rule in target.prune:
        node = _peek(doc, rule.key_path)
        if node and any(name in node for name in rule.names):
            return True
    return False


def _apply_prune(doc: dict, target: McpTarget) -> None:
    for rule in target.prune:
        node = _peek(doc, rule.key_path)
        if not node:
            continue
        for name in rule.names:
            node.pop(name, None)


# ── TOML ──────────────────────────────────────────────────────────────────────
#
# Edited as *text*, not as a parsed document. The stdlib ships a TOML reader only,
# so a parse-and-dump round trip would need a new dependency and would still discard
# every comment in a file that is hand-maintained (model, approval policy, sandbox).
# The managed table is spliced instead and every other byte is preserved — then the
# result is re-parsed and compared against the original, so a mis-chopped line aborts
# the install rather than corrupting the config.

# A table header alone on its line: `[x]`, `[[x]]`, either with a trailing comment.
# Marks where a table's body ends. `args = [` is not a header, nor is a nested-array
# continuation `[1, 2],` (the comma).
_TABLE_HEADER = re.compile(r"^\s*\[\[?[^\[\]]*\]\]?\s*(?:#.*)?$")

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_string(value: str) -> str:
    """``value`` as a TOML basic string.

    Hand-rendered because there is no stdlib writer. The proxy URL is built by the
    backend, but escaping it anyway is what keeps a token carrying a quote or a
    backslash from breaking out of the string and corrupting the config.
    """
    out: list[str] = []
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


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise _Refuse(f"cannot render {type(value).__name__} into TOML: {value!r}")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _toml_string(key)


def _toml_header_pattern(segments: tuple[str, ...]) -> re.Pattern[str]:
    """Match the header of ``[a.b.c]``, quoted keys included."""
    parts = []
    for seg in segments:
        esc = re.escape(seg)
        parts.append(rf"(?:{esc}|\"{esc}\"|'{esc}')")
    return re.compile(r"^\s*\[\s*" + r"\s*\.\s*".join(parts) + r"\s*\]\s*(?:#.*)?$")


def _render_toml_block(target: McpTarget, entry: dict) -> str:
    header = ".".join(_toml_key(k) for k in (*target.key_path, target.server_name))
    lines = [
        f"[{header}]",
        "# Managed by xo-space — rewritten by",
        "# POST /api/connectors/composio/refresh-gateway.",
    ]
    lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in entry.items()]
    return "\n".join(lines) + "\n"


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


def _without_managed(doc: dict, target: McpTarget) -> dict:
    """``doc`` minus the tables this module owns.

    Comparing the before and after of this projection is what proves the splice
    touched nothing else — a mis-chopped line shows up as a lost or moved key here,
    and the install aborts instead of writing.
    """
    out = copy.deepcopy(doc)
    parent: Any = out
    for key in target.key_path[:-1]:
        parent = parent.get(key) if isinstance(parent, dict) else None
        if not isinstance(parent, dict):
            return out
    leaf = target.key_path[-1]
    servers = parent.get(leaf) if isinstance(parent, dict) else None
    if isinstance(servers, dict):
        remaining = {
            name: value
            for name, value in servers.items()
            if name not in target.managed_names
        }
        if remaining:
            parent[leaf] = remaining
        else:
            parent.pop(leaf, None)
    return out


def _apply_toml(target: McpTarget, entry: dict, original: str, existed: bool) -> str | None:
    try:
        before = tomllib.loads(original) if existed else {}
    except tomllib.TOMLDecodeError as exc:
        raise _Refuse(f"not valid TOML ({exc}); refusing to rewrite it.") from exc

    servers = _peek(before, target.key_path) or {}
    has_legacy = any(name in servers for name in target.legacy_names)
    if existed and servers.get(target.server_name) == entry and not has_legacy:
        return None

    patterns = [
        _toml_header_pattern((*target.key_path, name)) for name in target.managed_names
    ]
    kept = "\n".join(_strip_tables(original.splitlines(), patterns)).rstrip("\n")
    text = (kept + "\n\n" if kept else "") + _render_toml_block(target, entry)

    try:
        after = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        trail = ".".join(target.key_path)
        raise _Refuse(
            f"editing it would produce invalid TOML ({exc}). This happens when "
            f"{trail} is written as an inline table; move it to [{trail}.<name>] "
            "tables and retry."
        ) from exc

    if _without_managed(after, target) != _without_managed(before, target):
        trail = ".".join(target.key_path)
        raise _Refuse(f"the edit would have changed settings outside {trail}.")
    if (_peek(after, target.key_path) or {}).get(target.server_name) != entry:
        raise _Refuse(f"the {target.server_name} entry did not apply.")
    return text


# ── the public entry point ────────────────────────────────────────────────────


def _write(path: Path, text: str, existed: bool) -> str | None:
    """Atomically replace ``path``. Returns an error message, or ``None`` on success."""
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
            "w", dir=str(path.parent), delete=False, suffix=".xo-mcp.tmp", encoding="utf-8"
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        # NamedTemporaryFile is 0600; keep an existing config's own mode rather than
        # silently tightening it, and default a new file to 0600.
        os.chmod(tmp_name, mode if mode is not None else 0o600)
        os.replace(tmp_name, path)
    except OSError as exc:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return f"Failed to write {path}: {exc}"
    return None


def apply(target: McpTarget, proxy_url: str) -> dict[str, Any]:
    """Idempotently point ``target``'s config at ``proxy_url``.

    Re-call to refresh. The caller (the refresh-gateway route, or the boot-time
    installer) is responsible for telling the user to restart the agent; an
    already-current config reports ``changed: False`` so a boot install stays silent.
    """
    if not proxy_url:
        return _err("No proxy URL supplied.")

    path = target.path
    existed = path.exists()
    if not existed and not target.create_if_missing:
        return _err(f"{target.agent} config not found at {path}")

    original = ""
    if existed:
        try:
            original = path.read_text(encoding="utf-8")
        except OSError as exc:
            return _err(f"Failed to read {target.agent} config at {path}: {exc}")

    entry = _render(target.entry, proxy_url)

    try:
        if target.fmt == "toml":
            text = _apply_toml(target, entry, original, existed)
        else:
            text = _apply_structured(target, entry, original, existed)
    except _Refuse as exc:
        return _err(f"Refusing to write {path}: {exc}")

    if text is None:
        return {
            "ok": True,
            "config_path": str(path),
            "restart_required": False,
            "changed": False,
        }

    error = _write(path, text, existed)
    if error:
        return _err(error)

    return {
        "ok": True,
        "config_path": str(path),
        "restart_required": True,
        "changed": True,
        "created": not existed,
    }

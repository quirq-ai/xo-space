#!/usr/bin/env python3
"""The XO Launchpad deploy script: the one executor of ``flow.json``.

    launchpad.py check     --project DIR            read files only; print the state as JSON
    launchpad.py preflight --project DIR            ask GitHub and Launchpad what a deploy would meet; change nothing
    launchpad.py deploy    --project DIR [options]  run the flow; JSON progress events, one per line
    launchpad.py watch     --project DIR            follow the latest build and release to the URL
    launchpad.py apps      --project DIR            the Launchpad apps this project could link to
    launchpad.py link      --project DIR --app APP  point launchpad.json at an existing app
    launchpad.py unlink    --project DIR [--forget] drop the app from launchpad.json (the app keeps running)

``flow.json`` beside this file is the definition: the states, the steps and
their rules, every failure's message and fix, the detection settings and the
Dockerfile templates. This script runs it, and agents read it.

Credentials come from the environment only, set by the Space server: XO_API_KEY
for Launchpad and GITHUB_TOKEN, the GitHub connector's token. Neither is
printed, logged, put in an argv or written to disk. Git gets the token through
GIT_CONFIG_* variables (an http extraheader), never in a URL or .git/config;
xo-launchpad gets XO_API_KEY, plus GITHUB_TOKEN for ``app create`` only.
Values the owner types for the app's environment arrive on stdin
(``--secrets-stdin``) and go to xo-launchpad through its environment.

Every command runs through ``utils.commands`` (DEVELOPING.md §7, one executor).
xo-launchpad is the copy installed in this Python's own environment
(``requirements.txt``), never one from PATH.

Events (deploy, watch, apps, link, unlink) are JSON lines on stdout:

    {"type": "step", "step": "build", "title": "...", "status": "running|done|skipped|failed|waiting", "message": "..."}
    {"type": "progress", "step": "build", "message": "...", "data": {...}}
    {"type": "warning", "step": "check", "message": "..."}
    {"type": "status", ...}                                        watch: the app as it is now
    {"type": "result", ...}                                        apps, link, unlink
    {"type": "done", "url": "...", "app": {...}, ...}              last event of a deploy or watch
    {"type": "error", "step": "...", "code": "...", "message": "...", "fix": "..."}
    {"type": "needs", "step": "...", "code": "...", "message": "...", "fix": "...", "options": [...]}

``needs`` stops the flow on a decision only the owner can make (create a repo,
link an app, recreate a deleted app, give the port); deploying again with the
answer carries on. Exit codes: 0 done, 1 error, 3 needs a decision, 130 stopped.
"""

from __future__ import annotations

import argparse
import base64
import functools
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
if not __package__:
    # Run as a file: put the xo-space root on the path for utils.commands.
    sys.path.insert(0, str(HERE.parents[1]))

from utils.commands import run_sync  # noqa: E402  (the one executor)

SCRIPT = Path(__file__).resolve()
FLOW_PATH = HERE / "flow.json"
CONFIG_NAME = "launchpad.json"
CONFIG_MAX_BYTES = 64 * 1024
SECRETS_MAX_BYTES = 256 * 1024
DEFAULT_REMOTE = "launchpad"
USER_AGENT = "xo-space-launchpad/1"

NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,23}[a-z0-9])?\Z")
ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
APP_ID_RE = re.compile(r"[0-9A-Za-z-]{8,64}\Z")
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,99}\Z")
FULL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}\Z")
REPO_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
REMOTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
HEALTH_RE = re.compile(r"/[\x21-\x7e]{0,199}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?\Z")
WORKFLOW_RE = re.compile(r"\.github/workflows/launchpad-[a-z0-9-]+\.ya?ml\Z")
CONFIG_KEYS = ("schema", "name", "app_id", "slug", "url", "workflow", "repo", "port", "health_path", "env")
POINTER_KEYS = ("app_id", "slug", "url", "workflow")

EXIT_DONE, EXIT_ERROR, EXIT_NEEDS, EXIT_STOPPED = 0, 1, 3, 130


# ── the flow ─────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def flow() -> dict[str, Any]:
    return json.loads(FLOW_PATH.read_text(encoding="utf-8"))


def action(code: str) -> str:
    """What the panel offers for an error or decision (flow.json ``failures``)."""
    return flow()["failures"].get(code, {}).get("action", "manual")


def step_title(step_id: str) -> str:
    return next((s["title"] for s in flow()["steps"] if s["id"] == step_id), step_id)


def timeout(name: str) -> float:
    return float(flow()["timeouts"][name])


def poll_seconds(name: str = "poll_seconds") -> float:
    """How long to wait between two looks: GitHub's ``poll_seconds``, or
    Launchpad's ``release_poll_seconds`` (cheaper, so more often)."""
    override = os.environ.get("LAUNCHPAD_POLL_SECONDS", "").strip()
    try:
        return max(0.01, float(override)) if override else timeout(name)
    except ValueError:
        return timeout(name)


#: Where the GitHub token came from (set by the server): connections, gh or env.
SOURCE_WORDS = {"connections": "Connections", "gh": "this machine's gh login", "env": "the Space's environment"}


def github_source() -> str:
    return os.environ.get("LAUNCHPAD_GITHUB_SOURCE") or "connections"


def github_fix(code: str) -> str | None:
    """A gh login is fixed with gh itself (or replaced by connecting GitHub)."""
    if github_source() == "gh" and code == "github_workflow_scope":
        return ("Run `gh auth refresh -h github.com -s workflow` on this machine, or connect GitHub in "
                "Connections (its sign-in asks for the workflow scope).")
    if github_source() == "gh" and code == "github_token_invalid":
        return "Run `gh auth login` on this machine again, or connect GitHub in Connections."
    return None


def github_api() -> str:
    return (os.environ.get("LAUNCHPAD_GITHUB_API") or "https://api.github.com").rstrip("/")


def github_git() -> str:
    """Where repos are pushed and fetched: ``<base>/<owner>/<repo>.git``."""
    return (os.environ.get("LAUNCHPAD_GITHUB_GIT") or "https://github.com").rstrip("/")


def repo_web_url(full: str) -> str:
    """The repo's address as Launchpad and people know it."""
    return f"https://github.com/{full}"


def repo_git_url(full: str) -> str:
    return f"{github_git()}/{full}.git"


# ── events ───────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(kind: str, **fields: Any) -> None:
    print(json.dumps({"type": kind, "ts": now_iso(), **{k: v for k, v in fields.items() if v is not None}}),
          flush=True)


class Stop(Exception):
    """Ends the flow: an ``error``, or a ``needs`` decision for the owner. The
    message and fix default to flow.json's text for ``code``."""

    def __init__(self, code: str, message: str | None = None, *, kind: str = "error", fix: str | None = None,
                 data: dict[str, Any] | None = None, options: list[dict[str, str]] | None = None) -> None:
        info = flow()["failures"].get(code, {})
        self.code, self.kind = code, kind
        self.message = message or info.get("message") or code
        self.fix = fix if fix is not None else info.get("fix")
        self.data, self.options = data or {}, options
        super().__init__(self.message)


class Stopped(BaseException):
    """SIGTERM: the person stopped the deploy. A BaseException, so the
    executor kills the running command before it travels on."""


_current_step: str | None = None


class Step:
    """One flow.json step: emits running, then done / skipped / failed."""

    def __init__(self, step_id: str) -> None:
        self.id, self.title = step_id, step_title(step_id)
        self.message: str | None = None
        self.skipped = False
        self.started = time.monotonic()

    def __enter__(self) -> "Step":
        global _current_step
        _current_step = self.id
        self.started = time.monotonic()
        emit("step", step=self.id, title=self.title, status="running")
        return self

    def done(self, message: str) -> None:
        self.message = message

    def skip(self, message: str) -> None:
        self.message, self.skipped = message, True

    def __exit__(self, kind, exc, tb) -> bool:
        elapsed = round(time.monotonic() - self.started, 1)
        if kind is None:
            emit("step", step=self.id, title=self.title, status="skipped" if self.skipped else "done",
                 message=self.message, elapsed=elapsed)
        elif isinstance(exc, Stop):
            emit("step", step=self.id, title=self.title, status="waiting" if exc.kind == "needs" else "failed",
                 message=exc.message, elapsed=elapsed)
        else:
            emit("step", step=self.id, title=self.title, status="failed", elapsed=elapsed)
        return False


def progress(message: str, **data: Any) -> None:
    emit("progress", step=_current_step, message=message, data=data or None)


def warn(message: str) -> None:
    emit("warning", step=_current_step, message=message)


# ── launchpad.json ───────────────────────────────────────────────────────────

def _branch_ok(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 200 and not value.startswith(("-", "/"))
            and not value.endswith(("/", ".lock", ".")) and ".." not in value and "//" not in value
            and re.fullmatch(r"[A-Za-z0-9._/-]+", value) is not None)


def validate_config(data: Any) -> list[str]:
    """What is wrong with a launchpad.json document; empty when it is valid."""
    if not isinstance(data, dict):
        return ["it must be a JSON object"]
    errors: list[str] = []
    if "schema" in data and data["schema"] != 1:
        errors.append("schema must be 1")
    if "name" in data and not (isinstance(data["name"], str) and NAME_RE.fullmatch(data["name"])):
        errors.append("name must be 1-25 lowercase letters, digits or hyphens")
    if data.get("app_id") is not None and not (isinstance(data["app_id"], str) and APP_ID_RE.fullmatch(data["app_id"])):
        errors.append("app_id is not a Launchpad app id")
    if data.get("slug") is not None and not (isinstance(data["slug"], str) and SLUG_RE.fullmatch(data["slug"])):
        errors.append("slug is not a Launchpad slug")
    if data.get("url") is not None and not (isinstance(data["url"], str) and data["url"].startswith(("https://", "http://"))):
        errors.append("url must be an http(s) URL")
    if data.get("workflow") is not None and not (isinstance(data["workflow"], str)
                                                 and re.fullmatch(r"launchpad-[a-z0-9-]+\.ya?ml", data["workflow"])):
        errors.append("workflow must be the file name launchpad-<slug>.yml")
    repo = data.get("repo")
    if repo is not None:
        if not isinstance(repo, dict):
            errors.append("repo must be an object")
        else:
            if not (isinstance(repo.get("full_name"), str) and FULL_RE.fullmatch(repo["full_name"])):
                errors.append("repo.full_name must be owner/name")
            if repo.get("remote") is not None and not (isinstance(repo["remote"], str) and REMOTE_RE.fullmatch(repo["remote"])):
                errors.append("repo.remote is not a git remote name")
            if repo.get("branch") is not None and not _branch_ok(repo["branch"]):
                errors.append("repo.branch is not a branch name")
    if data.get("app_id") and not isinstance(repo, dict):
        errors.append("app_id needs repo: the GitHub repo the app builds from")
    port = data.get("port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
        errors.append("port must be a number from 1 to 65535")
    health = data.get("health_path")
    if health is not None and not (isinstance(health, str) and HEALTH_RE.fullmatch(health)):
        errors.append("health_path must start with / and have no spaces, or be null")
    env = data.get("env")
    if env is not None:
        if not isinstance(env, list):
            errors.append("env must be a list of {name, sensitive}")
        else:
            for i, item in enumerate(env):
                if not isinstance(item, dict) or not (isinstance(item.get("name"), str) and ENV_RE.fullmatch(item["name"])):
                    errors.append(f"env[{i}] needs a name made of letters, digits and _")
                    continue
                extra = set(item) - {"name", "sensitive"}
                if extra:
                    errors.append(f"env[{i}] may hold only name and sensitive, not {', '.join(sorted(extra))}: "
                                  "values go to Launchpad, never into the repo")
                if "sensitive" in item and not isinstance(item["sensitive"], bool):
                    errors.append(f"env[{i}].sensitive must be true or false")
    return errors


def read_config(project: Path) -> tuple[dict[str, Any] | None, str | None]:
    """``(config, None)``, ``(None, None)`` when there is none, or
    ``(None, error)`` when the file is unreadable or invalid."""
    path = project / CONFIG_NAME
    if not path.exists() and not path.is_symlink():
        return None, None
    try:
        if path.is_symlink() or not path.is_file():
            return None, "launchpad.json must be a regular file"
        if path.stat().st_size > CONFIG_MAX_BYTES:
            return None, "launchpad.json is too large"
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        return None, f"launchpad.json cannot be read ({exc.__class__.__name__})"
    except ValueError as exc:
        return None, f"launchpad.json is not valid JSON ({exc})"
    errors = validate_config(data)
    return (None, "; ".join(errors)) if errors else (data, None)


def ordered_config(data: dict[str, Any]) -> dict[str, Any]:
    out = {"schema": 1}
    for key in CONFIG_KEYS[1:]:
        if key in data:
            out[key] = data[key]
    out.update({k: v for k, v in data.items() if k not in out})
    return out


def write_config(project: Path, data: dict[str, Any]) -> bool:
    """Write launchpad.json atomically; False when it already says exactly that."""
    data = ordered_config(data)
    errors = validate_config(data)
    if errors:
        raise Stop("config_invalid", "launchpad.json would not be valid: " + "; ".join(errors))
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path = project / CONFIG_NAME
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeError):
        pass
    temporary = project / f".{CONFIG_NAME}.{os.getpid()}.tmp"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return True


# ── git, read from files only ────────────────────────────────────────────────

def _strip_userinfo(url: str) -> str:
    """A remote URL without credentials someone may have put in it."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if parts.scheme in ("http", "https") and "@" in parts.netloc:
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[1], parts.path, parts.query, ""))
    return url


def github_full(url: str | None) -> str | None:
    """``owner/repo`` for a GitHub remote URL (https, ssh or scp form), else None."""
    if not url:
        return None
    url = _strip_userinfo(url.strip())
    base = github_git()
    rest = None
    if url.startswith(base + "/"):
        rest = url[len(base) + 1:]
    else:
        match = (re.fullmatch(r"(?:https?|git)://(?:www\.)?github\.com/(.+)", url)
                 or re.fullmatch(r"ssh://git@(?:ssh\.)?github\.com(?::\d+)?/(.+)", url)
                 or re.fullmatch(r"git@github\.com:(.+)", url))
        rest = match.group(1) if match else None
    if rest is None:
        return None
    rest = rest.rstrip("/")
    if rest.endswith(".git"):
        rest = rest[:-4]
    return rest if FULL_RE.fullmatch(rest) else None


def _git_dirs(project: Path) -> tuple[Path, Path] | None:
    dotgit = project / ".git"
    if dotgit.is_dir():
        gitdir = dotgit
    elif dotgit.is_file():
        try:
            line = dotgit.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None
        if not line.startswith("gitdir:"):
            return None
        gitdir = Path(line[7:].strip())
        gitdir = gitdir if gitdir.is_absolute() else (project / gitdir)
    else:
        return None
    common = gitdir
    try:
        commondir = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        common = Path(commondir) if Path(commondir).is_absolute() else (gitdir / commondir)
    except (OSError, UnicodeError):
        pass
    return gitdir, common


def _read_ref(gitdir: Path, common: Path, ref: str) -> str | None:
    for base in (gitdir, common):
        try:
            value = (base / ref).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if SHA_RE.fullmatch(value):
            return value
    try:
        for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1].strip() == ref and SHA_RE.fullmatch(parts[0]):
                return parts[0]
    except (OSError, UnicodeError):
        pass
    return None


def _git_remotes(common: Path) -> dict[str, str]:
    remotes: dict[str, str] = {}
    section = None
    try:
        lines = (common / "config").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return remotes
    for raw in lines:
        line = raw.strip()
        header = re.fullmatch(r'\[\s*remote\s+"([^"]+)"\s*\]', line)
        if header:
            section = header.group(1)
            continue
        if line.startswith("["):
            section = None
            continue
        if section is not None:
            match = re.fullmatch(r"url\s*=\s*(.+)", line, re.IGNORECASE)
            if match and section not in remotes:
                remotes[section] = _strip_userinfo(match.group(1).strip().strip('"'))
    return remotes


def read_git(project: Path) -> dict[str, Any]:
    """The project's git state from its files (no command runs)."""
    dirs = _git_dirs(project)
    if dirs is None:
        return {"repo": False}
    gitdir, common = dirs
    try:
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return {"repo": False}
    branch = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else None
    reftable = (common / "reftable").is_dir()
    if head.startswith("ref: "):
        sha = _read_ref(gitdir, common, head[5:].strip())
    else:
        sha = head if SHA_RE.fullmatch(head) else None
    remotes = _git_remotes(common)
    origin = remotes.get("origin")
    return {
        "repo": True,
        "branch": branch,
        "detached": branch is None,
        "head": sha,
        "no_commits": sha is None and not reftable and branch is not None,
        "remotes": remotes,
        "origin": origin,
        "origin_github": github_full(origin),
        "_dirs": (str(gitdir), str(common)),
    }


def remote_head(git: dict[str, Any], remote: str | None, branch: str | None) -> str | None:
    """The last-fetched (or last-deployed) commit of ``remote``/``branch``."""
    if not git.get("repo") or not remote or not branch:
        return None
    gitdir, common = (Path(p) for p in git["_dirs"])
    return _read_ref(gitdir, common, f"refs/remotes/{remote}/{branch}")


# ── detection ────────────────────────────────────────────────────────────────

def _read_text(path: Path, limit: int = 256 * 1024) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _write_new(path: Path, text: str) -> None:
    """Create ``path`` with ``text``; never through a link, never over a file
    (one that appeared since the check is the person's)."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except FileExistsError:
        raise Stop("not_deployable", f"{path.name} appeared while deploying; check again.") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _read_json(path: Path) -> dict[str, Any] | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def slug_name(value: str) -> str:
    """A Launchpad app name from a folder name."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:25].strip("-")
    return slug or "app"


def repo_name_for(project: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", project.name).strip("-.")[:100]
    return name or "app"


def has_code(project: Path) -> bool:
    detect = flow()["detect"]
    suffixes, names, skip = set(detect["code_suffixes"]), set(detect["code_files"]), set(detect["skip_dirs"])
    budget = 4000
    stack = [(project, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            budget -= 1
            if budget < 0:
                return False
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if depth < 3 and entry.name not in skip and not entry.name.startswith("."):
                    stack.append((Path(entry.path), depth + 1))
            elif entry.name in names or Path(entry.name).suffix.lower() in suffixes:
                return True
    return False


def _logical_lines(text: str) -> list[str]:
    lines, buffer = [], ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        lines.append(buffer + stripped)
        buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


def dockerfile_port(text: str) -> tuple[int | None, str | None]:
    """The final stage's EXPOSE (resolving ${VAR} from ARG/ENV), else its ENV PORT."""
    values: dict[str, str] = {}
    exposed: int | None = None
    env_port: int | None = None
    for line in _logical_lines(text):
        word, _, rest = line.partition(" ")
        word = word.upper()
        if word == "FROM":
            values, exposed, env_port = {}, None, None
        elif word in ("ARG", "ENV"):
            pairs = re.findall(r'([A-Za-z_][A-Za-z0-9_]*)=("[^"]*"|\S+)', rest)
            if not pairs and word == "ENV":
                parts = rest.split(None, 1)
                pairs = [(parts[0], parts[1])] if len(parts) == 2 else []
            for key, value in pairs:
                values[key] = value.strip('"')
                if key == "PORT" and word == "ENV" and value.strip('"').isdigit():
                    env_port = int(value.strip('"'))
        elif word == "EXPOSE" and exposed is None:
            for token in rest.split():
                token = token.split("/", 1)[0]
                var = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)(?::-([0-9]+))?\}?", token)
                if var:
                    token = values.get(var.group(1)) or var.group(2) or ""
                if token.isdigit() and 1 <= int(token) <= 65535:
                    exposed = int(token)
                    break
    if exposed is not None:
        return exposed, "Dockerfile EXPOSE"
    if env_port is not None and 1 <= env_port <= 65535:
        return env_port, "Dockerfile ENV PORT"
    return None, None


_PORT_PATTERNS = (
    re.compile(r"\.listen\(\s*(\d{2,5})\b"),
    re.compile(r"\bPORT\b[^\n]{0,40}?(?:\|\||\?\?|\bor\b|,)\s*['\"]?(\d{2,5})\b"),
    re.compile(r"\bport\s*[:=]\s*(\d{2,5})\b"),
)


def _port_in(text: str) -> int | None:
    for pattern in _PORT_PATTERNS:
        match = pattern.search(text)
        if match and 1 <= int(match.group(1)) <= 65535:
            return int(match.group(1))
    return None


def _port_in_command(command: str) -> int | None:
    match = re.search(r"(?:--port[= ]|-p\s+|--bind[= ]\S*:|-b\s+\S*:|PORT=)(\d{2,5})\b", command)
    return int(match.group(1)) if match and 1 <= int(match.group(1)) <= 65535 else None


def _health_in(texts: Iterable[str]) -> str | None:
    joined = "\n".join(texts)
    for path in flow()["detect"]["health_paths"]:
        if re.search(r"""['"`]""" + re.escape(path) + r"""['"`]""", joined):
            return path
    return None


def _node_version(project: Path, pkg: dict[str, Any]) -> str:
    engines = (pkg.get("engines") or {}).get("node") if isinstance(pkg.get("engines"), dict) else None
    for source in (engines, _read_text(project / ".nvmrc"), _read_text(project / ".node-version")):
        if isinstance(source, str):
            match = re.search(r"(\d{2})", source)
            if match and 18 <= int(match.group(1)) <= 40:
                return match.group(1)
    return flow()["detect"]["default_node"]


def _python_version(project: Path) -> str:
    for name, pattern in ((".python-version", r"(3\.\d{1,2})"), ("runtime.txt", r"python-(3\.\d{1,2})")):
        text = _read_text(project / name)
        match = re.search(pattern, text or "")
        if match:
            return match.group(1)
    return flow()["detect"]["default_python"]


def _node_plan(project: Path, pkg: dict[str, Any]) -> dict[str, Any]:
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    deps: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        if isinstance(pkg.get(key), dict):
            deps.update(pkg[key])
    if (project / "pnpm-lock.yaml").is_file():
        manager, lock, install = "pnpm", "pnpm-lock.yaml", "corepack enable && pnpm install --frozen-lockfile"
    elif (project / "yarn.lock").is_file():
        immutable = "--immutable" if (project / ".yarnrc.yml").is_file() else "--frozen-lockfile"
        manager, lock, install = "yarn", "yarn.lock", f"corepack enable && yarn install {immutable}"
    elif (project / "package-lock.json").is_file():
        manager, lock, install = "npm", "package-lock.json", "npm ci"
    else:
        manager, lock, install = "npm", None, "npm install"
    manifests = " ".join(["package.json", *([lock] if lock else [])])
    run_build = f"{manager} run build" if manager != "yarn" else "yarn build"
    start = scripts.get("start") if isinstance(scripts.get("start"), str) else None
    build = scripts.get("build") if isinstance(scripts.get("build"), str) else None
    values = {"node": _node_version(project, pkg), "manifests": manifests, "install": install}
    dev_server = bool(start) and re.search(r"\b(vite|react-scripts start|webpack(-dev-server| serve)|ng serve)\b", start)
    if "next" in deps and build and start:
        port = _port_in_command(start) or 3000
        return {"template": "node", "stack": "node", "port": port, "port_source": "Next.js", "confident": True,
                "values": {**values, "build": run_build, "start": json.dumps([manager, "start"])}}
    if start and not dev_server:
        texts = [t for t in (_read_text(project / f) for f in _node_entries(project, pkg)) if t]
        port = _port_in_command(start)
        source = "package.json start script" if port else None
        if port is None:
            port = next((p for p in (_port_in(t) for t in texts) if p), None)
            source = "the server code" if port else None
        return {"template": "node", "stack": "node", "port": port or 3000,
                "port_source": source or "default (the app should read $PORT)", "confident": False,
                "health": _health_in(texts),
                "values": {**values, "build": run_build if build else "", "start": json.dumps([manager, "start"])}}
    if build and ("vite" in deps or "react-scripts" in deps):
        out = "dist" if "vite" in deps else "build"
        return {"template": "node-static", "stack": "node", "port": 80, "port_source": "nginx", "confident": True,
                "values": {**values, "build": run_build, "out": out}}
    reason = ("package.json has no start script, so how to run the app is not clear" if not start
              else "package.json's start script runs a development server, not a production one")
    return {"reason": reason}


def _node_entries(project: Path, pkg: dict[str, Any]) -> list[str]:
    entries = list(flow()["detect"]["node_entries"])
    main = pkg.get("main")
    if isinstance(main, str) and ".." not in main and not main.startswith("/"):
        entries.insert(0, main)
    return [e for e in entries if (project / e).is_file()]


def _python_plan(project: Path) -> dict[str, Any]:
    requirements = (_read_text(project / "requirements.txt") or "").lower()
    texts = {name: _read_text(project / name) for name in flow()["detect"]["python_entries"]}
    texts = {name: text for name, text in texts.items() if text}
    values = {"python": _python_version(project)}
    procfile = _read_text(project / "Procfile") or ""
    web = re.search(r"^web:\s*(.+)$", procfile, re.MULTILINE)
    health = _health_in(texts.values())
    if web:
        command = web.group(1).strip()
        port = _port_in_command(command)
        return {"template": "python", "stack": "python", "port": port or 8000,
                "port_source": "Procfile" if port else "default (the Procfile reads $PORT)",
                "confident": bool(port) or "$PORT" in command or "${PORT" in command, "health": health,
                "values": {**values, "start": json.dumps(["sh", "-c", command])}}
    for name, text in texts.items():
        module = name[:-3]
        fastapi = re.search(r"^(\w+)\s*=\s*FastAPI\(", text, re.MULTILINE)
        if fastapi and re.search(r"^uvicorn\b", requirements, re.MULTILINE):
            target = f"{module}:{fastapi.group(1)}"
            return {"template": "python", "stack": "python", "port": 8000, "port_source": "FastAPI (uvicorn)",
                    "confident": True, "health": health,
                    "values": {**values, "start": json.dumps(["uvicorn", target, "--host", "0.0.0.0", "--port",
                                                              "{{port}}"])}}
        flask = re.search(r"^(\w+)\s*=\s*Flask\(", text, re.MULTILINE)
        if flask and re.search(r"^gunicorn\b", requirements, re.MULTILINE):
            target = f"{module}:{flask.group(1)}"
            return {"template": "python", "stack": "python", "port": 8000, "port_source": "Flask (gunicorn)",
                    "confident": True, "health": health,
                    "values": {**values, "start": json.dumps(["gunicorn", "--bind", "0.0.0.0:{{port}}", target])}}
    return {"reason": "requirements.txt is there, but no Procfile web command, FastAPI app with uvicorn or "
                      "Flask app with gunicorn says how to start the app"}


def render_template(name: str, values: dict[str, str]) -> str:
    """A flow.json Dockerfile template with ``{{key}}`` filled in. A line
    whose placeholder is empty is left out (an optional build step)."""
    lines = []
    for line in flow()["templates"][name]["dockerfile"]:
        if any(values.get(k) == "" for k in re.findall(r"\{\{(\w+)\}\}", line)):
            continue
        for _pass in range(3):                  # a value may hold {{port}} itself (a start command)
            keys = re.findall(r"\{\{(\w+)\}\}", line)
            if not keys:
                break
            for key in keys:
                if key not in values:
                    raise KeyError(f"template {name} needs {key}")
                line = line.replace("{{" + key + "}}", values[key])
        lines.append(line)
    return "\n".join(lines).rstrip("\n") + "\n"


def detect_build(project: Path) -> dict[str, Any]:
    """How the project builds: its Dockerfile, or a template for a known stack."""
    reasons: list[str] = []
    dockerfile = _read_text(project / "Dockerfile")
    pkg = _read_json(project / "package.json")
    build: dict[str, Any] = {"dockerfile": None, "stack": None, "template": None, "port": None,
                             "port_source": None, "port_confident": False, "health_path": None,
                             "dockerfile_preview": None}
    if dockerfile is None and os.path.lexists(project / "Dockerfile"):
        # A link, a folder, an unreadable or a very large file: the person's,
        # and a template written over it would replace what it points to.
        build["reasons"] = ["the Dockerfile is a link, a folder or larger than 256 KB, so the script can't "
                            "read it and won't replace it"]
        return build
    if dockerfile is not None:
        port, source = dockerfile_port(dockerfile)
        build.update(dockerfile="Dockerfile", stack="docker", port=port, port_source=source,
                     port_confident=port is not None)
        texts = [t for t in (_read_text(project / f) for f in _node_entries(project, pkg or {})) if t]
        if pkg:
            if port is None:
                plan = _node_plan(project, pkg)
                if plan.get("port") and plan.get("port_source", "").startswith(("package.json", "the server", "Next")):
                    build.update(port=plan["port"], port_source=plan["port_source"])
        texts += [t for t in (_read_text(project / f) for f in flow()["detect"]["python_entries"]) if t]
        if build["port"] is None:
            port = next((p for p in (_port_in(t) for t in texts) if p), None)
            if port:
                build.update(port=port, port_source="the server code")
        build["health_path"] = _health_in(texts)
        return build
    compose = [n for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
               if (project / n).is_file()]
    nested = sorted(str(p.relative_to(project)) for p in project.glob("*/Dockerfile")
                    if not any(part.startswith(".") or part == "node_modules" for part in p.relative_to(project).parts))
    plan: dict[str, Any] = {}
    if pkg is not None:
        if isinstance(pkg.get("workspaces"), (list, dict)):
            plan = {"reason": "package.json declares workspaces: a monorepo needs choosing which app to deploy"}
        else:
            plan = _node_plan(project, pkg)
    elif (project / "requirements.txt").is_file():
        plan = _python_plan(project)
    elif (project / "index.html").is_file():
        servers = sorted({p.name for p in project.glob("*.py")}
                         | {f for f in ("go.mod", "Cargo.toml", "Gemfile", "composer.json", "pom.xml", "build.gradle",
                                        "pyproject.toml", "server.js", "server.mjs", "app.js")
                            if (project / f).is_file()})
        if servers:
            plan = {"reason": f"index.html sits beside server code ({', '.join(servers[:3])}), so serving the folder "
                              "as a static site may not be what it needs"}
        else:
            plan = {"template": "static", "stack": "static", "port": 80, "port_source": "nginx", "confident": True,
                    "values": {}}
    else:
        plan = {"reason": "no Dockerfile, and the project is not a stack the script knows how to containerise "
                          "(a static site, a Node app with a start script, a Python web app)"}
    if plan.get("template"):
        build.update(stack=plan["stack"], template=plan["template"], port=plan["port"],
                     port_source=plan["port_source"], port_confident=plan["confident"],
                     health_path=plan.get("health"))
        build["_values"] = plan["values"]
        build["dockerfile_preview"] = render_template(plan["template"], {**plan["values"], "port": str(plan["port"])})
    else:
        reasons.append(plan.get("reason", "the project's build is not clear"))
    if compose:
        reasons.append(f"{compose[0]} describes several services; Launchpad runs one container")
    if nested:
        reasons.append("the Dockerfiles are in sub-folders (" + ", ".join(nested[:3]) + "), not at the root")
    build["reasons"] = reasons
    return build


_PLACEHOLDER = re.compile(r"your|change|replace|example|xxx|<|>|\.\.\.|todo", re.IGNORECASE)


def env_names(project: Path) -> list[dict[str, Any]]:
    """Variable names from the committed example env files. A non-sensitive
    name keeps its example value as ``default`` unless it is a placeholder;
    a sensitive one never carries a value."""
    detect = flow()["detect"]
    sensitive = re.compile(detect["sensitive"], re.IGNORECASE)
    found: dict[str, dict[str, Any]] = {}
    for name in detect["env_files"]:
        text = _read_text(project / name)
        for line in (text or "").splitlines():
            match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
            if match and match.group(1) not in found:
                key = match.group(1)
                secret = bool(sensitive.search(key))
                value = match.group(2).split(" #", 1)[0].strip().strip("'\"")
                item = {"name": key, "sensitive": secret, "source": name}
                if not secret and value and len(value) <= 200 and not _PLACEHOLDER.search(value):
                    item["default"] = value
                found[key] = item
    return list(found.values())


def is_sensitive(name: str) -> bool:
    return bool(re.search(flow()["detect"]["sensitive"], name, re.IGNORECASE))


SUMMARIES = {
    "linked": "Linked to the Launchpad app {name}: deploying pushes {branch} and rolls out a new release.",
    "ready": "Ready to deploy as {name} on port {port}.",
    "detectable": "Deployable as {name} once you confirm the details.",
    "needs-agent": "Not deployable as it is: an agent can prepare it.",
    "not-deployable": "Nothing to deploy yet.",
}


def check(project: str | Path) -> dict[str, Any]:
    """The project's deploy state and the fields filled in for the form.
    Reads files only and runs nothing; safe to call anywhere, any time."""
    project = Path(project).expanduser()
    out: dict[str, Any] = {"schema": 1, "project": project.name, "state": "not-deployable", "summary": "",
                           "reasons": [], "blockers": [], "warnings": [], "config": None, "config_error": None,
                           "fields": {}, "build": {}, "git": {"repo": False}}
    if not project.is_dir():
        out["reasons"].append("the project folder is missing")
        out["summary"] = SUMMARIES["not-deployable"]
        return out
    config, error = read_config(project)
    git = read_git(project)
    build = detect_build(project)
    out["config"], out["config_error"] = config, error
    cfg = config or {}
    repo_cfg = cfg.get("repo") or {}

    port = cfg.get("port") or build["port"]
    port_source = "launchpad.json" if cfg.get("port") else build["port_source"]
    if "health_path" in cfg:
        health, health_source = cfg["health_path"], "launchpad.json"
    elif build.get("health_path"):
        health, health_source = build["health_path"], "the server code"
    else:
        health, health_source = None, "default: Launchpad checks that the port accepts connections"
    examples = {e["name"]: e for e in env_names(project)}
    declared = []
    for e in cfg.get("env") or []:
        item = {"name": e["name"], "sensitive": e.get("sensitive", is_sensitive(e["name"])), "source": CONFIG_NAME}
        if not item["sensitive"] and "default" in examples.get(e["name"], {}):
            item["default"] = examples[e["name"]]["default"]
        declared.append(item)
    known = {e["name"] for e in declared}
    detected = [e for name, e in examples.items() if name not in known]
    branch = repo_cfg.get("branch") or git.get("branch")
    remote = repo_cfg.get("remote") or (None if not repo_cfg else DEFAULT_REMOTE)
    out["fields"] = {
        "name": {"value": cfg.get("name") or slug_name(project.name),
                 "source": CONFIG_NAME if cfg.get("name") else "the project folder"},
        "port": {"value": port, "source": port_source,
                 "confident": bool(cfg.get("port")) or bool(build["port_confident"])},
        "health_path": {"value": health, "source": health_source},
        "env": declared + detected,
        "repo": {"full_name": repo_cfg.get("full_name"), "remote": remote, "branch": branch,
                 "origin": git.get("origin_github"), "new_repo_name": repo_name_for(project)},
    }
    public_build = {k: v for k, v in build.items() if not k.startswith("_")}
    out["build"] = public_build
    out["git"] = {k: v for k, v in git.items() if not k.startswith("_") and k != "remotes"}
    if git.get("repo"):
        out["git"]["remotes"] = sorted(git.get("remotes") or {})
        out["git"]["deployed_head"] = remote_head(git, remote, branch) if repo_cfg else None

    reasons, warnings, blockers = out["reasons"], out["warnings"], out["blockers"]
    if error:
        state = "needs-agent"
        reasons.append(f"launchpad.json is not valid: {error}")
    elif not has_code(project):
        state = "not-deployable"
        reasons.append("the project has no code yet")
    elif cfg.get("app_id"):
        state = "linked"
        if build["dockerfile"] is None and not os.path.lexists(project / "Dockerfile"):
            warnings.append("there is no Dockerfile at the project root, so the build will fail")
    elif not git.get("repo"):
        state = "needs-agent"
        reasons.append("the project is not a git repository yet")
    elif git.get("no_commits"):
        state = "needs-agent"
        reasons.append("the repository has no commits yet")
    elif build["dockerfile"]:
        if port and (cfg.get("port") or build["port_confident"]):
            state = "ready"
        else:
            state = "detectable"
            reasons.append(f"the Dockerfile does not declare the port (EXPOSE); {port} was found in "
                           f"{build['port_source']}: confirm it" if port
                           else "the Dockerfile does not declare the port (EXPOSE): enter it")
    elif build["template"]:
        state = "detectable"
        reasons.append(f"there is no Dockerfile: a {build['template']} Dockerfile will be added")
        if not build["port_confident"]:
            reasons.append(f"port {port} is a guess ({build['port_source']}): confirm it")
    else:
        state = "needs-agent"
        reasons.extend(build.get("reasons") or ["the project's build is not clear"])
    if state in ("linked", "ready", "detectable") and git.get("repo"):
        if git.get("detached"):
            blockers.append("no branch is checked out in the project folder")
        elif repo_cfg.get("branch") and git.get("branch") != repo_cfg["branch"]:
            blockers.append(f"the app deploys {repo_cfg['branch']}, but the project folder is on {git.get('branch')}")
    if state == "linked" and not git.get("repo"):
        blockers.append("the project is not a git repository")
    out["state"] = state
    out["summary"] = SUMMARIES[state].format(name=out["fields"]["name"]["value"], port=port, branch=branch)
    return out


# ── commands ─────────────────────────────────────────────────────────────────

_NEVER_PASS = ("XO_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "LAUNCHPAD_SECRETS")


def base_env() -> dict[str, str]:
    """This process's environment minus credentials and git config overrides."""
    return {k: v for k, v in os.environ.items()
            if k not in _NEVER_PASS and not k.startswith(("GIT_CONFIG_", "LAUNCHPAD_VALUE_"))}


def run(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        seconds: float | None = None, quiet: bool = False):
    return run_sync(argv, cwd=cwd, env=env, timeout=seconds or timeout("command_seconds"), separate_stderr=True,
                    log_output=not quiet, log_label="launchpad")


def _first_line(text: str, limit: int = 400) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "")[:limit]


class GitFailed(Exception):
    def __init__(self, args: tuple[str, ...], output: str) -> None:
        self.args_, self.output = args, output
        super().__init__(f"git {args[0] if args else ''} failed: {_first_line(output)}")


#: Every git command runs with the repository's hooks and fsmonitor off (as
#: XO's own git does, services/git_safe.py): a hook or an fsmonitor command is
#: code an agent may have written into the project, and a push carries the
#: GitHub token in its environment. ``--no-verify`` alone leaves pre-push,
#: post-commit and post-merge running.
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


class Git:
    def __init__(self, project: Path, token: str | None = None) -> None:
        self.project, self.token = project, token
        self.identity: dict[str, str] = {}

    def env(self, network: bool) -> dict[str, str]:
        env = {**base_env(), "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", **self.identity}
        if network:
            # Only the transport the script pushes and fetches with
            # (https; file in tests), whatever the repository's config says.
            env["GIT_ALLOW_PROTOCOL"] = urllib.parse.urlsplit(github_git()).scheme or "https"
        if network and self.token:
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env.update({"GIT_CONFIG_COUNT": "2",
                        "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
                        "GIT_CONFIG_KEY_1": f"http.{github_git()}/.extraheader",
                        "GIT_CONFIG_VALUE_1": f"AUTHORIZATION: basic {basic}"})
        return env

    def run(self, *args: str, network: bool = False, check: bool = True, seconds: float | None = None):
        result = run(["git", *GIT_SAFE, "-C", str(self.project), *args], env=self.env(network), seconds=seconds)
        if check and not result.ok:
            raise GitFailed(args, (result.stderr or "") + (result.output or ""))
        return result

    def out(self, *args: str, **kw: Any) -> str:
        return self.run(*args, **kw).output.strip()

    def head(self) -> str:
        return self.out("rev-parse", "HEAD")

    def has_commit(self, sha: str) -> bool:
        return self.run("cat-file", "-e", f"{sha}^{{commit}}", check=False).ok

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self.run("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0

    def remote_branch(self, full: str, branch: str) -> str | None:
        out = self.out("ls-remote", "--heads", repo_git_url(full), f"refs/heads/{branch}", network=True,
                       seconds=timeout("command_seconds"))
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.strip() == f"refs/heads/{branch}":
                return sha.strip()
        return None

    def remote_heads(self, full: str) -> list[str]:
        out = self.out("ls-remote", "--heads", repo_git_url(full), network=True)
        return [line.split("\t", 1)[0] for line in out.splitlines() if "\t" in line]

    def fetch(self, full: str, branch: str, remote: str) -> None:
        self.run("fetch", "--no-tags", repo_git_url(full), f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
                 network=True, seconds=timeout("push_seconds"))

    def push(self, full: str, branch: str, remote: str) -> None:
        self.run("push", repo_git_url(full), f"HEAD:refs/heads/{branch}", network=True,
                 seconds=timeout("push_seconds"))
        self.run("update-ref", f"refs/remotes/{remote}/{branch}", "HEAD", check=False)

    def changed(self, base: str, tip: str) -> list[str]:
        return [p for p in self.out("diff", "--name-only", base, tip).splitlines() if p]

    def only_launchpad_files(self, base: str, tip: str) -> bool:
        paths = self.changed(base, tip)
        return bool(paths) and all(WORKFLOW_RE.fullmatch(p) for p in paths)

    def tracked(self, path: str) -> bool:
        return self.run("ls-files", "--error-unmatch", "--", path, check=False).ok

    def dirty_count(self) -> int:
        out = self.out("status", "--porcelain", "--untracked-files=no", check=False)
        return len([line for line in out.splitlines() if line.strip()])

    def commit(self, paths: list[str], message: str) -> str | None:
        """Commit only ``paths`` (whatever else is staged stays staged). None
        when they hold no change."""
        present = [p for p in paths if (self.project / p).exists()]
        gone = [p for p in paths if p not in present]
        if present:
            self.run("add", "-A", "-f", "--", *present)
        if gone:
            self.run("rm", "-q", "--cached", "--ignore-unmatch", "--", *gone)
        if self.run("diff", "--cached", "--quiet", "--", *paths, check=False).returncode == 0:
            return None
        self.run("commit", "--no-verify", "-m", message, "--", *paths)
        return self.head()

    def commit_empty(self, message: str) -> str:
        self.run("commit", "--no-verify", "--allow-empty", "--only", "-m", message)
        return self.head()

    def remote_set(self, name: str, full: str) -> str:
        """Make ``name`` point at ``full``: "added", "same" or "elsewhere"."""
        current = self.run("remote", "get-url", name, check=False)
        if not current.ok:
            self.run("remote", "add", name, repo_git_url(full))
            return "added"
        return "same" if github_full(current.output.strip()) == full else "elsewhere"


def ensure_identity(git: "Git", gh: "GitHub | None", user: dict[str, Any] | None = None) -> None:
    """Commit as the git user when one is configured, else as the connected
    GitHub account (its noreply address), else as XO Space."""
    if git.run("config", "--get", "user.email", check=False).output.strip():
        return
    if user is None and gh is not None:
        try:
            status, payload, _ = gh.call("GET", "/user")
            user = payload if status == 200 and isinstance(payload, dict) else None
        except Stop:
            user = None
    login = (user or {}).get("login")
    if login:
        email = (f"{user['id']}+{login}@users.noreply.github.com" if user.get("id")
                 else f"{login}@users.noreply.github.com")
        who = str(user.get("name") or login)
    else:
        email, who = "xo-space@users.noreply.github.com", "XO Space"
    git.identity = {"GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": email,
                    "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": email}


class GitHub:
    class _SameHost(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
                return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    def __init__(self, token: str) -> None:
        self.token = token
        self.opener = urllib.request.build_opener(self._SameHost())

    def call(self, method: str, path: str, body: Any = None, query: dict[str, Any] | None = None):
        """``(status, json, headers)``; raises Stop when GitHub can't be reached."""
        url = github_api() + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT,
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read()
                status, headers = response.status, {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            raw, status, headers = exc.read(), exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Stop("github_unreachable") from None
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        if status == 403 and headers.get("x-ratelimit-remaining") == "0":
            raise Stop("github_unreachable", "GitHub's API rate limit is used up for now.",
                       fix="Wait a few minutes, then try again.")
        return status, payload, headers

    def repo(self, full: str) -> tuple[int, dict[str, Any]]:
        status, payload, _ = self.call("GET", f"/repos/{full}")
        return status, payload if isinstance(payload, dict) else {}

    def runs(self, full: str, **query: Any) -> list[dict[str, Any]]:
        status, payload, _ = self.call("GET", f"/repos/{full}/actions/runs", query={"per_page": 30, **query})
        return list((payload or {}).get("workflow_runs") or []) if status == 200 else []

    def run(self, full: str, run_id: int) -> dict[str, Any] | None:
        status, payload, _ = self.call("GET", f"/repos/{full}/actions/runs/{run_id}")
        return payload if status == 200 and isinstance(payload, dict) else None

    def jobs(self, full: str, run_id: int) -> list[dict[str, Any]]:
        status, payload, _ = self.call("GET", f"/repos/{full}/actions/runs/{run_id}/jobs")
        return list((payload or {}).get("jobs") or []) if status == 200 else []

    def job_log_tail(self, full: str, job_id: int, lines: int = 40) -> str | None:
        """The last lines of a job's log. GitHub answers with a redirect to
        a signed URL on another host, which is fetched without the token."""
        request = urllib.request.Request(f"{github_api()}/repos/{full}/actions/jobs/{job_id}/logs", headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT})
        try:
            try:
                with self.opener.open(request, timeout=30) as response:
                    raw = response.read(4 * 1024 * 1024)
            except urllib.error.HTTPError as exc:
                location = exc.headers.get("Location") if exc.code in (301, 302, 303, 307, 308) else None
                if not location or not location.startswith("https://"):
                    return None
                signed = urllib.request.Request(location, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(signed, timeout=30) as response:
                    raw = response.read(4 * 1024 * 1024)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        text = raw.decode("utf-8", errors="replace")
        kept = [re.sub(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ", "", line) for line in text.splitlines()]
        kept = [line for line in kept if line.strip()]
        return "\n".join(kept[-lines:]) or None


class LaunchpadFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    @property
    def not_found(self) -> bool:
        return "HTTP 404" in self.message or "App not found" in self.message

    def stop(self, code: str = "create_failed") -> Stop:
        text = self.message
        if "HTTP 401" in text or "authentication rejected" in text or "Not signed in" in text:
            return Stop("launchpad_key_invalid")
        if "Could not reach Launchpad" in text or "HTTP 502" in text or "HTTP 503" in text:
            return Stop("launchpad_unreachable", f"Launchpad could not be reached: {text}")
        if "HTTP 429" in text:
            return Stop("launchpad_limit", f"Launchpad refused: {text}")
        if "HTTP 403" in text or "access denied" in text or "HTTP 402" in text:
            return Stop("launchpad_forbidden", f"Launchpad refused: {text}")
        if ".github/workflows" in text and "404" in text:
            return Stop("github_workflow_scope", fix=flow()["failures"]["github_workflow_scope"]["fix"],
                        data={"launchpad": text})
        return Stop(code, f"{flow()['failures'][code]['message']} Launchpad said: {text}")


class Launchpad:
    def __init__(self, key: str) -> None:
        self.key = key
        self.bin = os.environ.get("XO_LAUNCHPAD_BIN") or str(Path(sys.executable).with_name("xo-launchpad"))

    def available(self) -> bool:
        return os.path.isfile(self.bin) and os.access(self.bin, os.X_OK)

    def call(self, *args: str, extra_env: dict[str, str] | None = None, seconds: float | None = None) -> Any:
        env = {**base_env(), "XO_API_KEY": self.key, **(extra_env or {})}
        result = run([self.bin, "--json", "--yes", *args], env=env, seconds=seconds, quiet=True)
        if result.binary_missing:
            raise Stop("launchpad_cli_missing")
        if not result.ok:
            message = _first_line(result.stderr or result.output) or f"exit {result.returncode}"
            raise LaunchpadFailed(message.removeprefix("Error: "))
        try:
            return json.loads(result.output) if result.output.strip() else None
        except ValueError:
            raise LaunchpadFailed("xo-launchpad printed something that is not JSON") from None


def app_repo(app: dict[str, Any]) -> str | None:
    params = app.get("params") if isinstance(app.get("params"), dict) else {}
    url = params.get("repoUrl")
    return github_full(url if isinstance(url, str) else None)


def app_summary(app: dict[str, Any]) -> dict[str, Any]:
    params = app.get("params") if isinstance(app.get("params"), dict) else {}
    return {"id": app.get("id"), "name": app.get("name"), "slug": app.get("slug"), "source": app.get("source"),
            "status": app.get("status"), "url": app.get("public_url") or app.get("url"),
            "repo": app_repo(app), "branch": params.get("branch"), "port": app.get("port")}


def workflow_name(slug: str | None) -> str | None:
    return f"launchpad-{slug}.yml" if slug else None


def _run_workflow(run_: dict[str, Any]) -> str:
    return os.path.basename(str(run_.get("path") or "").split("@", 1)[0])


def probe_url(url: str, seconds: float) -> int | None:
    """The URL's HTTP status; retried while it answers 502-504 or nothing."""
    deadline = time.monotonic() + seconds
    status: int | None = None
    while True:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status not in (502, 503, 504):
                return status
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            status = None
        if time.monotonic() >= deadline:
            return status
        time.sleep(min(2.0, poll_seconds()))


# ── following a build and a release ──────────────────────────────────────────

class Follow:
    """The build and release waits deploy and watch share."""

    def __init__(self, lp: Launchpad, gh: GitHub | None, full: str | None, branch: str | None) -> None:
        self.lp, self.gh, self.full, self.branch = lp, gh, full, branch

    def find_run(self, workflow: str | None, sha: str | None) -> dict[str, Any] | None:
        if self.gh is None or not self.full:
            return None
        runs = self.gh.runs(self.full, **({"head_sha": sha} if sha else {"branch": self.branch}))
        exact = [r for r in runs if workflow and _run_workflow(r) == workflow]
        if not exact:
            # The name can be a guess (Launchpad's workflow commit had not
            # arrived): another Launchpad workflow's run is taken only while it
            # is the only one, since two apps can build the same branch.
            others = [r for r in runs if _run_workflow(r).startswith("launchpad-")]
            exact = others if workflow is None or len({_run_workflow(r) for r in others}) == 1 else []
        return max(exact, key=lambda r: int(r.get("id") or 0), default=None)

    def wait_for_run(self, workflow: str | None, sha: str | None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout("build_start_seconds")
        said = False
        while True:
            found = self.find_run(workflow, sha)
            if found:
                return found
            if time.monotonic() >= deadline:
                raise Stop("build_not_started", f"No GitHub Actions build started for {(sha or '')[:7] or 'the push'}"
                                                f"{' with ' + workflow if workflow else ''}.",
                           fix=f"Check that Actions is enabled for {self.full} and that "
                               f".github/workflows/{workflow or 'launchpad-<slug>.yml'} exists on {self.branch}.")
            if not said:
                progress("Waiting for GitHub Actions to start the build")
                said = True
            time.sleep(poll_seconds())

    def build(self, run_: dict[str, Any]) -> dict[str, Any]:
        """Follow a run to its end; raises Stop unless it succeeds."""
        assert self.gh is not None and self.full
        deadline = time.monotonic() + timeout("build_seconds")
        last = None
        run_id = int(run_["id"])
        while True:
            status, conclusion = run_.get("status"), run_.get("conclusion")
            jobs = self.gh.jobs(self.full, run_id) if status in ("in_progress", "completed") else []
            current = next((s.get("name") for j in jobs for s in (j.get("steps") or [])
                            if s.get("status") == "in_progress"), None)
            key = (status, current)
            if key != last:
                words = {"queued": "Build queued", "waiting": "Build waiting", "requested": "Build requested",
                         "pending": "Build pending", "in_progress": "Building", "completed": "Build finished"}
                progress(words.get(str(status), f"Build {status}") + (f": {current}" if current else ""),
                         run_id=run_id, run_url=run_.get("html_url"), status=status)
                last = key
            if status == "completed":
                if conclusion == "success":
                    return run_
                job = next((j for j in jobs if j.get("conclusion") in ("failure", "timed_out", "cancelled")), None)
                failed = next((s.get("name") for s in ((job or {}).get("steps") or [])
                               if s.get("conclusion") == "failure"), None)
                tail = self.gh.job_log_tail(self.full, int(job["id"])) if job and job.get("id") else None
                raise Stop("build_failed",
                           f"The GitHub Actions build {conclusion or 'failed'}"
                           + (f" at the step “{failed}”." if failed else "."),
                           data={"run_url": run_.get("html_url"), "failed_step": failed, "conclusion": conclusion,
                                 "log_tail": tail})
            if time.monotonic() >= deadline:
                raise Stop("build_timeout", data={"run_url": run_.get("html_url")})
            time.sleep(poll_seconds())
            run_ = self.gh.run(self.full, run_id) or run_

    def release(self, app_id: str, *, after: str | None, expect_new: bool) -> dict[str, Any] | None:
        """Wait for the app's release (a new one, past ``after``, when
        ``expect_new``) to be ready. None when there is none and none is coming."""
        deadline = time.monotonic() + timeout("release_seconds")
        last = None
        while True:
            try:
                status = self.lp.call("app", "status", app_id)
            except LaunchpadFailed as exc:
                raise exc.stop("release_failed") from None
            app, live, release = status.get("app") or {}, status.get("status") or {}, status.get("release") or {}
            rid = release.get("id")
            if app.get("status") == "stopped":
                raise Stop("release_failed", "The app is stopped on Launchpad.", fix="Start it in Launchpad.")
            replicas: dict[str, Any] = {}
            if expect_new and (rid is None or rid == after):
                note = "Waiting for Launchpad to pick up the new image"
            elif rid is None:
                return None
            else:
                rollout = release.get("rollout_status")
                if rollout == "ready" and app.get("status") == "running":
                    return status
                if rollout in ("failed", "timeout", "stopped"):
                    diagnosis = live.get("diagnosis") or {}
                    why = release.get("failure") or diagnosis.get("message") or release.get("rollout_reason") or rollout
                    data = {"release": release.get("number"), "diagnosis": diagnosis or None,
                            "port": app.get("port"), "exposed_ports": release.get("exposed_ports"),
                            "health_path": app.get("health_path")}
                    kind = diagnosis.get("kind")
                    if kind in ("port", "health_path"):
                        exposed = [p for p in (release.get("exposed_ports") or []) if p != app.get("port")]
                        if kind == "port":
                            fix = (f"The app does not answer on port {app.get('port')}"
                                   + (f" (the image declares {', '.join(map(str, exposed))})" if exposed else "")
                                   + f". Make it listen on 0.0.0.0:{app.get('port')}, or deploy again with the "
                                     "port it really uses.")
                        else:
                            fix = (f"{app.get('health_path')} does not answer"
                                   + (f" (HTTP {diagnosis.get('status_code')})" if diagnosis.get("status_code") else "")
                                   + ". Make it answer, or deploy again with another health path.")
                        raise Stop("health_check_failed", f"Release #{release.get('number')} failed its health "
                                                          f"check: {why}", fix=fix, data={**data, "kind": kind})
                    raise Stop("release_failed", f"Release #{release.get('number')} failed: {why}", data=data)
                replicas = live.get("replicas") or {}
                note = f"Release #{release.get('number')}: {rollout or 'starting'}"
                if replicas:
                    note += f" ({replicas.get('available', 0)}/{replicas.get('desired', 1)} ready)"
                if rollout == "superseded":
                    after, expect_new = rid, True
            if note != last:
                progress(note, release=release.get("number"), rollout=release.get("rollout_status"),
                         app_status=app.get("status"), ready=replicas.get("available"),
                         desired=replicas.get("desired"))
                last = note
            if time.monotonic() >= deadline:
                raise Stop("release_timeout")
            time.sleep(poll_seconds("release_poll_seconds"))


# ── deploy ───────────────────────────────────────────────────────────────────

class Deploy:
    def __init__(self, args: argparse.Namespace, secrets: dict[str, str]) -> None:
        self.args = args
        self.project = Path(args.project).resolve()
        self.secrets = secrets
        self.sensitive = set(args.sensitive or [])
        self.creating = False
        self.pushed = False
        self.build_sha: str | None = None
        self.before_release: str | None = None
        #: This deploy changed the app's port, health check or env, which
        #: Launchpad applies by rolling the current image out again.
        self.reapplied = False
        self.app: dict[str, Any] = {}
        self.workflow: str | None = None

    # check ──────────────────────────────────────────────────────────────
    def step_check(self) -> None:
        with Step("check") as step:
            checked = check(self.project)
            self.checked = checked
            state = checked["state"]
            if checked["config_error"]:
                raise Stop("config_invalid", f"launchpad.json is not valid: {checked['config_error']}")
            if state == "not-deployable":
                raise Stop("not_deployable", data={"reasons": checked["reasons"]})
            if state == "needs-agent":
                raise Stop("needs_agent", "This project needs preparing before it can deploy: "
                                          + "; ".join(checked["reasons"]) + ".", data={"reasons": checked["reasons"]})
            git = checked["git"]
            if not git.get("repo"):
                raise Stop("needs_agent", "The project is not a git repository.")
            if git.get("detached"):
                raise Stop("detached_head")
            if git.get("no_commits"):
                raise Stop("no_commits")
            self.config = dict(checked["config"] or {})
            fields = checked["fields"]
            name = (self.args.name or self.config.get("name") or fields["name"]["value"]).strip().lower()
            if not NAME_RE.fullmatch(name):
                raise Stop("name_invalid", data={"name": name})
            port = self.args.port or self.config.get("port") or fields["port"]["value"]
            if not port:
                raise Stop("port_missing", kind="needs", options=[{"id": "port", "label": "Enter the port"}])
            if self.args.health_path is not None:
                health = self.args.health_path or None
                if health is not None and not HEALTH_RE.fullmatch(health):
                    raise Stop("config_invalid", "The health path must start with / and have no spaces.")
            else:
                health = fields["health_path"]["value"]
            branch = git["branch"]
            wanted = (self.config.get("repo") or {}).get("branch") or branch
            if branch != wanted:
                raise Stop("wrong_branch", f"This app deploys {wanted}, but the project folder is on {branch}.",
                           fix=f"Check out {wanted}, or merge your work into it, then deploy again.")
            self.name, self.port, self.health, self.branch = name, int(port), health, branch
            # Names only: declared, detected, and the ones the owner gave values for now.
            env: dict[str, bool] = {}
            for item in fields["env"]:
                env[item["name"]] = bool(item.get("sensitive"))
            for key in self.secrets:
                env[key] = env.get(key, False) or key in self.sensitive or is_sensitive(key)
            for key in self.sensitive:
                if key in env:
                    env[key] = True
            self.env = [{"name": k, "sensitive": v} for k, v in env.items()]
            self.build = detect_build(self.project)
            self.template = self.build.get("template") if not self.build.get("dockerfile") else None
            if self.template in ("static", "node-static") and self.port != 80:
                warn(f"The generated {self.template} Dockerfile serves on port 80, so the app uses port 80, "
                     f"not {self.port}.")
                self.port = 80
            self.git = Git(self.project)
            dirty = self.git.dirty_count()
            if dirty:
                warn(f"{dirty} uncommitted change{'s' if dirty != 1 else ''} in the project folder "
                     f"{'are' if dirty != 1 else 'is'} not part of this deploy: only commits deploy.")
            step.done(f"{state}: {name} on port {self.port}, from {branch}")

    # credentials ────────────────────────────────────────────────────────
    def step_credentials(self) -> None:
        with Step("credentials") as step:
            key = os.environ.get("XO_API_KEY", "").strip()
            if not key:
                raise Stop("launchpad_key_missing")
            self.lp = Launchpad(key)
            if not self.lp.available():
                raise Stop("launchpad_cli_missing")
            token = os.environ.get("GITHUB_TOKEN", "").strip()
            if not token:
                raise Stop("github_not_connected")
            self.gh = GitHub(token)
            status, user, headers = self.gh.call("GET", "/user")
            if status in (401, 403):
                raise Stop("github_token_invalid", fix=github_fix("github_token_invalid"))
            if status != 200 or not isinstance(user, dict) or not user.get("login"):
                raise Stop("github_unreachable", f"GitHub answered {status} for the signed-in user.")
            self.login = str(user["login"])
            scopes = headers.get("x-oauth-scopes")
            if scopes is not None:
                have = {s.strip() for s in scopes.split(",") if s.strip()}
                if "workflow" not in have or not ({"repo", "public_repo"} & have):
                    raise Stop("github_workflow_scope", fix=github_fix("github_workflow_scope"),
                               data={"scopes": sorted(have), "source": github_source()})
            else:
                warn("GitHub does not list this token's scopes (a fine-grained token?). It needs read and write "
                     "access to Contents and Workflows, and Administration to create a repo.")
            self.git.token = token
            ensure_identity(self.git, self.gh, user)
            step.done(f"Launchpad key found; GitHub connected as {self.login} "
                      f"(from {SOURCE_WORDS.get(github_source(), github_source())})")

    # repo ───────────────────────────────────────────────────────────────
    def step_repo(self) -> None:
        with Step("repo") as step:
            recorded = self.config.get("repo")
            if recorded:
                full, remote = recorded["full_name"], recorded.get("remote") or DEFAULT_REMOTE
                status, data = self.gh.repo(full)
                if status != 200:
                    raise Stop("repo_unreachable", data={"repo": full})
                if not (data.get("permissions") or {}).get("push"):
                    raise Stop("repo_no_push", data={"repo": full})
                where = self.git.remote_set(remote, full)
                if where == "added":
                    progress(f"Added the git remote {remote} for {full}")
                elif where == "elsewhere":
                    warn(f"The git remote {remote} points somewhere other than {full}; deploys push to {full}.")
                self.full, self.remote = full, remote
                step.done(f"{full} ({remote}), branch {self.branch}")
                return
            # A 'launchpad' remote is the deploy repo made for this project earlier
            # (its launchpad.json may have been deleted since): use it again.
            earlier = github_full((read_git(self.project).get("remotes") or {}).get(DEFAULT_REMOTE))
            if earlier:
                status, data = self.gh.repo(earlier)
                if status == 200 and (data.get("permissions") or {}).get("admin"):
                    self.full, self.remote = earlier, DEFAULT_REMOTE
                    step.done(f"{earlier} ({DEFAULT_REMOTE}), branch {self.branch}")
                    return
            origin = self.checked["git"].get("origin_github")
            if origin:
                status, data = self.gh.repo(origin)
                if status == 200 and (data.get("permissions") or {}).get("admin"):
                    self.full, self.remote = origin, "origin"
                    step.done(f"{origin} (origin, you administer it), branch {self.branch}")
                    return
                reason = (f"You don't administer {origin}, the project's origin" if status == 200
                          else f"{origin}, the project's origin, can't be reached with {self.login}'s GitHub account")
            elif self.checked["git"].get("origin"):
                reason = "The project's origin is not on GitHub"
            else:
                reason = "The project has no GitHub remote"
            name = self.args.repo_name or repo_name_for(self.project)
            if not REPO_NAME_RE.fullmatch(name) or name in (".", ".."):
                raise Stop("repo_name_taken", "That repo name is not valid: use letters, digits, '.', '-' and '_'.",
                           kind="needs", data={"repo_name": name})
            full = f"{self.login}/{name}"
            if not self.args.create_repo:
                raise Stop("create_repo", f"{reason}, so this project deploys from a private repo of its own: "
                                          f"create {full}?", kind="needs",
                           data={"repo": full, "reason": reason},
                           options=[{"id": "create_repo", "label": f"Create private repo {full}"},
                                    {"id": "repo_name", "label": "Use another name"}])
            status, data = self.gh.repo(full)
            if status == 200:
                if not self._reusable(full, data):
                    raise Stop("repo_name_taken", f"{full} already exists and holds other code.", kind="needs",
                               data={"repo": full, "suggestion": f"{name}-app"[:100]},
                               options=[{"id": "repo_name", "label": "Use another name"}])
                progress(f"Reusing {full}: it is empty or already holds this project's history")
            elif status == 404:
                status, made, _ = self.gh.call("POST", "/user/repos", body={
                    "name": name, "private": True, "auto_init": False, "has_wiki": False,
                    "description": f"{self.project.name}, deployed with XO Launchpad"})
                if status == 422:
                    raise Stop("repo_name_taken", f"GitHub refused the name {full}.", kind="needs",
                               data={"repo": full, "suggestion": f"{name}-app"[:100]})
                if status not in (200, 201) or not isinstance(made, dict):
                    detail = (made or {}).get("message") if isinstance(made, dict) else None
                    raise Stop("github_token_invalid" if status in (401, 403) else "github_unreachable",
                               f"GitHub could not create {full}" + (f": {detail}" if detail else f" (HTTP {status})."))
                full = made.get("full_name") or full
                progress(f"Created the private repo {full}")
            else:
                raise Stop("github_unreachable", f"GitHub answered {status} for {full}.")
            where = self.git.remote_set(DEFAULT_REMOTE, full)
            if where == "elsewhere":
                raise Stop("remote_conflict", f"This project's git remote '{DEFAULT_REMOTE}' points somewhere else.",
                           fix=f"Rename or remove the '{DEFAULT_REMOTE}' remote, then deploy again.")
            self.full, self.remote = full, DEFAULT_REMOTE
            step.done(f"{full} ({DEFAULT_REMOTE}), branch {self.branch}; origin is untouched")

    def _reusable(self, full: str, data: dict[str, Any]) -> bool:
        if not (data.get("permissions") or {}).get("admin"):
            return False
        heads = self.git.remote_heads(full)
        if not heads:
            return True
        tip = self.git.remote_branch(full, self.branch)
        return bool(tip) and self.git.has_commit(tip) and self.git.is_ancestor(tip, "HEAD")

    # app ────────────────────────────────────────────────────────────────
    def step_app(self) -> None:
        with Step("app") as step:
            app_id = self.config.get("app_id")
            if app_id:
                try:
                    self.app = (self.lp.call("app", "get", app_id) or {}).get("app") or {}
                except LaunchpadFailed as exc:
                    if not exc.not_found:
                        raise exc.stop("launchpad_unreachable") from None
                    if not self.args.recreate:
                        raise Stop("app_missing", f"The app {self.config.get('name') or app_id} in launchpad.json no "
                                                  "longer exists on Launchpad.", kind="needs",
                                   data={"app_id": app_id, "name": self.config.get("name")},
                                   options=[{"id": "recreate", "label": "Recreate it"},
                                            {"id": "unlink", "label": "Unlink"}]) from None
                    self.creating = True
                    progress("The app no longer exists; it will be created again")
                if self.app:
                    if app_repo(self.app) and app_repo(self.app) != self.full:
                        warn(f"The app builds {app_repo(self.app)}, but launchpad.json names {self.full}.")
                    self.before_release = self.app.get("current_release_id")
                    self.name = self.app.get("name") or self.name
                    self.workflow = self.config.get("workflow") or workflow_name(self.app.get("slug"))
                    step.done(f"{self.name}: {self.app.get('status')}, {self.app.get('public_url') or ''}".strip(", "))
                    return
            else:
                self.creating = True
            try:
                apps = (self.lp.call("app", "list") or {}).get("apps") or []
            except LaunchpadFailed as exc:
                raise exc.stop("launchpad_unreachable") from None
            same = [a for a in apps if a.get("name") == self.name]
            if same:
                other = same[0]
                if other.get("source") == "github" and app_repo(other) == self.full:
                    raise Stop("link_existing", f"The app {self.name} already deploys {self.full}: link it?",
                               kind="needs", data={"app": app_summary(other)},
                               options=[{"id": "link", "label": f"Link {self.name}"}])
                raise Stop("name_taken", f"Another Launchpad app is already called {self.name}.", kind="needs",
                           data={"app": app_summary(other), "suggestion": f"{self.name[:21]}-app"},
                           options=[{"id": "name", "label": "Use another name"}])
            step.done(f"A new app {self.name} will be created")

    # prepare ────────────────────────────────────────────────────────────
    def step_prepare(self) -> None:
        with Step("prepare") as step:
            paths: list[str] = []
            notes: list[str] = []
            if self.template:
                values = dict(self.build.get("_values") or {})
                text = render_template(self.template, {**values, "port": str(self.port)})
                _write_new(self.project / "Dockerfile", text)
                paths.append("Dockerfile")
                notes.append(f"added a {self.template} Dockerfile")
            if not self.creating:
                if self.app.get("port") != self.port:
                    self._lp("app", "port", "set", self.app["id"], str(self.port))
                    notes.append(f"port set to {self.port}")
                    self.reapplied = True
                if (self.app.get("health_path") or None) != self.health:
                    self._lp("app", "health", "set", self.app["id"],
                             *(["--path", self.health] if self.health else ["--tcp"]))
                    notes.append(f"health check set to {self.health or 'the port'}")
                    self.reapplied = True
                if write_config(self.project, self._config()):
                    paths.append(CONFIG_NAME)
                    notes.append("updated launchpad.json")
            if paths:
                try:
                    self.git.commit(paths, "Prepare the Launchpad deploy")
                except GitFailed as exc:
                    raise Stop("commit_failed", f"Committing {', '.join(paths)} failed: {_first_line(exc.output)}") from None
                step.done("; ".join(notes).capitalize() + ", and committed it.")
            else:
                step.skip("Nothing to change.")

    def _lp(self, *args: str, extra_env: dict[str, str] | None = None) -> Any:
        try:
            return self.lp.call(*args, extra_env=extra_env)
        except LaunchpadFailed as exc:
            raise exc.stop("launchpad_unreachable") from None

    def _config(self) -> dict[str, Any]:
        config = dict(self.config)
        config.update({"schema": 1, "name": self.name, "port": self.port, "health_path": self.health,
                       "repo": {"full_name": self.full, "remote": self.remote, "branch": self.branch}})
        if self.app:
            config.update({"app_id": self.app.get("id"), "slug": self.app.get("slug"),
                           "url": self.app.get("public_url") or self.app.get("url"),
                           "workflow": self.workflow or config.get("workflow")})
        if self.env:
            config["env"] = self.env
        return {k: v for k, v in config.items() if v is not None or k == "health_path"}

    # push ───────────────────────────────────────────────────────────────
    def step_push(self) -> None:
        with Step("push") as step:
            try:
                self._push(step)
            except GitFailed as exc:
                text = exc.output
                if "rejected" in text or "non-fast-forward" in text or "fetch first" in text:
                    raise Stop("push_rejected", data={"git": _first_line(text)}) from None
                raise Stop("push_failed", f"Git failed: {_first_line(text)}") from None

    def _push(self, step: Step) -> None:
        git, full, branch = self.git, self.full, self.branch
        head = git.head()
        remote_sha = git.remote_branch(full, branch)
        if remote_sha and remote_sha != head:
            if not git.has_commit(remote_sha):
                git.fetch(full, branch, self.remote)
            if git.is_ancestor(remote_sha, head):
                pass                                    # ours is ahead: a fast-forward push
            elif git.is_ancestor(head, remote_sha):     # GitHub is ahead
                if not git.only_launchpad_files(head, remote_sha):
                    raise Stop("remote_ahead", f"{full} has commits on {branch} that this project folder lacks.")
                git.run("merge", "--ff-only", remote_sha)
                progress("Brought in Launchpad's workflow commit from GitHub")
                head = remote_sha
            else:
                base = git.out("merge-base", head, remote_sha)
                if not git.only_launchpad_files(base, remote_sha):
                    raise Stop("push_rejected", f"{full} and this project folder have both moved on {branch}.")
                git.run("merge", "--no-edit", "-m", f"Merge Launchpad's workflow from {self.remote}/{branch}",
                        remote_sha)
                progress("Merged Launchpad's workflow commit from GitHub")
                head = git.head()
        if remote_sha == head and self.args.rebuild and not self.creating:
            head = git.commit_empty("Rebuild on Launchpad")
            progress("Made an empty commit to build again")
        if remote_sha == head:
            self.build_sha = head
            git.run("update-ref", f"refs/remotes/{self.remote}/{branch}", head, check=False)
            step.skip(f"{head[:7]} is already on GitHub: nothing new to push.")
            return
        git.push(full, branch, self.remote)
        self.pushed, self.build_sha = True, head
        step.done(f"Pushed {head[:7]} to {full} {branch}")

    # create ─────────────────────────────────────────────────────────────
    def step_create(self) -> None:
        with Step("create") as step:
            argv = ["app", "create", self.name, "--template", "github", "--port", str(self.port),
                    *(["--health-path", self.health] if self.health else []),
                    # As JSON: the CLI reads a --param value as JSON when it parses,
                    # so a branch named 2024 or true would arrive as a number or a bool.
                    "--params", json.dumps({"repoUrl": repo_web_url(self.full), "branch": self.branch}),
                    "--param-env", "githubToken=GITHUB_TOKEN"]
            try:
                created = self.lp.call(*argv, extra_env={"GITHUB_TOKEN": self.gh.token}, seconds=180) or {}
            except LaunchpadFailed as exc:
                raise exc.stop("create_failed") from None
            self.app = created.get("app") or {}
            if not self.app.get("id"):
                raise Stop("create_failed", "Launchpad answered without an app.")
            url = self.app.get("public_url") or self.app.get("url")
            progress(f"Created the app {self.app.get('name')}", app_id=self.app["id"], url=url)
            self.before_release = None
            # Launchpad's commit (the workflow) starts the first build; bring it into the branch.
            head = self.git.head()
            deadline = time.monotonic() + timeout("workflow_commit_seconds")
            remote_sha = self.git.remote_branch(self.full, self.branch)
            while remote_sha == head and time.monotonic() < deadline:
                time.sleep(min(2.0, poll_seconds()))
                remote_sha = self.git.remote_branch(self.full, self.branch)
            self.build_sha = None
            before = [p for p in self.git.out("ls-files", "--", ".github/workflows").splitlines()
                      if WORKFLOW_RE.fullmatch(p)]
            added: list[str] = []
            if remote_sha and remote_sha != head:
                try:
                    self.git.fetch(self.full, self.branch, self.remote)
                    if self.git.is_ancestor(head, remote_sha) and self.git.only_launchpad_files(head, remote_sha):
                        added = self.git.changed(head, remote_sha)
                        self.git.run("merge", "--ff-only", remote_sha)
                        self.build_sha = remote_sha
                    else:
                        warn(f"{self.full} has other new commits on {self.branch}; pull them before the next deploy.")
                except GitFailed as exc:
                    warn(f"Could not bring in Launchpad's workflow commit: {_first_line(exc.output)}")
            else:
                warn("Launchpad's workflow commit has not reached GitHub yet; following the branch instead.")
            # The pointer, committed with [skip ci] so it does not build a second time. A
            # recreated app's old workflow would build for an app that is gone: it goes.
            # Any other launchpad-*.yml is another app's (one unlinked with forget
            # keeps running) and stays.
            if added:
                self.workflow = os.path.basename(added[0])
            else:
                self.workflow = workflow_name(self.app.get("slug"))
            gone = ((self.config.get("workflow") or workflow_name(self.config.get("slug")))
                    if self.args.recreate else None)
            stale = [p for p in before if gone and os.path.basename(p) == gone
                     and p not in added and os.path.basename(p) != self.workflow]
            for path in stale:
                self.git.run("rm", "-q", "--", path)
            write_config(self.project, self._config())
            try:
                committed = self.git.commit([CONFIG_NAME, *stale], f"Link Launchpad app {self.name} [skip ci]")
                if committed and self.build_sha:
                    self.git.push(self.full, self.branch, self.remote)
            except GitFailed as exc:
                warn(f"launchpad.json is written but could not be committed and pushed: {_first_line(exc.output)}")
            step.done(f"{self.app.get('name')} is at {url}")

    # env ────────────────────────────────────────────────────────────────
    def step_env(self) -> None:
        with Step("env") as step:
            app_id = self.app.get("id")
            done: list[str] = []
            if self.secrets:
                argv: list[str] = ["app", "env", "set", app_id]
                extra: dict[str, str] = {}
                sensitive = {e["name"] for e in self.env if e["sensitive"]}
                for i, (key, value) in enumerate(sorted(self.secrets.items())):
                    extra[f"LAUNCHPAD_VALUE_{i}"] = value
                    argv += ["--from-env", f"{key}=LAUNCHPAD_VALUE_{i}"]
                    if key in sensitive:
                        argv += ["--sensitive", key]
                try:
                    self.lp.call(*argv, extra_env=extra)
                except LaunchpadFailed as exc:
                    raise exc.stop("env_failed") from None
                done = sorted(self.secrets)
                self.reapplied = True
            declared = {e["name"] for e in self.env}
            if declared:
                try:
                    present = {v.get("key") for v in (self.lp.call("app", "env", "list", app_id) or {}).get("vars") or []}
                except LaunchpadFailed:
                    present = None
                missing = sorted(declared - (present or set()) - set(done)) if present is not None else []
                if missing:
                    warn("No value on Launchpad yet for " + ", ".join(missing) + ": add them in the deploy form "
                         "if the app needs them.")
            if done:
                step.done("Set " + ", ".join(done) + " on the app")
            else:
                step.skip("No values to set.")

    # build, release, url ────────────────────────────────────────────────
    def step_build(self) -> None:
        with Step("build") as step:
            follow = Follow(self.lp, self.gh, self.full, self.branch)
            self.follow = follow
            workflow = self.workflow or workflow_name(self.app.get("slug"))
            if self.pushed or self.creating:
                if self.reapplied and not self.creating:
                    self._release_now()
                found = follow.wait_for_run(workflow, self.build_sha)
                run_ = follow.build(found)
                self.new_build = True
                step.done(f"Built in {self._duration(run_)}" if self._duration(run_) else "Built")
                return
            self.new_build = False
            found = follow.find_run(workflow, self.build_sha)
            if found is None:
                step.skip("No build for this commit on GitHub; showing the current release.")
                return
            if found.get("status") != "completed":
                follow.build(found)
                self.new_build = True
                step.done("Built")
                return
            if found.get("conclusion") != "success":
                raise Stop("build_failed", f"The last build of {(self.build_sha or '')[:7]} {found.get('conclusion')}"
                                           ", and nothing new was pushed.",
                           fix="Commit a fix and deploy again, or deploy with rebuild to try the same commit again.",
                           data={"run_url": found.get("html_url")})
            step.skip(f"{(self.build_sha or '')[:7]} was already built.")

    def _release_now(self) -> None:
        """Take the app's release as it is now as the one to move past: the
        port, health or env change above rolled the old image out again, and
        that release must not pass for the new build's."""
        try:
            app = (self.lp.call("app", "get", self.app["id"]) or {}).get("app") or {}
        except LaunchpadFailed:
            return
        self.before_release = app.get("current_release_id") or self.before_release

    @staticmethod
    def _duration(run_: dict[str, Any]) -> str | None:
        try:
            start = datetime.fromisoformat(str(run_["run_started_at"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(run_["updated_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return None
        seconds = max(0, int((end - start).total_seconds()))
        return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"

    def step_release(self) -> None:
        with Step("release") as step:
            status = self.follow.release(self.app["id"], after=self.before_release, expect_new=self.new_build)
            if status is None:
                step.skip("The app has no release yet.")
                self.release = None
                return
            self.release = status.get("release") or {}
            self.app = status.get("app") or self.app
            step.done(f"Release #{self.release.get('number')} is live")

    def step_url(self) -> None:
        with Step("url") as step:
            url = self.app.get("public_url") or self.app.get("url")
            self.http_status = probe_url(url, timeout("url_seconds")) if url else None
            if self.http_status is None:
                warn(f"{url} did not answer yet.")
            elif self.http_status >= 400:
                warn(f"{url} answered HTTP {self.http_status}.")
            step.done(f"{url} answered HTTP {self.http_status}" if self.http_status else f"{url}")

    def run(self) -> None:
        self.step_check()
        self.step_credentials()
        self.step_repo()
        self.step_app()
        self.step_prepare()
        self.step_push()
        if self.creating:
            self.step_create()
        self.step_env()
        self.step_build()
        self.step_release()
        self.step_url()
        emit("done", url=self.app.get("public_url") or self.app.get("url"), http_status=self.http_status,
             app={"id": self.app.get("id"), "name": self.app.get("name"), "slug": self.app.get("slug")},
             repo=self.full, branch=self.branch, commit=(self.build_sha or "")[:12] or None,
             release=(self.release or {}).get("number"), created=self.creating)


# ── watch, apps, link, unlink ────────────────────────────────────────────────

def _key() -> str:
    key = os.environ.get("XO_API_KEY", "").strip()
    if not key:
        raise Stop("launchpad_key_missing")
    return key


def _launchpad() -> Launchpad:
    lp = Launchpad(_key())
    if not lp.available():
        raise Stop("launchpad_cli_missing")
    return lp


def _linked(project: Path) -> dict[str, Any]:
    config, error = read_config(project)
    if error:
        raise Stop("config_invalid", f"launchpad.json is not valid: {error}")
    if not config or not config.get("app_id"):
        raise Stop("not_linked")
    return config


def watch(args: argparse.Namespace) -> None:
    project = Path(args.project).resolve()
    config = _linked(project)
    lp = _launchpad()
    try:
        status = lp.call("app", "status", config["app_id"]) or {}
    except LaunchpadFailed as exc:
        if exc.not_found:
            raise Stop("app_missing", kind="needs", data={"app_id": config["app_id"], "name": config.get("name")},
                       options=[{"id": "recreate", "label": "Recreate it"}, {"id": "unlink", "label": "Unlink"}]) from None
        raise exc.stop("launchpad_unreachable") from None
    app = status.get("app") or {}
    release = status.get("release") or {}
    emit("status", app=app_summary(app), release={k: release.get(k) for k in (
        "id", "number", "rollout_status", "failure", "created_at", "finished_at")} if release else None,
         live={k: (status.get("status") or {}).get(k) for k in ("state", "reason", "message", "replicas")})
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = config.get("repo") or {}
    follow = Follow(lp, GitHub(token) if token else None, repo.get("full_name"), repo.get("branch"))
    expect_new, before = False, app.get("current_release_id")
    with Step("build") as step:
        if follow.gh is None:
            step.skip("GitHub is not connected, so the build cannot be followed.")
        else:
            found = follow.find_run(config.get("workflow") or workflow_name(app.get("slug") or config.get("slug")),
                                    None)
            if found is None:
                step.skip("No build found on GitHub.")
            elif found.get("status") != "completed":
                follow.build(found)
                expect_new = True
                step.done("Built")
            else:
                if found.get("conclusion") != "success":
                    warn(f"The last build {found.get('conclusion')}: {found.get('html_url')}")
                step.skip(f"The last build {found.get('conclusion')}.")
    with Step("release") as step:
        status = follow.release(config["app_id"], after=before, expect_new=expect_new)
        if status is None:
            step.skip("The app has no release yet.")
        else:
            app = status.get("app") or app
            release = status.get("release") or {}
            step.done(f"Release #{release.get('number')} is live")
    with Step("url") as step:
        url = app.get("public_url") or app.get("url")
        code = probe_url(url, timeout("url_seconds")) if url and status is not None else None
        step.done(f"{url} answered HTTP {code}" if code else f"{url}")
    emit("done", url=app.get("public_url") or app.get("url"), http_status=code,
         app={"id": app.get("id"), "name": app.get("name"), "slug": app.get("slug")},
         release=(release or {}).get("number"))


def _preflight_github(project: Path, checked: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The connected GitHub account, and the repo a deploy would use."""
    config, fields = checked["config"] or {}, checked["fields"]
    github: dict[str, Any] = {"connected": False, "source": None}
    login = None
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    gh = GitHub(token) if token else None
    source = github_source() if token else None
    if gh is not None:
        try:
            status, user, headers = gh.call("GET", "/user")
            if status == 200 and isinstance(user, dict) and user.get("login"):
                login = str(user["login"])
                scopes = headers.get("x-oauth-scopes")
                have = sorted({s.strip() for s in scopes.split(",") if s.strip()}) if scopes is not None else None
                github = {"connected": True, "source": source, "login": login, "scopes": have,
                          "scopes_known": have is not None, "workflow_scope": None if have is None else "workflow" in have}
                if have is not None and "workflow" not in have:
                    github["fix"] = github_fix("github_workflow_scope") or flow()["failures"]["github_workflow_scope"]["fix"]
            else:
                code = "github_token_invalid" if status in (401, 403) else "github_unreachable"
                github = {"connected": False, "source": source, "error": code,
                          "fix": github_fix(code) or flow()["failures"][code]["fix"]}
        except Stop as stop:
            github = {"connected": False, "source": source, "error": stop.code}

    repo: dict[str, Any] = {"branch": fields["repo"]["branch"]}
    recorded = config.get("repo")
    origin = checked["git"].get("origin_github")
    try:
        if recorded:
            repo.update(mode="recorded", full_name=recorded["full_name"],
                        remote=recorded.get("remote") or DEFAULT_REMOTE)
            if gh is not None and login:
                status, data = gh.repo(recorded["full_name"])
                repo.update(reachable=status == 200, can_push=bool((data.get("permissions") or {}).get("push")),
                            private=data.get("private"), default_branch=data.get("default_branch"))
            return github, repo

        def administered(full: str | None) -> tuple[bool, int | None, dict[str, Any]]:
            if not full or gh is None or not login:
                return False, None, {}
            status, data = gh.repo(full)
            return status == 200 and bool((data.get("permissions") or {}).get("admin")), status, data

        # The deploy's order: an earlier deploy repo, an administered origin, else a new one.
        earlier = github_full((read_git(project).get("remotes") or {}).get(DEFAULT_REMOTE))
        admin, _status, data = administered(earlier)
        if admin:
            repo.update(mode="remote", full_name=earlier, remote=DEFAULT_REMOTE, is_admin=True,
                        default_branch=data.get("default_branch"))
            return github, repo
        if origin:
            admin, status, data = administered(origin)
            repo.update(origin=origin, origin_reachable=status == 200 if status else None, is_admin=admin,
                        default_branch=data.get("default_branch") if status == 200 else None)
            if admin:
                repo.update(mode="origin", full_name=origin, remote="origin")
                return github, repo
        name = fields["repo"]["new_repo_name"]
        repo.update(mode="new", remote=DEFAULT_REMOTE, new_repo_name=name,
                    reason=("You don't administer the project's origin" if origin and repo.get("origin_reachable")
                            else "The project's origin can't be reached with the connected account" if origin
                            else "The project has no GitHub remote"))
        if login:
            full = f"{login}/{name}"
            status, _data = gh.repo(full)
            repo.update(full_name=full, exists=status == 200)
    except Stop as stop:
        repo["error"] = stop.code
    return github, repo


def _preflight_launchpad(checked: dict[str, Any], name: str | None) -> dict[str, Any]:
    """The linked app's live state, or whether the app name is free."""
    config, fields = checked["config"] or {}, checked["fields"]
    launchpad: dict[str, Any] = {"key": bool(os.environ.get("XO_API_KEY", "").strip())}
    try:
        lp = _launchpad()
        launchpad["cli"] = True
        if config.get("app_id"):
            try:
                status = lp.call("app", "status", config["app_id"]) or {}
                app, release = status.get("app") or {}, status.get("release") or {}
                live = status.get("status") or {}
                launchpad["app"] = {**app_summary(app), "exists": True,
                                    "release": release.get("number"), "rollout": release.get("rollout_status"),
                                    "last_deploy_at": release.get("finished_at") or release.get("created_at"),
                                    "ready": (live.get("replicas") or {}).get("available"),
                                    "desired": (live.get("replicas") or {}).get("desired")}
                declared = [e["name"] for e in fields["env"]]
                if declared:
                    present = {v.get("key") for v in (lp.call("app", "env", "list", config["app_id"]) or {})
                               .get("vars") or []}
                    launchpad["env_set"] = {name: name in present for name in declared}
            except LaunchpadFailed as exc:
                if not exc.not_found:
                    raise exc.stop("launchpad_unreachable") from None
                launchpad["app"] = {"id": config["app_id"], "name": config.get("name"), "exists": False}
        else:
            wanted = name or fields["name"]["value"]
            listed = (lp.call("app", "list") or {}).get("apps") or []
            same = next((a for a in listed if a.get("name") == wanted), None)
            if same:
                launchpad["same_name"] = app_summary(same)
            launchpad["name_free"] = same is None
    except Stop as stop:
        launchpad["error"] = stop.code
        launchpad.setdefault("cli", stop.code != "launchpad_cli_missing")
    except LaunchpadFailed as exc:
        launchpad["error"] = exc.stop("launchpad_unreachable").code
    return launchpad


def preflight(args: argparse.Namespace) -> None:
    """What a deploy would meet, asked of GitHub and Launchpad without
    changing anything: the connected account and its scopes, the repo the
    deploy would use (or create), and the linked app's live state. The two
    services are asked at the same time, and each part reports its own
    error, so one service being down still shows the other."""
    project = Path(args.project).resolve()
    checked = check(project)
    found: dict[str, Any] = {}

    def ask_launchpad() -> None:
        try:
            found["launchpad"] = _preflight_launchpad(checked, args.name)
        except Exception as exc:  # noqa: BLE001 - reported as this part's error
            found["launchpad"] = {"error": "internal", "detail": exc.__class__.__name__}

    worker = threading.Thread(target=ask_launchpad, name="preflight-launchpad", daemon=True)
    worker.start()
    github, repo = _preflight_github(project, checked)
    worker.join()
    launchpad = found["launchpad"]
    same = launchpad.get("same_name")
    if same is not None:
        same["linkable"] = same.get("source") == "github" and same.get("repo") == repo.get("full_name")
    emit("result", preflight={"state": checked["state"], "github": github, "repo": repo, "launchpad": launchpad})


def apps(args: argparse.Namespace) -> None:
    project = Path(args.project).resolve()
    config, _error = read_config(project)
    git = read_git(project)
    name = (config or {}).get("name") or slug_name(project.name)
    repos = {r for r in (((config or {}).get("repo") or {}).get("full_name"), git.get("origin_github")) if r}
    try:
        listed = (_launchpad().call("app", "list") or {}).get("apps") or []
    except LaunchpadFailed as exc:
        raise exc.stop("launchpad_unreachable") from None
    out = []
    for app in listed:
        summary = app_summary(app)
        match = [m for m, hit in (("name", app.get("name") == name), ("repo", summary["repo"] in repos)) if hit]
        out.append({**summary, "match": match, "linkable": app.get("source") == "github"})
    out.sort(key=lambda a: (-len(a["match"]), not a["linkable"], a.get("name") or ""))
    emit("result", apps=out, linked=(config or {}).get("app_id"), name=name)


def link(args: argparse.Namespace) -> None:
    project = Path(args.project).resolve()
    config, error = read_config(project)
    if error:
        raise Stop("config_invalid", f"launchpad.json is not valid: {error}")
    git = read_git(project)
    if not git.get("repo"):
        raise Stop("needs_agent", "The project is not a git repository yet.")
    lp = _launchpad()
    try:
        app = (lp.call("app", "get", args.app) or {}).get("app") or {}
    except LaunchpadFailed as exc:
        if exc.not_found:
            raise Stop("app_not_found") from None
        raise exc.stop("launchpad_unreachable") from None
    if config and config.get("app_id") and config["app_id"] != app.get("id"):
        raise Stop("already_linked", data={"app_id": config["app_id"], "name": config.get("name")})
    full = app_repo(app)
    if app.get("source") != "github" or not full:
        raise Stop("not_a_github_app", data={"app": app_summary(app)})
    params = app.get("params") if isinstance(app.get("params"), dict) else {}
    branch = params.get("branch") if isinstance(params.get("branch"), str) else git.get("branch")
    remotes = git.get("remotes") or {}
    remote = next((n for n, url in sorted(remotes.items(), key=lambda kv: kv[0] != "origin")
                   if github_full(url) == full), None)
    tool = Git(project)
    if remote is None:
        remote = DEFAULT_REMOTE if DEFAULT_REMOTE not in remotes else f"{DEFAULT_REMOTE}-{app.get('slug', 'app')}"[:64]
        tool.remote_set(remote, full)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    gh = GitHub(token) if token else None
    if gh is not None:
        status, data = gh.repo(full)
        if status != 200 or not (data.get("permissions") or {}).get("push"):
            warn(f"The connected GitHub account cannot push to {full}; deploys need an account that can.")
    ensure_identity(tool, gh)
    previous = config or {}
    declared = previous.get("env") or [{"name": e["name"], "sensitive": e["sensitive"]} for e in env_names(project)]
    linked = {**previous, "schema": 1, "name": app.get("name"), "app_id": app.get("id"), "slug": app.get("slug"),
              "url": app.get("public_url") or app.get("url"),
              "repo": {"full_name": full, "remote": remote, "branch": branch},
              "port": app.get("port"), "health_path": app.get("health_path")}
    if declared:
        linked["env"] = declared
    write_config(project, linked)
    try:
        commit = tool.commit([CONFIG_NAME], f"Link Launchpad app {app.get('name')}")
    except GitFailed as exc:
        commit = None
        warn(f"launchpad.json is written but not committed: {_first_line(exc.output)}")
    emit("result", config=ordered_config(linked), commit=commit, app=app_summary(app))


def unlink(args: argparse.Namespace) -> None:
    project = Path(args.project).resolve()
    path = project / CONFIG_NAME
    if not path.exists():
        raise Stop("not_linked")
    config, _error = read_config(project)
    was = {"app_id": (config or {}).get("app_id"), "name": (config or {}).get("name")}
    tool = Git(project)
    if args.forget or config is None:
        path.unlink()
        message = "Remove launchpad.json"
    else:
        kept = {k: v for k, v in config.items() if k not in POINTER_KEYS}
        write_config(project, kept)
        message = f"Unlink Launchpad app {was['name'] or was['app_id']}"
    commit = None
    if read_git(project).get("repo") and (tool.tracked(CONFIG_NAME) or path.exists()):
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        ensure_identity(tool, GitHub(token) if token else None)
        try:
            commit = tool.commit([CONFIG_NAME], message)
        except GitFailed as exc:
            warn(f"launchpad.json is changed but not committed: {_first_line(exc.output)}")
    emit("result", unlinked=was, forgot=bool(args.forget or config is None), commit=commit)


# ── the command line ─────────────────────────────────────────────────────────

def _port(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("must be from 1 to 65535")
    return number


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="launchpad.py", description="Deploy an XO project to Launchpad (flow.json).")
    sub = p.add_subparsers(dest="mode", required=True)
    for mode in ("check", "preflight", "deploy", "watch", "apps", "link", "unlink"):
        command = sub.add_parser(mode)
        command.add_argument("--project", required=True, help="The project folder")
        if mode == "preflight":
            command.add_argument("--name", help="The app name the deploy would use")
        if mode == "deploy":
            command.add_argument("--name", help="The Launchpad app name")
            command.add_argument("--port", type=_port)
            command.add_argument("--health-path", help="An HTTP path; empty for a port check")
            command.add_argument("--create-repo", action="store_true", help="Create a private repo when needed")
            command.add_argument("--repo-name", help="The new repo's name (default: the project folder's)")
            command.add_argument("--recreate", action="store_true", help="Recreate an app deleted on Launchpad")
            command.add_argument("--rebuild", action="store_true", help="Build again with nothing new to push")
            command.add_argument("--sensitive", action="append", default=[], metavar="NAME",
                                 help="Mark an environment variable sensitive")
            command.add_argument("--secrets-stdin", action="store_true",
                                 help='Read {"env": {"NAME": "value"}} from stdin')
        if mode == "link":
            command.add_argument("--app", required=True, help="The app's id or exact name")
        if mode == "unlink":
            command.add_argument("--forget", action="store_true", help="Delete launchpad.json entirely")
    return p


def read_secrets() -> dict[str, str]:
    raw = sys.stdin.read(SECRETS_MAX_BYTES + 1)
    if len(raw) > SECRETS_MAX_BYTES:
        raise Stop("env_failed", "The environment values are too large.")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise Stop("env_failed", "The environment values are not valid JSON.") from None
    values = data.get("env", data) if isinstance(data, dict) else None
    if not isinstance(values, dict):
        raise Stop("env_failed", 'Send {"env": {"NAME": "value"}}.')
    out: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not ENV_RE.fullmatch(key):
            raise Stop("env_failed", "Environment variable names are letters, digits and _.")
        if not isinstance(value, str):
            raise Stop("env_failed", f"The value of {key} must be text.")
        out[key] = value
    return out


def _stopped(*_: Any) -> None:
    raise Stopped()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.mode == "check":
        print(json.dumps(check(args.project), indent=2))
        return EXIT_DONE
    signal.signal(signal.SIGTERM, _stopped)
    try:
        if args.mode == "deploy":
            secrets = read_secrets() if args.secrets_stdin else {}
            Deploy(args, secrets).run()
        else:
            {"preflight": preflight, "watch": watch, "apps": apps, "link": link, "unlink": unlink}[args.mode](args)
        return EXIT_DONE
    except Stop as stop:
        emit(stop.kind, step=_current_step, code=stop.code, message=stop.message, fix=stop.fix,
             action=action(stop.code), data=stop.data or None, options=stop.options)
        return EXIT_NEEDS if stop.kind == "needs" else EXIT_ERROR
    except GitFailed as exc:
        emit("error", step=_current_step, code="internal", message=f"Git failed: {_first_line(exc.output)}",
             fix=flow()["failures"]["internal"]["fix"], action=action("internal"))
        return EXIT_ERROR
    except (Stopped, KeyboardInterrupt):
        info = flow()["failures"]["cancelled"]
        emit("error", step=_current_step, code="cancelled", message=info["message"], fix=info["fix"],
             action=action("cancelled"))
        return EXIT_STOPPED
    except Exception as exc:  # noqa: BLE001 - the last line says what failed, never a secret
        emit("error", step=_current_step, code="internal",
             message=f"{flow()['failures']['internal']['message']} ({exc.__class__.__name__}: {_first_line(str(exc), 200)})",
             fix=flow()["failures"]["internal"]["fix"], action=action("internal"))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

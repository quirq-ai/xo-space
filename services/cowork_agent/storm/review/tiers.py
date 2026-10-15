"""The tier of an action, computed and never claimed (design ch 15 §2.2;
ch 16 §7; R-E §3.2).

Each simple command of a compound is judged on its own, and the action's tier
is the highest of its parts. Every part also gets a ``category``: what kind of
thing it is (a read inside the session's folder, read-only git, a local git
write, …). XO's default rules (``rules.py``) allow routine work by category,
so a person can revoke them one by one. The agent's own words (a reason, a
"why") never enter here.

The tables are data, and they name no agent CLI: the CLIs' own folders and
project config come in through ``Places``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from services.cowork_agent.storm.review.paths import Place, Places
from services.cowork_agent.storm.review.shell import Redirect, ShellRefused, SimpleCommand, parse


class Tier(IntEnum):
    ROUTINE = 0
    NOTABLE = 1
    DESTRUCTIVE = 2


# Categories: what default rules can cover. Anything else is "other".
READ_INSIDE = "read_inside"           # reads, searches, listings in the session's folder
GIT_READ = "git_read"                 # read-only git
GIT_LOCAL = "git_local"               # git add, commit, checkout -b in the worktree
EDIT_INSIDE = "edit_inside"           # a file edit, create or delete in the worktree
COMMS = "comms"                       # XO's own comms tools
KNOWN_HOST_GET = "known_host_get"     # a GET to a listed docs or registry host
XO_DOCUMENTED_GET = "xo_documented_get"   # a GET to an XO route the xo-projects skill documents
OTHER = "other"

#: Package registries and documentation hosts a GET may reach under a default rule.
KNOWN_HOSTS = frozenset({
    "registry.npmjs.org", "www.npmjs.com", "pypi.org", "files.pythonhosted.org", "crates.io", "static.crates.io",
    "docs.rs", "pkg.go.dev", "proxy.golang.org", "rubygems.org", "nodejs.org", "docs.python.org",
    "developer.mozilla.org", "docs.github.com",
})
#: XO routes the xo-projects skill documents for agents (ch 16 §7): the inbox and backups.
XO_AGENT_ROUTES = ("/api/inbox", "/api/xo-projects-sync/")
#: Anything else naming these is XO's own machinery (anti-forgery, R-E T2).
XO_MARKERS = ("/mcp/space-comms", "/api/chat/", "XO_COMMS_TOKEN", "XO_SESSION_TOKEN", "XO_API_KEY",
              "XO_AUTH_SESSION_ID", "XO_POLL_TOKEN")
LOOPBACK = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})

#: Read-only commands, each with the flags that would make it not read-only.
READ_ONLY = {
    "ls": (), "cat": (), "head": (), "tail": (), "wc": (), "pwd": (), "file": (), "stat": (), "tree": (),
    "du": (), "basename": (), "dirname": (), "realpath": (), "readlink": (), "which": (), "type": (),
    "echo": (), "printf": (), "true": (), "false": (), "test": (), "[": (), "date": (), "whoami": (),
    "id": (), "uname": (), "diff": (), "cmp": (), "md5sum": (), "sha1sum": (), "sha256sum": (), "nl": (),
    "cut": (), "column": (), "strings": (), "od": (), "hexdump": (), "less": (), "more": (), "jq": (),
    "grep": (), "egrep": (), "fgrep": (), "ag": (),
    "rg": ("--pre", "--pre-glob"),
    "sort": ("-o", "--output"),
    "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"),
    "fd": ("-x", "--exec", "-X", "--exec-batch"),
}
#: Commands whose first word is a pattern, not a path.
PATTERN_FIRST = frozenset({"grep", "egrep", "fgrep", "rg", "ag"})
#: Commands that print the environment (which holds this session's tokens)
#: when given no arguments, or only these.
ENV_DUMPS = {"printenv": None, "env": (), "set": (), "export": ("-p",), "declare": ("-p", "-x", "-px")}
#: Words that run the rest of the line as a command.
WRAPPERS = {"nice": 0, "nohup": 0, "time": 0, "command": 0, "builtin": 0, "exec": 0, "stdbuf": 0,
            "ionice": 0, "setsid": 0, "timeout": 1, "env": 0, "xargs": 0}
ELEVATE = frozenset({"sudo", "su", "doas", "pkexec", "chroot", "runuser"})
ALWAYS_DESTRUCTIVE = {
    "docker": "runs containers with access to the machine", "podman": "runs containers",
    "crontab": "schedules commands", "systemctl": "controls system services", "launchctl": "controls services",
    "service": "controls system services", "kill": "stops processes", "pkill": "stops processes",
    "killall": "stops processes", "shutdown": "stops the machine", "reboot": "restarts the machine",
    "halt": "stops the machine", "mkfs": "formats a disk", "fdisk": "edits disk partitions",
    "nc": "opens raw network connections", "ncat": "opens raw network connections",
    "netcat": "opens raw network connections", "telnet": "opens raw network connections",
    "ssh": "runs commands on another machine", "scp": "copies files to another machine",
    "sftp": "copies files to another machine", "ftp": "copies files to another machine",
    "helm": "changes a cluster", "eval": "runs text as a command",
}
INTERPRETERS = {
    "python": ("-c",), "python3": ("-c",), "node": ("-e", "--eval", "-p", "--print"), "nodejs": ("-e", "--eval"),
    "deno": ("eval",), "bun": ("-e", "--eval"), "ruby": ("-e",), "perl": ("-e", "-E"), "php": ("-r",),
    "bash": ("-c",), "sh": ("-c",), "zsh": ("-c",), "dash": ("-c",), "ksh": ("-c",), "fish": ("-c",),
    "pwsh": ("-c", "-Command"), "lua": ("-e",), "osascript": ("-e",), "awk": (), "gawk": (), "sed": (),
}
#: Commands that build or run the project's own code.
RUNS_REPO_CODE = frozenset({"make", "cmake", "ninja", "gradle", "gradlew", "mvn", "tox", "nox", "pytest",
                            "jest", "vitest", "mocha", "rake", "just", "task", "tsc", "webpack", "vite"})
PACKAGE_MANAGERS = {
    "npm": {"install", "i", "add", "ci", "update", "upgrade", "remove", "rm", "uninstall", "un"},
    "pnpm": {"install", "i", "add", "update", "up", "remove", "rm"},
    "yarn": {"install", "add", "upgrade", "remove"},
    "bun": {"install", "i", "add", "update", "remove", "rm"},
    "pip": {"install", "uninstall", "download"}, "pip3": {"install", "uninstall", "download"},
    "uv": {"add", "remove", "sync", "lock", "pip"}, "poetry": {"add", "install", "remove", "update", "lock"},
    "cargo": {"add", "install", "remove", "update", "uninstall"}, "go": {"get", "install"},
    "gem": {"install", "uninstall", "update"}, "pipx": {"install", "uninstall", "upgrade"},
}
SYSTEM_PACKAGES = frozenset({"apt", "apt-get", "yum", "dnf", "pacman", "brew", "snap", "port", "apk", "zypper"})
PROJECT_RUNNERS = {"npm": {"test", "t", "run", "run-script", "start", "exec"}, "pnpm": {"test", "run", "start", "exec"},
                   "yarn": {"test", "run", "start"}, "bun": {"test", "run"}, "cargo": {"build", "test", "run", "check", "clippy", "bench"},
                   "go": {"build", "test", "run", "vet", "generate"}, "uv": {"run"}, "poetry": {"run"}}
DOWNLOAD_AND_RUN = frozenset({"npx", "bunx", "pnpx", "uvx"})
PUBLISH = {"npm": {"publish", "unpublish"}, "pnpm": {"publish"}, "yarn": {"publish"}, "cargo": {"publish"},
           "gem": {"push"}, "twine": {"upload"}, "poetry": {"publish"}}
OTHER_REGISTRY = ("--registry", "--index-url", "-i", "--extra-index-url", "--index", "--source")
GLOBAL_FLAGS = ("-g", "--global", "--location=global")

GIT_READ_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "ls-tree",
                                  "cat-file", "describe", "shortlog", "grep", "reflog", "rev-list", "whatchanged"})
GIT_LOCAL_SUBCOMMANDS = frozenset({"add", "commit", "checkout", "switch", "restore", "stash", "mv", "rm"})
GIT_OUTPUT_FLAGS = ("--output", "-o")
GIT_FORCE = ("-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror", "--delete", "-d", "--prune")
THREAD_BRANCH = re.compile(r"^(refs/heads/)?xo/thread-[A-Za-z0-9._-]+$")

GH_READ = {"pr": {"view", "list", "status", "diff", "checks"}, "issue": {"view", "list", "status"},
           "repo": {"view", "list"}, "run": {"view", "list", "watch"}, "release": {"view", "list"},
           "search": {"repos", "issues", "prs", "code", "commits"}}

INFRA_READ = {"terraform": {"plan", "validate", "fmt", "show", "output", "providers", "version", "init"},
              "kubectl": {"get", "describe", "logs", "explain", "version", "top", "api-resources"}}

MCP_READ_VERBS = frozenset({"get", "list", "read", "search", "fetch", "find", "query", "describe", "view", "show"})
MCP_WRITE_VERBS = frozenset({"send", "create", "delete", "remove", "update", "post", "write", "put", "publish",
                             "merge", "upload", "add", "set", "move", "archive", "reply", "forward", "patch",
                             "execute", "run", "invite", "share", "transfer"})
COMMS_SERVER = "xo_comms"


@dataclass(frozen=True)
class Part:
    """One judged piece of an action (a simple command, a file, a URL)."""

    tier: Tier
    category: str
    reasons: tuple[str, ...] = ()
    read_only: bool = False
    secret_read: bool = False
    protected: tuple[str, ...] = ()       # protected paths it would write: always denied
    runs_repo_code: bool = False
    # What a rule matches against:
    words: tuple[str, ...] = ()           # a command's words, its program by name only
    path: str | None = None               # a file tool's path, relative to the session's folder
    host: str | None = None
    mcp: tuple[str, str] | None = None


@dataclass(frozen=True)
class Assessment:
    parts: tuple[Part, ...]
    refused: str | None = None            # the parser couldn't see through it

    @property
    def tier(self) -> Tier:
        floor = Tier.NOTABLE if self.refused else Tier.ROUTINE
        return max([floor, *(p.tier for p in self.parts)])

    @property
    def reasons(self) -> tuple[str, ...]:
        found = [f"XO can't check it: {self.refused}"] if self.refused else []
        for part in self.parts:
            found += [r for r in part.reasons if r not in found]
        return tuple(found)

    @property
    def read_only(self) -> bool:
        return not self.refused and all(p.read_only for p in self.parts)

    @property
    def protected(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(w for p in self.parts for w in p.protected))


@dataclass
class _Judge:
    tier: Tier = Tier.ROUTINE
    category: str = OTHER
    reasons: list[str] = field(default_factory=list)
    read_only: bool = False
    secret_read: bool = False
    protected: list[str] = field(default_factory=list)
    runs_repo_code: bool = False
    words: tuple[str, ...] = ()
    host: str | None = None

    def at_least(self, tier: Tier, reason: str) -> None:
        self.tier = max(self.tier, tier)
        if reason not in self.reasons:
            self.reasons.append(reason)

    def part(self, *, path: str | None = None, mcp: tuple[str, str] | None = None) -> Part:
        return Part(self.tier, self.category, tuple(self.reasons), self.read_only, self.secret_read,
                    tuple(self.protected), self.runs_repo_code, self.words, path, self.host, mcp)


# ── Words a rule matches ─────────────────────────────────────────────────────

#: git's options before the subcommand; the ones with a value take the next word.
GIT_GLOBAL_VALUED = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"})
_SHORT_FLAGS = re.compile(r"^-[A-Za-z]+$")


def canonical_words(words: Iterable[str]) -> tuple[str, ...]:
    """The form rules compare: the program by name only, git's options before
    its subcommand dropped, and each run of short flags spelled one flag a
    word, sorted, so ``rm -fr``, ``rm -rf`` and ``rm -r -f`` are the same
    words and ``git -C . push`` is ``git push`` (R-E F6, F7)."""
    words = list(words)
    if not words:
        return ()
    out = [words[0].rsplit("/", 1)[-1]]
    rest = words[1:]
    if out[0] == "git":
        while rest and rest[0].startswith("-"):
            flag = rest.pop(0)
            if flag in GIT_GLOBAL_VALUED and rest:
                rest.pop(0)
    run: list[str] = []
    for word in rest:
        if _SHORT_FLAGS.match(word):
            run += [f"-{c}" for c in word[1:]]
            continue
        out += sorted(set(run))
        run = []
        out.append(word)
    return tuple(out + sorted(set(run)))


# ── Whole actions ────────────────────────────────────────────────────────────


def assess_command(text: str, places: Places, cwd: Path | None, *, branch: str | None = None,
                   xo_ports: Iterable[int] = ()) -> Assessment:
    ports = frozenset(xo_ports)
    try:
        commands = parse(text)
        refused = None
    except ShellRefused as exc:
        commands, refused = _visible(text), exc.reason
    parts = []
    where = cwd or places.session_folder
    for command in commands:
        parts.append(_command(command, places, where, branch, ports))
        where = _after_cd(command, places, where)
    return Assessment(tuple(parts), refused)


def assess_paths(kind: str, paths: Iterable[str], places: Places, cwd: Path | None) -> Assessment:
    """A file tool: ``read``/``search``/``list`` reads, the rest write."""
    writes = kind in ("edit_file", "create_file", "delete_file")
    parts = []
    for path in paths:
        judge = _Judge(read_only=not writes)
        (_write if writes else _read)(judge, path, places, cwd)
        if not writes and judge.tier == Tier.ROUTINE:
            judge.category = READ_INSIDE
        elif writes and judge.tier == Tier.ROUTINE and not judge.protected:
            judge.category = EDIT_INSIDE
        parts.append(judge.part(path=places.relative(path, cwd)))
    return Assessment(tuple(parts) or (Part(Tier.NOTABLE, OTHER, ("it names no file",)),))


def assess_url(kind: str, url: str | None, xo_ports: Iterable[int] = ()) -> Assessment:
    judge = _Judge(read_only=True)
    if kind == "web_search":
        judge.at_least(Tier.NOTABLE, "it searches the web, sending the query to another host")
    else:
        _fetch(judge, url or "", "GET", frozenset(xo_ports))
    return Assessment((judge.part(),))


def assess_mcp(server: str, tool: str) -> Assessment:
    ref = (server, tool)
    if server == COMMS_SERVER:
        return Assessment((Part(Tier.ROUTINE, COMMS, read_only=True, mcp=ref),))
    words = set(re.split(r"[^a-z]+", tool.lower()))
    if words & MCP_WRITE_VERBS:
        return Assessment((Part(Tier.DESTRUCTIVE, OTHER, (f"the tool {server}/{tool} sends, creates or changes "
                                                          "something outside this machine",), mcp=ref),))
    if words & MCP_READ_VERBS:
        return Assessment((Part(Tier.ROUTINE, OTHER, read_only=True, mcp=ref),))
    return Assessment((Part(Tier.NOTABLE, OTHER, (f"XO doesn't know what {server}/{tool} does",), mcp=ref),))


def _visible(text: str) -> tuple[SimpleCommand, ...]:
    """The parts of a refused command line that can still be seen, with the
    hidden parts as an unknown word, so the worst visible part sets the tier."""
    blanked = re.sub(r"\$\([^)]*\)?|`[^`]*`?|[<>]\([^)]*\)?", " $HIDDEN ", text)
    blanked = blanked.split("<<", 1)[0]
    try:
        return parse(blanked[:10_000])
    except ShellRefused:
        return ()


def _after_cd(command: SimpleCommand, places: Places, where: Path | None) -> Path | None:
    """The working folder after ``command``: unknown (None) after a ``cd`` XO
    can't follow, so later relative paths count as outside."""
    if command.name not in ("cd", "pushd", "popd"):
        return where
    args = [a for a in command.argv[1:] if not a.startswith("-")]
    if command.name == "popd" or len(args) != 1:
        return None
    forms = places.resolve(args[0], where)
    return forms[1] if forms else None


# ── One simple command ───────────────────────────────────────────────────────


def _command(command: SimpleCommand, places: Places, where: Path | None, branch: str | None,
             ports: frozenset[int]) -> Part:
    judge = _Judge()
    for text in (*command.argv, *command.assignments, *(r.target for r in command.redirects)):
        _anti_forgery(judge, text, places)
    if command.assignments:
        judge.at_least(Tier.NOTABLE, "it changes the environment the command runs with")
    for redirect in command.redirects:
        _redirect(judge, redirect, places, where)
    argv = _unwrap(command.argv, judge)
    judge.words = canonical_words(argv)
    if argv:
        _program(judge, argv, places, where, branch, ports)
    elif not command.redirects:
        judge.read_only = True
    if judge.tier == Tier.ROUTINE and judge.category == OTHER and judge.read_only:
        judge.category = READ_INSIDE
    return judge.part()


def _anti_forgery(judge: _Judge, text: str, places: Places) -> None:
    if any(marker in text for marker in XO_MARKERS) or str(places.state_root) in text or "/.quirq" in text:
        judge.at_least(Tier.DESTRUCTIVE, "it names XO's own routes, state or tokens")


def _redirect(judge: _Judge, redirect: Redirect, places: Places, where: Path | None) -> None:
    if not redirect.is_file or redirect.target == "/dev/null":
        return
    if redirect.writes:
        _write(judge, redirect.target, places, where)
    else:
        _read(judge, redirect.target, places, where)


def _unwrap(argv: tuple[str, ...], judge: _Judge) -> tuple[str, ...]:
    """Strip words that only run the rest of the line (``env``, ``nohup``,
    ``timeout 10`` …); elevation is destructive outright."""
    argv = tuple(argv)
    while argv:
        name = argv[0].rsplit("/", 1)[-1]
        if name in ELEVATE:
            judge.at_least(Tier.DESTRUCTIVE, f"it runs as another user ({name})")
            argv = argv[1:]
            continue
        if name not in WRAPPERS:
            return argv
        rest = list(argv[1:])
        if name == "env" and not [a for a in rest if not a.startswith("-") and "=" not in a]:
            return argv      # bare env prints the environment
        while rest and (rest[0].startswith("-") or (name == "env" and "=" in rest[0])):
            if name == "env" and "=" in rest[0]:
                judge.at_least(Tier.NOTABLE, "it changes the environment the command runs with")
            rest.pop(0)
        rest = rest[WRAPPERS[name]:]
        if name == "xargs":
            judge.at_least(Tier.NOTABLE, "xargs adds arguments XO can't see")
            rest = [*rest, "$XARGS"]
        argv = tuple(rest)
    return argv


def _program(judge: _Judge, argv: tuple[str, ...], places: Places, where: Path | None, branch: str | None,
             ports: frozenset[int]) -> None:
    name, args = argv[0].rsplit("/", 1)[-1], list(argv[1:])
    if name in ENV_DUMPS and (ENV_DUMPS[name] is None or set(args) <= set(ENV_DUMPS[name])):
        judge.at_least(Tier.DESTRUCTIVE, "it prints the environment, which holds this session's tokens")
        return
    if name in ALWAYS_DESTRUCTIVE or name.startswith("mkfs"):
        judge.at_least(Tier.DESTRUCTIVE, f"{name} {ALWAYS_DESTRUCTIVE.get(name, 'formats a disk')}")
        return
    if name == "git":
        return _git(judge, args, places, where, branch)
    if name == "gh":
        return _gh(judge, args)
    if name in ("curl", "wget", "http", "https"):
        return _download(judge, name, args, places, where, ports)
    if name in INTERPRETERS:
        return _interpreter(judge, name, args, places, where)
    if name in SYSTEM_PACKAGES:
        judge.at_least(Tier.DESTRUCTIVE, f"{name} changes the machine's own packages")
        return
    if name in PACKAGE_MANAGERS or name in DOWNLOAD_AND_RUN or name in ("twine",):
        return _packages(judge, name, args, places, where)
    if name in INFRA_READ:
        if args and args[0] in INFRA_READ[name]:
            judge.at_least(Tier.NOTABLE, f"{name} {args[0]} reads remote infrastructure")
        else:
            judge.at_least(Tier.DESTRUCTIVE, f"{name} changes remote infrastructure")
        return
    if name in RUNS_REPO_CODE or argv[0].startswith("./") or name in ("gradlew",):
        judge.runs_repo_code = True
        judge.at_least(Tier.NOTABLE, "runs code from this repo")
        return
    if name in ("rm", "rmdir", "unlink", "shred"):
        return _remove(judge, name, args, places, where)
    if name in READ_ONLY:
        return _read_only(judge, name, args, places, where)
    if name in ("cd", "pushd", "popd"):
        judge.read_only = True
        return
    if name in ("mv", "cp", "install", "ln", "touch", "mkdir", "tee", "truncate", "chmod", "chown", "chgrp",
                "rsync", "patch", "dd"):
        return _file_write(judge, name, args, places, where)
    judge.at_least(Tier.NOTABLE, f"XO doesn't know what {name} does")


def _read_only(judge: _Judge, name: str, args: list[str], places: Places, where: Path | None) -> None:
    unsafe = next((a.split("=", 1)[0] for a in args if a.split("=", 1)[0] in READ_ONLY[name]), None)
    if unsafe:
        # rg --pre runs any program on every file it reads; the others act on what they find.
        judge.at_least(Tier.DESTRUCTIVE if name == "rg" else Tier.NOTABLE, f"{name} {unsafe} runs programs or writes")
        return
    judge.read_only = True
    paths = [a for a in args if not a.startswith("-")]
    if name in PATTERN_FIRST and not any(a in ("-e", "-f", "--regexp", "--file") for a in args):
        paths = paths[1:]
    if name in ("echo", "printf", "test", "[", "true", "false", "date", "whoami", "id", "uname", "which", "type"):
        paths = []
    for path in paths:
        _read(judge, path, places, where)


def _read(judge: _Judge, path: str, places: Places, where: Path | None) -> None:
    place = places.place(path, where)
    if place is None:
        judge.at_least(Tier.NOTABLE, f"XO can't tell where {path} is")
    elif place is Place.SECRET or place is Place.STATE_ROOT or place is Place.CLI_HOME:
        judge.secret_read = True
        judge.at_least(Tier.DESTRUCTIVE, f"it reads credentials or private state ({path})")
    elif place is not Place.INSIDE:
        judge.at_least(Tier.NOTABLE, f"it reads outside this session's folder ({path})")


def _write(judge: _Judge, path: str, places: Places, where: Path | None) -> None:
    judge.read_only = False
    why = places.protected(path, where)
    if why:
        judge.protected.append(f"{path}: {why}")
        judge.at_least(Tier.DESTRUCTIVE, f"it writes a protected path ({path})")
        return
    place = places.place(path, where)
    if place is None:
        judge.at_least(Tier.NOTABLE, f"XO can't tell where {path} is")
    elif place in (Place.STATE_ROOT, Place.OTHER_WORKTREE, Place.CLI_HOME, Place.SECRET):
        judge.at_least(Tier.DESTRUCTIVE, f"it writes {_PLACE_WORDS[place]} ({path})")
    elif place is Place.OUTSIDE:
        judge.at_least(Tier.NOTABLE, f"it writes outside this session's folder ({path})")


_PLACE_WORDS = {Place.STATE_ROOT: "XO's own state", Place.OTHER_WORKTREE: "another thread's worktree",
                Place.CLI_HOME: "an agent CLI's own folder", Place.SECRET: "a credentials file"}


def _remove(judge: _Judge, name: str, args: list[str], places: Places, where: Path | None) -> None:
    judge.read_only = False
    targets = [a for a in args if not a.startswith("-") or a == "-"]
    if not targets:
        judge.at_least(Tier.NOTABLE, f"{name} with no visible target")
    for target in targets:
        if places.resolve(target, where) is None:
            judge.at_least(Tier.DESTRUCTIVE, f"{name} with a target XO can't place ({target})")
            continue
        why = places.protected(target, where)
        if why:
            judge.protected.append(f"{target}: {why}")
            judge.at_least(Tier.DESTRUCTIVE, f"it deletes a protected path ({target})")
        elif places.place(target, where) is not Place.INSIDE:
            judge.at_least(Tier.DESTRUCTIVE, f"{name} outside this session's folder ({target})")
    if judge.tier == Tier.ROUTINE:
        judge.category = EDIT_INSIDE


def _file_write(judge: _Judge, name: str, args: list[str], places: Places, where: Path | None) -> None:
    judge.read_only = False
    operands = [a for a in args if not a.startswith("-")]
    if name == "dd":
        targets = [a.split("=", 1)[1] for a in args if a.startswith("of=")]
        reads = [a.split("=", 1)[1] for a in args if a.startswith("if=")]
    elif name in ("cp", "install", "ln", "rsync"):
        targets, reads = operands[-1:], operands[:-1]
    else:
        targets, reads = operands, []
    if name == "chmod" and any("s" in a and a.startswith(("+", "u+", "g+")) for a in operands):
        judge.at_least(Tier.DESTRUCTIVE, "chmod +s lets a program run as its owner")
    if name == "rsync" and any(":" in a for a in operands):
        judge.at_least(Tier.DESTRUCTIVE, "rsync copies to or from another machine")
    for path in reads:
        _read(judge, path, places, where)
    for path in (operands if name == "mv" else targets):
        _write(judge, path, places, where)
    if not targets:
        judge.at_least(Tier.NOTABLE, f"{name} with no visible target")
    if judge.tier == Tier.ROUTINE:
        judge.category = EDIT_INSIDE


# ── git, gh ──────────────────────────────────────────────────────────────────


def _git(judge: _Judge, args: list[str], places: Places, where: Path | None, branch: str | None) -> None:
    here = where
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag == "-c" or flag.startswith(("--config-env", "--exec-path", "--git-dir", "--work-tree", "--namespace")):
            judge.at_least(Tier.DESTRUCTIVE, f"git {flag} changes what git runs or where it writes")
            if flag == "-c" and args:
                args.pop(0)
        elif flag == "-C" and args:
            target = args.pop(0)
            forms = places.resolve(target, here)
            here = forms[1] if forms else None
    if not args:
        judge.read_only = True
        return
    sub, rest = args[0], args[1:]
    outside = here is None or places.place(str(here), None) not in (Place.INSIDE,) and here != places.session_folder
    if any(a.split("=", 1)[0] in GIT_OUTPUT_FLAGS for a in rest):
        judge.at_least(Tier.NOTABLE, f"git {sub} --output writes a file")
        for a in rest:
            if a.startswith("--output="):
                _write(judge, a.split("=", 1)[1], places, here)
        return
    if sub in GIT_READ_SUBCOMMANDS or (sub in ("branch", "tag") and _lists_only(rest)) or (
            sub == "remote" and (not rest or rest[0] in ("-v", "show", "get-url"))) or (
            sub == "config" and any(a in rest for a in ("--get", "--get-all", "--list", "-l", "--get-regexp"))) or (
            sub == "stash" and rest[:1] in (["list"], ["show"])) or (sub == "worktree" and rest[:1] == ["list"]):
        judge.read_only = True
        if outside:
            judge.at_least(Tier.NOTABLE, "it reads a repository outside this session's folder")
        else:
            judge.category = GIT_READ
        return
    if outside:
        judge.at_least(Tier.NOTABLE, f"git {sub} outside this session's folder")
    if sub == "push":
        return _git_push(judge, rest, branch)
    if sub == "config":
        judge.protected.append("git config: it writes git's own config, which can run code")
        judge.at_least(Tier.DESTRUCTIVE, "git config changes git's own settings")
        return
    if sub == "reset" and "--hard" in rest:
        judge.at_least(Tier.DESTRUCTIVE, "git reset --hard throws away work")
        return
    if sub in ("filter-branch", "filter-repo", "update-ref", "replace"):
        judge.at_least(Tier.DESTRUCTIVE, f"git {sub} rewrites history")
        return
    if sub == "clean" and outside:
        judge.at_least(Tier.DESTRUCTIVE, "git clean outside this session's folder")
        return
    if sub in ("checkout", "restore", "rm", "mv"):
        paths = rest[rest.index("--") + 1:] if "--" in rest else (
            [a for a in rest if not a.startswith("-")] if sub in ("restore", "rm", "mv") else [])
        for path in paths:
            _write(judge, path, places, here)
    if sub in GIT_LOCAL_SUBCOMMANDS or sub == "branch":
        if not outside and not judge.protected:
            judge.category = GIT_LOCAL if sub in ("add", "commit") or (sub == "checkout" and "-b" in rest) else OTHER
        return
    judge.at_least(Tier.NOTABLE, f"git {sub} needs a look")


def _lists_only(rest: list[str]) -> bool:
    listing = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--list", "-l", "--show-current", "--merged",
               "--no-merged", "--contains", "--sort", "--format", "--color", "--no-color"}
    return all(a.split("=", 1)[0] in listing for a in rest)


def _git_push(judge: _Judge, rest: list[str], branch: str | None) -> None:
    flags = [a for a in rest if a.startswith("-")]
    refs = [a for a in rest if not a.startswith("-")][1:]   # after the remote
    if any(f.split("=", 1)[0] in GIT_FORCE for f in flags) or any(r.startswith((":", "+")) for r in refs) or "--tags" in flags:
        judge.at_least(Tier.DESTRUCTIVE, "git push that forces, deletes or pushes tags")
        return
    destinations = [r.split(":", 1)[-1] for r in refs] or ([branch] if branch else [])
    if destinations and all(THREAD_BRANCH.match(d or "") for d in destinations):
        judge.at_least(Tier.NOTABLE, "git push to this thread's own branch")
    else:
        judge.at_least(Tier.DESTRUCTIVE, "git push to a shared branch")


def _gh(judge: _Judge, args: list[str]) -> None:
    group, action = (args + ["", ""])[:2]
    if group == "api":
        method = _flag_value(args, ("-X", "--method")) or "GET"
        # Fields make gh api send a POST unless a method says otherwise.
        fields = any(a.split("=", 1)[0] in ("-f", "-F", "--field", "--raw-field", "--input") for a in args)
        if method.upper() != "GET" or fields:
            judge.at_least(Tier.DESTRUCTIVE, "gh api writes to GitHub")
        else:
            judge.read_only = True
        return
    if action in GH_READ.get(group, ()):
        judge.read_only = True
        return
    if (group, action) == ("pr", "create"):
        judge.at_least(Tier.NOTABLE, "gh pr create opens a pull request")
        return
    judge.at_least(Tier.DESTRUCTIVE, f"gh {group} {action} changes GitHub".rstrip())


# ── Network ──────────────────────────────────────────────────────────────────


def _download(judge: _Judge, name: str, args: list[str], places: Places, where: Path | None,
              ports: frozenset[int]) -> None:
    """curl, wget and httpie: the method, any upload, any file written, and
    each URL."""
    data_flags = ("-d", "--data", "--data-binary", "--data-raw", "--data-urlencode", "--json", "-F", "--form",
                  "-T", "--upload-file", "--post-data", "--post-file", "--body-data", "--body-file")
    valued = ("-X", "--request", "--method", "-o", "--output", "-O", "--output-document", "-K", "--config",
              "-e", "--execute", "-H", "--header", "-u", "--user", "-A", "--user-agent", *data_flags)
    method = (_flag_value(args, ("-X", "--request", "--method")) or "GET").upper()
    if any(a.split("=", 1)[0] in data_flags for a in args):
        method = "POST" if method == "GET" else method
    settings = ("-K", "--config") if name == "curl" else ("-e", "--execute") if name == "wget" else ()
    if any(a.split("=", 1)[0] in settings for a in args):
        judge.at_least(Tier.DESTRUCTIVE, f"{name} reads settings XO can't see")
    for i, arg in enumerate(args):
        flag, eq, value = arg.partition("=")
        if flag in data_flags and not eq and i + 1 < len(args):
            value = args[i + 1]
        if flag in data_flags and value.startswith("@") and len(value) > 1:
            _read(judge, value[1:], places, where)       # curl -d @file sends the file
        if flag in ("--post-file", "--body-file", "-T", "--upload-file") and value:
            _read(judge, value, places, where)
    output = _flag_value(args, ("-o", "--output", "--output-document") if name != "wget" else ("-O", "--output-document"))
    if output and output != "-":
        _write(judge, output, places, where)
    words = _operands(args, valued)
    if name in ("http", "https"):
        # httpie: an optional method word, the URL, then items; a data item makes it a POST.
        if words and words[0].upper() in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            method = words.pop(0).upper()
        if any("=" in w or ":=" in w or "@" in w for w in words[1:]):
            method = "POST" if method == "GET" else method
        words = words[:1]
    if not words:
        judge.at_least(Tier.NOTABLE, f"{name} with no visible URL")
    for url in words:
        _fetch(judge, url, method, ports)


def _flag_value(args: list[str], flags: tuple[str, ...]) -> str | None:
    """The value of the last of ``flags`` (``-X POST`` or ``--request=POST``)."""
    found = None
    for i, arg in enumerate(args):
        flag, eq, value = arg.partition("=")
        if flag in flags:
            found = value if eq else (args[i + 1] if i + 1 < len(args) else "")
    return found


def _operands(args: list[str], valued: tuple[str, ...]) -> list[str]:
    """The words that aren't flags or flag values."""
    words, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg.startswith("-"):
            skip = arg in valued
        else:
            words.append(arg)
    return words


def _fetch(judge: _Judge, url: str, method: str, ports: frozenset[int]) -> None:
    target = url if "://" in url else f"http://{url}"
    if "$" in target:
        split = urlsplit(target.replace("$", ""))
        if split.path.startswith(("/api/", "/mcp/")) and not split.path.startswith(XO_AGENT_ROUTES):
            judge.at_least(Tier.DESTRUCTIVE, "it calls an XO route agents may not use")
        else:
            judge.at_least(Tier.NOTABLE, f"XO can't tell which host {url} reaches")
        return
    try:
        split = urlsplit(target)
        host, port = (split.hostname or "").lower(), split.port
    except ValueError:
        judge.at_least(Tier.NOTABLE, f"XO can't read the URL {url}")
        return
    judge.host = host or None
    if host in LOOPBACK and (port in ports or not ports):
        if split.path.startswith(XO_AGENT_ROUTES):
            if method == "GET":
                judge.category = XO_DOCUMENTED_GET
            else:
                judge.at_least(Tier.NOTABLE, f"it changes XO's {split.path.split('/')[2]} through the API")
        else:
            judge.at_least(Tier.DESTRUCTIVE, "it calls an XO route agents may not use")
        return
    if method != "GET":
        judge.at_least(Tier.DESTRUCTIVE, f"it sends data to {host or url} ({method})")
    elif host in KNOWN_HOSTS:
        judge.category = KNOWN_HOST_GET
    else:
        judge.at_least(Tier.NOTABLE, f"it fetches from {host or url}")


# ── Interpreters and packages ────────────────────────────────────────────────


def _interpreter(judge: _Judge, name: str, args: list[str], places: Places, where: Path | None) -> None:
    inline = INTERPRETERS[name]
    if name in ("awk", "gawk", "sed"):
        if name == "sed" and any(a.startswith("-i") or a == "--in-place" for a in args):
            judge.read_only = False
            files = [a for a in args if not a.startswith("-")][1:]
            for path in files:
                _write(judge, path, places, where)
            if judge.tier == Tier.ROUTINE:
                judge.category = EDIT_INSIDE
            return
        judge.at_least(Tier.NOTABLE, f"{name} programs can write files and run commands")
        return
    if any(a.split("=", 1)[0] in inline for a in args) or (name == "deno" and args[:1] == ["eval"]):
        judge.at_least(Tier.DESTRUCTIVE, f"{name} runs inline code XO can't check")
        return
    if args[:2] in (["-m", "pip"], ["-m", "pip3"]):
        return _packages(judge, "pip", args[2:], places, where)
    if args[:2] == ["-m", "pytest"] or args[:2] == ["-m", "unittest"]:
        judge.runs_repo_code = True
        judge.at_least(Tier.NOTABLE, "runs code from this repo")
        return
    script = next((a for a in args if not a.startswith("-")), None)
    if script is None:
        judge.at_least(Tier.DESTRUCTIVE, f"{name} runs code from its input, which XO can't check")
        return
    if places.place(script, where) is Place.INSIDE:
        judge.runs_repo_code = True
        judge.at_least(Tier.NOTABLE, "runs code from this repo")
    else:
        judge.at_least(Tier.NOTABLE, f"{name} runs {script}")


def _packages(judge: _Judge, name: str, args: list[str], places: Places, where: Path | None) -> None:
    action = next((a for a in args if not a.startswith("-")), "")
    if name in DOWNLOAD_AND_RUN or (name in ("pnpm", "yarn") and action == "dlx"):
        judge.at_least(Tier.NOTABLE, f"{name} downloads a package and runs it")
        return
    if action in PUBLISH.get(name, ()) or name == "twine":
        judge.at_least(Tier.DESTRUCTIVE, f"{name} {action} publishes a package")
        return
    if action in PROJECT_RUNNERS.get(name, ()):
        judge.runs_repo_code = True
        judge.at_least(Tier.NOTABLE, "runs code from this repo")
        return
    if action in PACKAGE_MANAGERS.get(name, ()):
        judge.at_least(Tier.NOTABLE, f"it installs or removes packages ({name} {action})")
        if any(a.split("=", 1)[0] in GLOBAL_FLAGS for a in args) or name == "pipx" or (
                name in ("cargo", "go", "gem") and action == "install"):
            judge.at_least(Tier.DESTRUCTIVE, "it installs packages for the whole machine")
        if any(a.split("=", 1)[0] in OTHER_REGISTRY for a in args):
            judge.at_least(Tier.DESTRUCTIVE, "it installs from a registry other than the default")
        return
    if action in ("ls", "list", "outdated", "view", "show", "freeze", "info", "why", "audit") and "fix" not in args:
        judge.read_only = True
        return
    judge.at_least(Tier.NOTABLE, f"XO doesn't know what {name} {action} does".rstrip())

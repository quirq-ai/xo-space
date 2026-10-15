"""The tier of an action, computed and never claimed (design ch 15 §2.2;
ch 16 §7; R-E §3.2).

Each simple command of a compound is judged on its own, and the action's tier
is the highest of its parts. Every part also gets a ``category``: what kind of
thing it is (a read inside the session's folder, read-only git, a local git
write, …). XO's default rules (``rules.py``) allow routine work by category,
so a person can revoke them one by one. The agent's own words (a reason, a
"why") never enter here.

Two principles keep this sound:

* **Allowlists for what is allowed unasked.** A command lands in a default
  rule's category only when it is a known command used with flags known to be
  safe (``READ_ANY_FLAG``, ``READ_ONLY_FLAGS``, ``GIT_READ_FLAGS``,
  ``GIT_LOCAL_FLAGS``, ``CURL_SAFE``, …). Any other flag, however harmless it
  may be, takes it out of the category and asks the person. Lists of what is
  dangerous are kept only to rate a command more severely, never to allow one.
* **Options read as the program reads them** (``options.py``): glued values,
  bundled short flags and options after operands all count.

The tables are data, and they name no agent CLI: the CLIs' own folders and
project config come in through ``Places``.
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
import socket
from dataclasses import dataclass, field
from enum import IntEnum
from functools import lru_cache
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlsplit

from services.cowork_agent.storm.review.options import Options, Spec, read_options
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
#: Matched on the path's segments, after decoding and resolving "..".
XO_AGENT_ROUTES = ("/api/inbox", "/api/xo-projects-sync")
#: Anything else naming these is XO's own machinery (anti-forgery, R-E T2).
XO_MARKERS = ("/mcp/space-comms", "/api/chat/", "XO_COMMS_TOKEN", "XO_SESSION_TOKEN", "XO_API_KEY",
              "XO_AUTH_SESSION_ID", "XO_POLL_TOKEN")


def _spec(short: str = "", *long: str, optional: str = "") -> Spec:
    return Spec(short_valued=short, short_optional=optional, long_valued=frozenset(long))


# ── Reading commands ─────────────────────────────────────────────────────────

_GREP = _spec("efmABCdD", "--regexp", "--file", "--max-count", "--after-context", "--before-context", "--context",
              "--include", "--exclude", "--exclude-dir", "--label", "--devices", "--directories", "--binary-files")
#: Commands none of whose options writes a file, runs a program or prints the
#: environment (checked against each one's manual), so every flag is safe.
#: The Spec only tells their values from their operands.
READ_ANY_FLAG: dict[str, Spec] = {
    "ls": _spec("IwT", "--hide", "--ignore", "--width", "--tabsize", "--format", "--sort", "--time",
                "--time-style", "--quoting-style", "--indicator-style", "--block-size"),
    "cat": Spec(), "head": _spec("nc", "--lines", "--bytes"), "tail": _spec("ncs", "--lines", "--bytes",
                                                                            "--sleep-interval", "--pid"),
    "wc": Spec(), "pwd": Spec(), "stat": _spec("c", "--format", "--printf"), "du": _spec("dBt", "--max-depth",
                                                                                        "--block-size", "--threshold",
                                                                                        "--exclude", "--time-style"),
    "basename": _spec("s", "--suffix"), "dirname": Spec(), "realpath": _spec("", "--relative-to", "--relative-base"),
    "readlink": Spec(), "which": Spec(), "type": Spec(), "echo": Spec(), "printf": Spec(), "true": Spec(),
    "false": Spec(), "test": Spec(), "[": Spec(), "whoami": Spec(), "id": Spec(), "uname": Spec(),
    "diff": _spec("CUFIDXSL", "--unified", "--context", "--label", "--ifdef", "--show-function-line",
                  "--ignore-matching-lines", "--exclude", "--exclude-from", "--starting-file", "--to-file",
                  "--from-file", "--width", "--tabsize", "--color", "--palette"),
    "cmp": _spec("in", "--ignore-initial", "--bytes"), "md5sum": Spec(), "sha1sum": Spec(), "sha256sum": Spec(),
    "nl": _spec("bdfhilnsvw"), "cut": _spec("bcdf", "--bytes", "--characters", "--delimiter", "--fields",
                                            "--output-delimiter"),
    "column": _spec("cstoNRWHlOE"), "strings": _spec("ntTe", "--bytes", "--radix", "--target", "--encoding"),
    "od": _spec("AjNSwt", "--address-radix", "--skip-bytes", "--read-bytes", "--strings", "--width", "--format"),
    "hexdump": _spec("enfs"), "grep": _GREP, "egrep": _GREP, "fgrep": _GREP,
}
#: Commands that only read with these flags; any other flag asks.
READ_ONLY_FLAGS: dict[str, tuple[Spec, frozenset[str]]] = {
    "rg": (_spec("eEfgtTAmBCjMd", "--regexp", "--file", "--glob", "--iglob", "--type", "--type-not",
                 "--after-context", "--before-context", "--context", "--max-count", "--max-depth", "--threads",
                 "--max-columns", "--colors", "--sort", "--sortr", "--encoding"),
           frozenset({"-n", "-i", "-S", "-s", "-l", "-c", "-w", "-v", "-F", "-x", "-o", "-u", "-H", "-I", "-N", "-q",
                      "-L", "-z", "-U", "-P", "-a", "-e", "-f", "-g", "-t", "-T", "-A", "-B", "-C", "-m", "-j", "-M",
                      "-d", "--line-number", "--ignore-case", "--smart-case", "--case-sensitive",
                      "--files-with-matches", "--files-without-match", "--count", "--count-matches",
                      "--word-regexp", "--invert-match", "--fixed-strings", "--line-regexp", "--only-matching",
                      "--unrestricted", "--hidden", "--no-ignore", "--with-filename", "--no-filename",
                      "--no-heading", "--heading", "--json", "--files", "--type", "--type-not", "--glob", "--iglob",
                      "--regexp", "--file", "--after-context", "--before-context", "--context", "--max-count",
                      "--max-depth", "--follow", "--multiline", "--pcre2", "--text", "--color", "--colors",
                      "--column", "--vimgrep", "--sort", "--sortr", "--trim", "--max-columns", "--threads", "--null",
                      "--stats", "--quiet", "--type-list", "--encoding", "--no-messages", "--passthru"})),
    "sort": (_spec("ktS", "--key", "--field-separator", "--buffer-size"),
             frozenset({"-n", "-r", "-u", "-k", "-t", "-h", "-V", "-f", "-b", "-s", "-g", "-M", "-R", "-z", "-c",
                        "-C", "-S", "--numeric-sort", "--reverse", "--unique", "--key", "--field-separator",
                        "--human-numeric-sort", "--version-sort", "--ignore-case", "--stable", "--check",
                        "--general-numeric-sort", "--month-sort", "--random-sort", "--zero-terminated",
                        "--ignore-leading-blanks", "--buffer-size"})),
    "fd": (_spec("tedEgcj", "--type", "--extension", "--max-depth", "--min-depth", "--exclude", "--glob",
                 "--color", "--threads", "--size", "--changed-within", "--changed-before", "--owner"),
           frozenset({"-t", "-e", "-d", "-E", "-H", "-I", "-s", "-i", "-a", "-l", "-0", "-g", "-L", "-F", "-c", "-j",
                      "-p", "-u", "--type", "--extension", "--max-depth", "--min-depth", "--exclude", "--hidden",
                      "--no-ignore", "--case-sensitive", "--ignore-case", "--absolute-path", "--list-details",
                      "--print0", "--glob", "--follow", "--fixed-strings", "--color", "--full-path", "--size",
                      "--changed-within", "--changed-before", "--owner", "--threads", "--unrestricted"})),
    "tree": (_spec("LIP", "--charset", "--filelimit", "--sort", "--timefmt"),
             frozenset({"-a", "-d", "-f", "-L", "-I", "-P", "-h", "-s", "-C", "-n", "-J", "-i", "-D", "-p", "-u", "-g",
                        "-F", "-l", "--noreport", "--dirsfirst", "--gitignore", "--charset", "--filelimit", "--sort",
                        "--timefmt", "--prune", "--matchdirs", "--ignore-case"})),
    "file": (_spec("eFP", "--exclude", "--separator", "--parameter"),
             frozenset({"-b", "-i", "-L", "-z", "-s", "-k", "-N", "-0", "-r", "-h", "-E", "-e", "-F", "-P", "--brief",
                        "--mime", "--mime-type", "--mime-encoding", "--dereference", "--no-dereference",
                        "--uncompress", "--special-files", "--keep-going", "--no-pad", "--print0", "--raw",
                        "--exclude", "--separator", "--parameter", "--extension", "--apple"})),
    "date": (_spec("dfr", "--date", "--file", "--reference"),
             frozenset({"-u", "-I", "-R", "-d", "-f", "-r", "--iso-8601", "--rfc-email", "--rfc-3339", "--date",
                        "--file", "--reference", "--utc", "--universal"})),
}
#: Flags of reading commands that run another program: they only raise the
#: tier (the allowlist above already keeps them from running unasked).
RUNS_PROGRAMS = {"rg": frozenset({"--pre", "--pre-glob"}), "sort": frozenset({"--compress-program"}),
                 "fd": frozenset({"-x", "--exec", "-X", "--exec-batch"})}
#: Commands whose first operand is a pattern, not a path.
PATTERN_FIRST = frozenset({"grep", "egrep", "fgrep", "rg"})
#: Commands whose operands are not paths at all.
NO_PATHS = frozenset({"echo", "printf", "test", "[", "true", "false", "date", "whoami", "id", "uname", "which",
                      "type", "pwd", "basename", "dirname"})
#: find's read-only expression words, and the ones that take a value.
FIND_SAFE = frozenset({"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex",
                       "-type", "-xtype", "-maxdepth", "-mindepth", "-size", "-mtime", "-mmin", "-atime", "-amin",
                       "-ctime", "-cmin", "-newer", "-empty", "-readable", "-writable", "-executable", "-user",
                       "-group", "-perm", "-links", "-inum", "-samefile", "-print", "-print0", "-prune", "-not",
                       "-and", "-or", "-a", "-o", "-true", "-false", "-depth", "-follow", "-L", "-H", "-P", "-mount",
                       "-xdev", "-noleaf", "-daystart", "-quit", "-ls", "-printf", "-regextype", "!", "(", ")", ","})
FIND_VALUED = frozenset({"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex",
                         "-type", "-xtype", "-maxdepth", "-mindepth", "-size", "-mtime", "-mmin", "-atime", "-amin",
                         "-ctime", "-cmin", "-newer", "-user", "-group", "-perm", "-links", "-inum", "-samefile",
                         "-printf", "-regextype"})
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

# ── Interpreters ─────────────────────────────────────────────────────────────

_SHELL = _spec("o", optional="")
#: Each interpreter's options, and the flags that hand it code inline.
INTERPRETERS: dict[str, tuple[Spec, frozenset[str]]] = {
    "python": (_spec("cmWXQ"), frozenset({"-c"})), "python3": (_spec("cmWXQ"), frozenset({"-c"})),
    "node": (_spec("epr", "--eval", "--print", "--require", "--import", "--loader"),
             frozenset({"-e", "-p", "--eval", "--print"})),
    "nodejs": (_spec("epr", "--eval", "--print", "--require"), frozenset({"-e", "-p", "--eval", "--print"})),
    "bun": (_spec("er", "--eval", "--print"), frozenset({"-e", "--eval", "--print"})),
    "deno": (Spec(), frozenset()), "ruby": (_spec("eIrC"), frozenset({"-e"})),
    "perl": (_spec("eEIMm"), frozenset({"-e", "-E"})), "php": (_spec("rdcf"), frozenset({"-r"})),
    "lua": (_spec("el"), frozenset({"-e"})), "osascript": (_spec("e"), frozenset({"-e"})),
    "bash": (_SHELL, frozenset({"-c"})), "sh": (_SHELL, frozenset({"-c"})), "zsh": (_SHELL, frozenset({"-c"})),
    "dash": (_SHELL, frozenset({"-c"})), "ksh": (_SHELL, frozenset({"-c"})), "fish": (_spec("C"), frozenset({"-c"})),
    "pwsh": (Spec(), frozenset({"-c", "-C", "-Command", "-command", "-EncodedCommand", "-e", "-ec"})),
}
#: Commands that build or run the project's own code.
RUNS_REPO_CODE = frozenset({"make", "cmake", "ninja", "gradle", "gradlew", "mvn", "tox", "nox", "pytest",
                            "jest", "vitest", "mocha", "rake", "just", "task", "tsc", "webpack", "vite"})

# ── Packages ─────────────────────────────────────────────────────────────────

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
PACKAGE_SPEC = _spec("irctep", "--registry", "--index-url", "--extra-index-url", "--index", "--source", "--prefix",
                     "--target", "--root", "--cache", "--constraint", "--requirement", "--editable", "--location",
                     "--path", "--git", "--branch")
SYSTEM_PACKAGES = frozenset({"apt", "apt-get", "yum", "dnf", "pacman", "brew", "snap", "port", "apk", "zypper"})
PROJECT_RUNNERS = {"npm": {"test", "t", "run", "run-script", "start", "exec"}, "pnpm": {"test", "run", "start", "exec"},
                   "yarn": {"test", "run", "start"}, "bun": {"test", "run"},
                   "cargo": {"build", "test", "run", "check", "clippy", "bench"},
                   "go": {"build", "test", "run", "vet", "generate"}, "uv": {"run"}, "poetry": {"run"}}
DOWNLOAD_AND_RUN = frozenset({"npx", "bunx", "pnpx", "uvx"})
PUBLISH = {"npm": {"publish", "unpublish"}, "pnpm": {"publish"}, "yarn": {"publish"}, "cargo": {"publish"},
           "gem": {"push"}, "twine": {"upload"}, "poetry": {"publish"}}
OTHER_REGISTRY = frozenset({"--registry", "--index-url", "-i", "--extra-index-url", "--index", "--source"})
GLOBAL_FLAGS = frozenset({"-g", "--global"})
#: Package-manager flags naming where to install: a write there.
INSTALL_INTO = frozenset({"--prefix", "--target", "-t", "--root", "--path"})

# ── git ──────────────────────────────────────────────────────────────────────

#: git's options before the subcommand; the ones with a value take the next word.
GIT_GLOBAL_VALUED = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"})
GIT_READ_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "ls-tree",
                                  "cat-file", "describe", "shortlog", "grep", "reflog", "rev-list", "whatchanged"})
GIT_SPEC = _spec("nSGLebBcmoF", "--max-count", "--skip", "--since", "--until", "--after", "--before", "--author",
                 "--committer", "--grep", "--format", "--pretty", "--date", "--abbrev", "--diff-filter", "--sort",
                 "--contains", "--merged", "--no-merged", "--points-at", "--output", "--output-directory",
                 "--message", "--source", "--pathspec-from-file", "--src-prefix", "--dst-prefix", "--prefix",
                 "--remote", "--exec", "--push-option", "--repo", "--receive-pack", "--upload-pack", "--orphan",
                 "--create", "--track", optional="UMCB")
#: Flags of read-only git subcommands known to only read; any other asks
#: (``--output``, ``--ext-diff``, ``--no-index``, ``grep -O``, …).
GIT_READ_FLAGS = frozenset({
    "-n", "-p", "-u", "-s", "-b", "-z", "-q", "-v", "-a", "-r", "-l", "-e", "-i", "-w", "-c", "-t", "-S", "-G", "-L", "-o",
    "-U", "-M", "-C", "-B", "-1", "-2", "-3", "-4", "-5", "-6", "-7", "-8", "-9", "-E", "-F", "-P", "-h", "-H", "-W",
    "--oneline", "--max-count", "--skip", "--patch", "--no-patch", "--stat", "--numstat", "--shortstat",
    "--name-only", "--name-status", "--format", "--pretty", "--graph", "--decorate", "--no-decorate", "--all",
    "--since", "--until", "--after", "--before", "--author", "--committer", "--grep", "--color", "--no-color",
    "--abbrev-commit", "--abbrev", "--follow", "--reverse", "--first-parent", "--merges", "--no-merges", "--short",
    "--branch", "--porcelain", "--untracked-files", "--ignored", "--cached", "--staged", "--word-diff", "--unified",
    "--find-renames", "--no-renames", "--raw", "--date", "--relative-date", "--minimal", "--ignore-space-change",
    "--ignore-all-space", "--diff-filter", "--check", "--summary", "--dirstat", "--exit-code", "--quiet",
    "--full-index", "--src-prefix", "--dst-prefix", "--no-prefix", "--show-email", "--line-porcelain", "--list",
    "--others", "--exclude-standard", "--deleted", "--modified", "--full-name", "--long", "--tags", "--always",
    "--show-toplevel", "--show-prefix", "--git-dir", "--is-inside-work-tree", "--abbrev-ref", "--verify",
    "--short-format", "--count", "--left-right", "--cherry-pick", "--boundary", "--simplify-by-decoration",
    "--ignore-case", "--line-number", "--files-with-matches", "--word-regexp", "--extended-regexp",
    "--fixed-strings", "--perl-regexp", "--heading", "--break", "--null", "--show-current", "--merged",
    "--no-merged", "--contains", "--sort", "--points-at", "--remotes", "--ahead-behind", "--no-ahead-behind",
    "--stat-width", "--compact-summary", "--ignore-blank-lines", "--function-context", "--full-history",
    "--topo-order", "--date-order", "--author-date-order", "--no-walk", "--cherry", "--source", "--use-mailmap",
    "--no-abbrev", "--show-signature", "--expand-tabs", "--notes", "--no-notes", "--text", "--textconv",
    "--no-textconv", "--no-ext-diff", "--get", "--get-all", "--get-regexp", "--show-origin", "--show-scope",
})
#: The local git work the default rule covers, and the flags it may use.
GIT_LOCAL_FLAGS = {
    "add": frozenset({"-A", "--all", "-u", "--update", "-v", "--verbose", "-N", "--intent-to-add",
                      "--ignore-errors", "-n", "--dry-run"}),
    "commit": frozenset({"-m", "--message", "-a", "--all", "--amend", "--no-edit", "-q", "--quiet", "-v",
                         "--verbose", "--allow-empty", "--allow-empty-message", "-s", "--signoff", "--no-verify",
                         "-n", "--author", "--date", "--no-gpg-sign"}),
    "checkout": frozenset({"-b", "-q", "--quiet"}),
    "switch": frozenset({"-c", "--create", "-q", "--quiet"}),
}
#: Each git subcommand's options naming a file it writes.
GIT_OUTPUT = {"diff": ("--output",), "log": ("--output",), "show": ("--output",), "format-patch": ("-o", "--output-directory"),
              "archive": ("-o", "--output"), "bundle": ()}
GIT_FORCE = frozenset({"-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror", "--delete", "-d",
                       "--prune"})
THREAD_BRANCH = re.compile(r"^(refs/heads/)?xo/thread-[A-Za-z0-9._-]+$")
#: git's own short magic (":/path" is from the top; ":!" and ":^" exclude).
_PATHSPEC_SHORT = {"/": "top", "!": "exclude", "^": "exclude"}

GH_READ = {"pr": {"view", "list", "status", "diff", "checks"}, "issue": {"view", "list", "status"},
           "repo": {"view", "list"}, "run": {"view", "list", "watch"}, "release": {"view", "list"},
           "search": {"repos", "issues", "prs", "code", "commits"}}
GH_SPEC = _spec("XfFHpqt", "--method", "--field", "--raw-field", "--header", "--input", "--jq", "--template",
                "--hostname", "--preview", "--cache")

INFRA_READ = {"terraform": {"plan", "validate", "fmt", "show", "output", "providers", "version", "init"},
              "kubectl": {"get", "describe", "logs", "explain", "version", "top", "api-resources"}}

# ── Network ──────────────────────────────────────────────────────────────────

_CURL_DATA = frozenset({"-d", "--data", "--data-binary", "--data-raw", "--data-urlencode", "--data-ascii", "--json",
                        "-F", "--form", "--form-string", "-T", "--upload-file"})
_WGET_DATA = frozenset({"--post-data", "--post-file", "--body-data", "--body-file"})
#: Flags that send the request somewhere other than its URL says.
REROUTE = frozenset({"-x", "--proxy", "--preproxy", "--proxy1.0", "--connect-to", "--resolve", "--unix-socket",
                     "--abstract-unix-socket", "--socks4", "--socks4a", "--socks5", "--socks5-hostname",
                     "--doh-url", "--dns-servers", "--interface", "--haproxy-protocol"})
DOWNLOADERS = {
    "curl": {
        "spec": _spec("AbcCdDeEFHKmoPQrTuUwxXyYz", "--url", "--output", "--output-dir", "--request", "--header",
                      "--user-agent", "--referer", "--data", "--data-binary", "--data-raw", "--data-urlencode",
                      "--data-ascii", "--json", "--form", "--form-string", "--upload-file", "--cookie",
                      "--cookie-jar", "--config", "--max-time", "--connect-timeout", "--retry", "--retry-delay",
                      "--retry-max-time", "--max-redirs", "--range", "--user", "--proxy", "--preproxy",
                      "--proxy-user", "--connect-to", "--resolve", "--unix-socket", "--abstract-unix-socket",
                      "--interface", "--dns-servers", "--doh-url", "--socks4", "--socks4a", "--socks5",
                      "--socks5-hostname", "--cacert", "--capath", "--cert", "--key", "--write-out", "--dump-header",
                      "--trace", "--trace-ascii", "--stderr", "--limit-rate", "--max-filesize", "--netrc-file",
                      "--etag-save", "--etag-compare", "--hsts", "--alt-svc", "--libcurl", "--variable"),
        "data": _CURL_DATA, "settings": frozenset({"-K", "--config", "--netrc-file"}),
        "writes": frozenset({"-o", "--output", "-D", "--dump-header", "-c", "--cookie-jar", "--trace",
                             "--trace-ascii", "--stderr", "--etag-save", "--hsts", "--alt-svc", "--libcurl"}),
        "safe": frozenset({"-s", "-S", "-L", "-f", "-i", "-I", "-v", "-g", "-4", "-6", "-#", "-N", "-m", "-H", "-A",
                           "-e", "-r", "--silent", "--show-error", "--location", "--fail", "--fail-with-body",
                           "--include", "--head", "--verbose", "--globoff", "--compressed", "--http1.1", "--http2",
                           "--no-progress-meter", "--progress-bar", "--no-buffer", "--ipv4", "--ipv6", "--max-time",
                           "--connect-timeout", "--retry", "--retry-delay", "--retry-max-time", "--max-redirs",
                           "--header", "--user-agent", "--referer", "--range", "--url"}),
    },
    "wget": {
        "spec": _spec("OoaPtTUeiBwQlARDXI", "--output-document", "--output-file", "--append-output",
                      "--directory-prefix", "--tries", "--timeout", "--user-agent", "--header", "--execute",
                      "--input-file", "--base", "--post-data", "--post-file", "--body-data", "--body-file", "--method",
                      "--user", "--password", "--save-cookies", "--load-cookies", "--warc-file", "--proxy-user",
                      "--bind-address", "--ca-certificate", "--certificate", "--private-key"),
        "data": _WGET_DATA, "settings": frozenset({"-e", "--execute", "-i", "--input-file"}),
        "writes": frozenset({"-O", "--output-document", "-o", "--output-file", "-a", "--append-output",
                             "--save-cookies", "--warc-file"}),
        "safe": frozenset({"-q", "--quiet", "--no-verbose", "-v", "--verbose", "-S", "--server-response",
                           "--spider", "-t", "--tries", "-T", "--timeout", "-U", "--user-agent", "--header"}),
    },
    "http": {
        "spec": _spec("aopP", "--auth", "--output", "--print", "--pretty", "--style", "--session", "--verify",
                      "--cert", "--timeout"),
        "data": frozenset({"-f", "--form", "--raw"}), "settings": frozenset({"--session"}),
        "writes": frozenset({"-o", "--output"}),
        "safe": frozenset({"-b", "--body", "-h", "--headers", "-v", "--verbose", "--follow", "--timeout",
                           "--check-status", "--ignore-stdin", "-I", "-p", "--print", "--pretty", "--style"}),
    },
}
DOWNLOADERS["https"] = DOWNLOADERS["http"]
#: wget's two-letter short options, which a getopt reader would split.
WGET_TWO_LETTER = {"-nv": "--no-verbose", "-nc": "--no-clobber", "-nd": "--no-directories",
                   "-nH": "--no-host-directories", "-np": "--no-parent"}

MCP_READ_VERBS = frozenset({"get", "list", "read", "search", "fetch", "find", "query", "describe", "view", "show"})
MCP_WRITE_VERBS = frozenset({"send", "create", "delete", "remove", "update", "post", "write", "put", "publish",
                             "merge", "upload", "add", "set", "move", "archive", "reply", "forward", "patch",
                             "execute", "run", "invite", "share", "transfer"})
COMMS_SERVER = "xo_comms"

# ── File writers ─────────────────────────────────────────────────────────────

_TARGET_DIR = ("--target-directory", "--suffix")
FILE_WRITERS: dict[str, Spec] = {
    "cp": _spec("tS", *_TARGET_DIR), "mv": _spec("tS", *_TARGET_DIR), "ln": _spec("tS", *_TARGET_DIR),
    "install": _spec("tSmog", *_TARGET_DIR, "--mode", "--owner", "--group"),
    "rsync": _spec("ef", "--rsh", "--filter", "--exclude", "--include", "--exclude-from", "--include-from"),
    "tee": Spec(), "touch": _spec("dtr", "--date", "--reference"), "mkdir": _spec("m", "--mode"),
    "truncate": _spec("sr", "--size", "--reference"), "chmod": _spec("", "--reference"),
    "chown": _spec("", "--reference", "--from"), "chgrp": _spec("", "--reference"), "dd": Spec(),
    "patch": _spec("pioDdrBYzVgFsF", "--strip", "--input", "--output", "--directory", "--reject-file"),
}
COPIERS = frozenset({"cp", "install", "ln", "rsync", "mv"})
REMOVER_SPEC = Spec()


@dataclass(frozen=True)
class Part:
    """One judged piece of an action (a simple command, a file, a URL)."""

    tier: Tier
    category: str
    reasons: tuple[str, ...] = ()
    read_only: bool = False
    secret_read: bool = False             # it may read credentials (or XO can't tell what it reads)
    protected: tuple[str, ...] = ()       # protected paths it would write: always denied
    runs_repo_code: bool = False
    unverifiable: bool = False            # it writes where XO can't tell: no grant or rule may cover it
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

    @property
    def unverifiable(self) -> bool:
        return any(p.unverifiable for p in self.parts)


@dataclass
class _Judge:
    """Collects one part's features. Findings only ever add up: once a part
    writes (``wrote``) or uses a flag outside its allowlist (``unknown``),
    nothing later can put it in a default rule's category."""

    tier: Tier = Tier.ROUTINE
    category: str = OTHER
    reasons: list[str] = field(default_factory=list)
    read_only: bool = False               # the program only reads, as far as its own words go
    wrote: bool = False                   # something in it writes a file
    secret_read: bool = False
    protected: list[str] = field(default_factory=list)
    runs_repo_code: bool = False
    unverifiable: bool = False
    unknown_flags: list[str] = field(default_factory=list)
    words: tuple[str, ...] = ()
    host: str | None = None

    def at_least(self, tier: Tier, reason: str) -> None:
        self.tier = max(self.tier, tier)
        if reason not in self.reasons:
            self.reasons.append(reason)

    def unknown(self, program: str, flag: str) -> None:
        """A flag outside the program's allowlist: it asks, whatever it does."""
        self.unknown_flags.append(flag)
        self.at_least(Tier.NOTABLE, f"`{program} {flag}` is not on XO's list of flags that run unasked")

    def part(self, *, path: str | None = None, mcp: tuple[str, str] | None = None) -> Part:
        category = self.category
        if self.wrote:
            # A write never rides on a read, git or network default rule.
            routine_edit = self.tier == Tier.ROUTINE and not self.protected and category in (OTHER, EDIT_INSIDE)
            category = EDIT_INSIDE if routine_edit else OTHER
        if self.unknown_flags:
            category = OTHER
        return Part(self.tier, category, tuple(self.reasons), self.read_only and not self.wrote, self.secret_read,
                    tuple(self.protected), self.runs_repo_code, self.unverifiable, self.words, path, self.host, mcp)


# ── Words a rule matches ─────────────────────────────────────────────────────

_SHORT_FLAGS = re.compile(r"^-[A-Za-z]+$")
#: Other spellings of the same flag, for the commands whose rules matter most.
FLAG_ALIASES = {
    "rm": {"-R": "-r", "--recursive": "-r", "--force": "-f", "--dir": "-d", "--verbose": "-v",
           "--interactive": "-i"},
    "cp": {"-R": "-r", "--recursive": "-r", "--force": "-f", "--archive": "-a", "--verbose": "-v"},
    "mv": {"--force": "-f", "--verbose": "-v", "--no-clobber": "-n"},
    "chmod": {"--recursive": "-R", "--verbose": "-v"},
    "chown": {"--recursive": "-R", "--verbose": "-v"},
}


def canonical_words(words: Iterable[str]) -> tuple[str, ...]:
    """The form rules compare. The program by name only; git's options
    before its subcommand dropped; and each run of short flags, with their
    long spellings, folded into one sorted word, so ``rm -fr``, ``rm -rf``,
    ``rm -r -f``, ``rm -fR`` and ``rm --recursive --force`` are all
    ``rm -fr`` and ``git -C . push`` is ``git push`` (R-E F6, F7). Rules
    compare a command's flags as sets wherever they stand (``rules.py``)."""
    words = list(words)
    if not words:
        return ()
    program = words[0].rsplit("/", 1)[-1]
    aliases = FLAG_ALIASES.get(program, {})
    out = [program]
    rest = words[1:]
    if program == "git":
        while rest and rest[0].startswith("-"):
            flag = rest.pop(0)
            if flag in GIT_GLOBAL_VALUED and rest:
                rest.pop(0)
    run: set[str] = set()
    for word in rest:
        word = aliases.get(word, word)
        if _SHORT_FLAGS.match(word):
            run.update(aliases.get(f"-{c}", f"-{c}")[1] for c in word[1:])
            continue
        if run:
            out.append("-" + "".join(sorted(run)))
            run = set()
        out.append(word)
    if run:
        out.append("-" + "".join(sorted(run)))
    return tuple(out)


def is_flag_run(word: str) -> bool:
    return bool(_SHORT_FLAGS.match(word))


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
        parts.append(judge.part(path=places.relative(path, cwd)))
    return Assessment(tuple(parts) or (Part(Tier.NOTABLE, OTHER, ("it names no file",)),))


def assess_url(kind: str, url: str | None, places: Places, xo_ports: Iterable[int] = ()) -> Assessment:
    judge = _Judge(read_only=True)
    if kind == "web_search":
        judge.at_least(Tier.NOTABLE, "it searches the web, sending the query to another host")
    else:
        _fetch(judge, url or "", "GET", frozenset(xo_ports), places)
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
    if judge.tier == Tier.ROUTINE and judge.category == OTHER and judge.read_only and not judge.wrote:
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
    if name in DOWNLOADERS:
        return _download(judge, name, args, places, where, ports)
    if name in INTERPRETERS or name in ("awk", "gawk", "sed"):
        return _interpreter(judge, name, args, places, where)
    if name in SYSTEM_PACKAGES:
        judge.at_least(Tier.DESTRUCTIVE, f"{name} changes the machine's own packages")
        return
    if name in PACKAGE_MANAGERS or name in DOWNLOAD_AND_RUN or name == "twine":
        return _packages(judge, name, args, places, where)
    if name in INFRA_READ:
        if args and args[0] in INFRA_READ[name]:
            judge.at_least(Tier.NOTABLE, f"{name} {args[0]} reads remote infrastructure")
        else:
            judge.at_least(Tier.DESTRUCTIVE, f"{name} changes remote infrastructure")
        return
    if name in RUNS_REPO_CODE or argv[0].startswith("./"):
        judge.runs_repo_code = True
        judge.at_least(Tier.NOTABLE, "runs code from this repo")
        return
    if name in ("rm", "rmdir", "unlink", "shred"):
        return _remove(judge, name, args, places, where)
    if name == "find":
        return _find(judge, args, places, where)
    if name in READ_ANY_FLAG or name in READ_ONLY_FLAGS:
        return _read_only(judge, name, args, places, where)
    if name in ("cd", "pushd", "popd"):
        judge.read_only = True
        return
    if name in FILE_WRITERS:
        return _file_write(judge, name, args, places, where)
    judge.at_least(Tier.NOTABLE, f"XO doesn't know what {name} does")


def _read_only(judge: _Judge, name: str, args: list[str], places: Places, where: Path | None) -> None:
    if name in READ_ANY_FLAG:
        options = read_options(args, READ_ANY_FLAG[name])
    else:
        spec, allowed = READ_ONLY_FLAGS[name]
        options = read_options(args, spec)
        for flag in options.names() - allowed:
            judge.unknown(name, flag)
        for flag in options.names() & RUNS_PROGRAMS.get(name, frozenset()):
            judge.at_least(Tier.DESTRUCTIVE, f"{name} {flag} runs another program on what it reads")
    judge.read_only = True
    paths = [] if name in NO_PATHS else list(options.operands)
    if name in PATTERN_FIRST and not options.has("-e", "-f", "--regexp", "--file"):
        paths = paths[1:]
    for path in paths:
        _read(judge, path, places, where)


def _find(judge: _Judge, args: list[str], places: Places, where: Path | None) -> None:
    """find's own grammar: start points, then an expression. Only read-only
    expression words run unasked; ``-exec``, ``-delete``, ``-fprint`` … ask."""
    judge.read_only = True
    starts, i = [], 0
    while i < len(args) and not (args[i].startswith("-") or args[i] in ("(", "!")):
        starts.append(args[i])
        i += 1
    while i < len(args):
        word = args[i]
        if word not in FIND_SAFE:
            judge.unknown("find", word)
            if word in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"):
                judge.read_only = False
        i += 2 if word in FIND_VALUED else 1
    for start in starts or ["."]:
        _read(judge, start, places, where)


def _read(judge: _Judge, path: str, places: Places, where: Path | None) -> None:
    place = places.place(path, where)
    if place is None:
        # A variable, a glob or a brace expansion can name anything, credentials included.
        judge.secret_read = True
        judge.at_least(Tier.NOTABLE, f"XO can't tell where {path} is, so it may be a secret")
    elif place is Place.SECRET or place is Place.STATE_ROOT or place is Place.CLI_HOME:
        judge.secret_read = True
        judge.at_least(Tier.DESTRUCTIVE, f"it reads credentials or private state ({path})")
    elif place is not Place.INSIDE:
        judge.at_least(Tier.NOTABLE, f"it reads outside this session's folder ({path})")


def _write(judge: _Judge, path: str, places: Places, where: Path | None) -> None:
    judge.wrote = True
    if places.resolve(path, where) is None:
        _unverifiable(judge, path)
        return
    why = places.protected(path, where)
    if why:
        judge.protected.append(f"{path}: {why}")
        judge.at_least(Tier.DESTRUCTIVE, f"it writes a protected path ({path})")
        return
    place = places.place(path, where)
    if place in _PLACE_WORDS:
        judge.at_least(Tier.DESTRUCTIVE, f"it writes {_PLACE_WORDS[place]} ({path})")
    elif place is Place.OUTSIDE:
        judge.at_least(Tier.NOTABLE, f"it writes outside this session's folder ({path})")


_PLACE_WORDS = {Place.STATE_ROOT: "XO's own state", Place.OTHER_WORKTREE: "another thread's worktree",
                Place.CLI_HOME: "an agent CLI's own folder", Place.SECRET: "a credentials file",
                Place.HOME_CONFIG: "a file in your home that shells and tools load"}


def _unverifiable(judge: _Judge, path: str) -> None:
    _cannot_check(judge, f"XO can't tell where {path} is, so it can't check it isn't protected")


def _cannot_check(judge: _Judge, reason: str) -> None:
    """A write XO can't place may hit a protected path, so no grant or rule
    may cover it: the person sees it every time."""
    judge.unverifiable = True
    judge.at_least(Tier.DESTRUCTIVE, reason)


def _guard(judge: _Judge, path: str, places: Places, where: Path | None) -> None:
    """Only the protected-path check, for a path a command may write without
    it being its main effect (a git pathspec, a file landing in a folder)."""
    if places.resolve(path, where) is None:
        _unverifiable(judge, path)
        return
    why = places.protected(path, where)
    if why:
        judge.protected.append(f"{path}: {why}")
        judge.at_least(Tier.DESTRUCTIVE, f"it writes a protected path ({path})")


def _remove(judge: _Judge, name: str, args: list[str], places: Places, where: Path | None) -> None:
    judge.wrote = True
    targets = list(read_options(args, REMOVER_SPEC).operands)
    if not targets:
        judge.at_least(Tier.NOTABLE, f"{name} with no visible target")
    for target in targets:
        forms = places.resolve(target, where)
        if forms is None:
            _unverifiable(judge, target)
            continue
        if any(form == places.session_folder or form in places.session_folder.parents for form in forms):
            judge.at_least(Tier.DESTRUCTIVE, f"{name} would delete this session's whole folder ({target})")
            continue
        why = places.protected(target, where)
        if why:
            judge.protected.append(f"{target}: {why}")
            judge.at_least(Tier.DESTRUCTIVE, f"it deletes a protected path ({target})")
        elif places.place(target, where) is not Place.INSIDE:
            judge.at_least(Tier.DESTRUCTIVE, f"{name} outside this session's folder ({target})")


def _file_write(judge: _Judge, name: str, args: list[str], places: Places, where: Path | None) -> None:
    judge.wrote = True
    options = read_options(args, FILE_WRITERS[name])
    operands = list(options.operands)
    into = options.value("-t", "--target-directory") if name in ("cp", "mv", "ln", "install") else None
    if name == "dd":
        targets = [a.split("=", 1)[1] for a in operands if a.startswith("of=")]
        reads = [a.split("=", 1)[1] for a in operands if a.startswith("if=")]
    elif name in COPIERS and into is not None:
        targets, reads = [into], operands
    elif name in COPIERS:
        targets, reads = operands[-1:], operands[:-1]
    elif name in ("chmod", "chown", "chgrp") and not options.has("--reference"):
        targets, reads = operands[1:], []       # the first operand is the mode or the owner
    elif name == "patch":
        # patch rewrites the file named first (or writes -o's), reading the
        # second as the patch. With no file named, or a -d folder, it writes
        # whatever files the patch itself names, which XO can't see.
        outputs, inputs = options.values("-o", "--output"), options.values("-i", "--input")
        if not (operands or outputs) or options.has("-d", "--directory"):
            _cannot_check(judge, "patch writes the files its patch names, which XO can't check aren't protected")
            targets, reads = [], operands + inputs
        elif outputs:
            targets, reads = outputs, operands[:2] + inputs
        else:
            targets, reads = operands[:1], operands[1:2] + inputs
    else:
        targets, reads = operands, []
    if name == "chmod" and operands and re.search(r"[ug]?\+[a-z]*s", operands[0]):
        judge.at_least(Tier.DESTRUCTIVE, "chmod +s lets a program run as its owner")
    if name == "rsync" and any(":" in a for a in operands):
        judge.at_least(Tier.DESTRUCTIVE, "rsync copies to or from another machine")
    for path in reads:
        _read(judge, path, places, where)
    for path in (*targets, *(reads if name == "mv" else ())):   # mv also removes its sources
        _write(judge, path, places, where)
    if name in COPIERS:
        # The destination may be a folder: each source lands inside it by name.
        for folder in targets:
            for source in reads:
                _guard(judge, f"{folder.rstrip('/')}/{source.rstrip('/').rsplit('/', 1)[-1]}", places, where)
    if not targets:
        judge.at_least(Tier.NOTABLE, f"{name} with no visible target")


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
    sub, options = args[0], read_options(args[1:], GIT_SPEC)
    outside = here is None or places.place(str(here), None) is not Place.INSIDE and here != places.session_folder
    for output in options.values(*GIT_OUTPUT.get(sub, ("--output",))):
        judge.at_least(Tier.NOTABLE, f"git {sub} writes a file")
        _write(judge, output, places, here)
    if options.has("--pathspec-from-file"):
        _unverifiable(judge, "the paths listed in --pathspec-from-file")
    if sub in GIT_READ_SUBCOMMANDS or (sub in ("branch", "tag") and _lists_only(options)) or (
            sub == "remote" and (not options.operands or options.operands[0] in ("show", "get-url"))
            and options.names() <= {"-v", "--verbose"}) or (
            sub == "config" and options.has("--get", "--get-all", "--list", "-l", "--get-regexp")) or (
            sub in ("stash", "worktree") and options.operands[:1] == ("list",)) or (
            sub == "stash" and options.operands[:1] == ("show",)):
        _git_read(judge, sub, options, outside)
        return
    if outside:
        judge.at_least(Tier.NOTABLE, f"git {sub} outside this session's folder")
    if sub == "push":
        return _git_push(judge, options, branch)
    if sub == "config":
        judge.protected.append("git config: it writes git's own config, which can run code")
        judge.at_least(Tier.DESTRUCTIVE, "git config changes git's own settings")
        return
    if sub == "reset" and options.has("--hard"):
        judge.at_least(Tier.DESTRUCTIVE, "git reset --hard throws away work")
        return
    if sub in ("filter-branch", "filter-repo", "update-ref", "replace"):
        judge.at_least(Tier.DESTRUCTIVE, f"git {sub} rewrites history")
        return
    if sub == "clean" and outside:
        judge.at_least(Tier.DESTRUCTIVE, "git clean outside this session's folder")
        return
    if sub in ("checkout", "restore", "rm", "mv"):
        # Any operand may be a pathspec, with "--" or without (git checkout
        # HEAD .xo/project.json), magic included (":(top)…", ":/…"); a branch
        # name never matches a protected path, so checking every operand
        # costs nothing.
        top = places.worktree or places.session_folder
        for operand in options.operands:
            path = _pathspec(operand, here, top)
            if path is not None:
                _guard(judge, path, places, here)
    if sub in ("add", "commit", "checkout", "switch", "restore", "stash", "mv", "rm", "branch"):
        if not outside and not judge.protected and _is_git_local(sub, options):
            judge.category = GIT_LOCAL
        return
    judge.at_least(Tier.NOTABLE, f"git {sub} needs a look")


def _git_read(judge: _Judge, sub: str, options: Options, outside: bool) -> None:
    judge.read_only = True
    for flag in options.names() - GIT_READ_FLAGS:
        judge.unknown(f"git {sub}", flag)
    if outside:
        judge.at_least(Tier.NOTABLE, "it reads a repository outside this session's folder")
    else:
        judge.category = GIT_READ


def _is_git_local(sub: str, options: Options) -> bool:
    """The local git work XO's default rule covers: ``git add``, ``git
    commit``, and creating a branch (``checkout -b NAME [START]``,
    ``switch -c``), each with allowlisted flags only."""
    allowed = GIT_LOCAL_FLAGS.get(sub)
    if allowed is None or not options.names() <= allowed:
        return False
    if sub in ("checkout", "switch"):
        creates = options.has("-b") if sub == "checkout" else options.has("-c", "--create")
        return creates and len(options.operands) <= 1
    return True


def _pathspec(spec: str, here: Path | None, top: Path) -> str | None:
    """The file a git pathspec names, read the way git reads it: ``:/x`` and
    ``:(top)x`` from the top of the work tree, exclusions (``:!x``,
    ``:(exclude)x``) touch nothing (None), and a pattern (git's default
    globbing, ``:(glob)``, ``:(icase)``) is returned as-is to count as
    unplaceable."""
    if not spec.startswith(":") or spec == ":":
        return spec
    if spec.startswith(":("):
        close = spec.find(")")
        if close < 0:
            return "*"                          # malformed magic: unplaceable
        magic, rest = set(spec[2:close].split(",")), spec[close + 1:]
    else:
        magic, rest = set(), spec[1:]
        while rest[:1] in _PATHSPEC_SHORT:
            magic.add(_PATHSPEC_SHORT[rest[0]])
            rest = rest[1:]
    if "exclude" in magic:
        return None
    if magic - {"top", "literal"}:
        return "*"                              # glob, icase, attr…: unplaceable
    base = top if "top" in magic else here
    return str(base / rest) if base is not None else "*"


def _lists_only(options: Options) -> bool:
    listing = {"-a", "--all", "-r", "--remotes", "-v", "-l", "--list", "--show-current", "--merged",
               "--no-merged", "--contains", "--sort", "--format", "--color", "--no-color", "--points-at"}
    return options.names() <= listing and not (options.operands and not options.has("-l", "--list"))


def _git_push(judge: _Judge, options: Options, branch: str | None) -> None:
    refs = list(options.operands[1:])         # after the remote
    if options.names() & GIT_FORCE or options.has("--tags") or any(r.startswith((":", "+")) for r in refs):
        judge.at_least(Tier.DESTRUCTIVE, "git push that forces, deletes or pushes tags")
        return
    destinations = [r.split(":", 1)[-1] for r in refs] or ([branch] if branch else [])
    if destinations and all(THREAD_BRANCH.match(d or "") for d in destinations):
        judge.at_least(Tier.NOTABLE, "git push to this thread's own branch")
    else:
        judge.at_least(Tier.DESTRUCTIVE, "git push to a shared branch")


def _gh(judge: _Judge, args: list[str]) -> None:
    options = read_options(args, GH_SPEC)
    group, action = (list(options.operands) + ["", ""])[:2]
    if group == "api":
        method = options.value("-X", "--method") or "GET"
        # Fields make gh api send a POST unless a method says otherwise.
        if method.upper() != "GET" or options.has("-f", "-F", "--field", "--raw-field", "--input"):
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
    """curl, wget and httpie: the method, any upload or rerouting, every file
    written, and each URL. Only allowlisted flags keep it a plain GET a
    default rule may allow."""
    tool = DOWNLOADERS[name]
    if name == "wget":
        args = [WGET_TWO_LETTER.get(a, a) for a in args]
    options = read_options(args, tool["spec"])
    for flag in options.names() - tool["safe"]:
        if not (name == "wget" and flag in ("-O", "--output-document")
                and options.value("-O", "--output-document") == "-"):
            judge.unknown(name, flag)
    method = (options.value("-X", "--request", "--method") or "GET").upper()
    if options.names() & tool["data"]:
        method = "POST" if method == "GET" else method
    if options.names() & tool["settings"]:
        judge.at_least(Tier.DESTRUCTIVE, f"{name} reads settings XO can't see")
    for flag, value in options.flags:
        if value and value.startswith("@") and len(value) > 1:
            _read(judge, value[1:], places, where)          # curl -d @file, -H @file send the file
        elif flag in ("--post-file", "--body-file", "-T", "--upload-file") and value:
            _read(judge, value, places, where)
        if flag in REROUTE:
            local = value is not None and (flag in ("--unix-socket", "--abstract-unix-socket")
                                           or _names_this_machine(value))
            judge.at_least(Tier.DESTRUCTIVE if local else Tier.NOTABLE,
                           f"{name} {flag} sends the request somewhere other than its URL says")
    urls = list(options.operands) + options.values("--url")
    if name in ("http", "https"):
        # httpie: an optional method word, the URL, then items; a data item makes it a POST.
        if urls and urls[0].upper() in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            method = urls.pop(0).upper()
        if any("=" in w or ":=" in w or "@" in w for w in urls[1:]):
            method = "POST" if method == "GET" else method
        urls = urls[:1]
    if not urls:
        judge.at_least(Tier.NOTABLE, f"{name} with no visible URL")
    for path in _downloaded_files(name, options, urls, tool):
        _write(judge, path, places, where)
    for url in urls:
        _fetch(judge, url, method, ports, places)


def _downloaded_files(name: str, options: Options, urls: list[str], tool: dict) -> list[str]:
    """The files a download writes: the ones its flags name, or the ones the
    tool names after the URL."""
    named = [v for flag, v in options.flags if flag in tool["writes"] and v and v != "-"]
    if name == "curl" and options.has("-O", "--remote-name", "--remote-name-all"):
        folder = options.value("--output-dir") or "."
        named += [f"{folder.rstrip('/')}/{_url_file(url)}" for url in urls]
    elif name == "wget" and not options.has("-O", "--output-document", "--spider"):
        folder = options.value("-P", "--directory-prefix") or "."
        named += [f"{folder.rstrip('/')}/{_url_file(url)}" for url in urls]
    elif name in ("http", "https") and options.has("-d", "--download"):
        named += [f"./{_url_file(url)}" for url in urls]
    return named


def _url_file(url: str) -> str:
    try:
        path = urlsplit(url if "://" in url else f"http://{url}").path
    except ValueError:
        path = ""
    return path.rstrip("/").rsplit("/", 1)[-1] or "index.html"


def _names_this_machine(value: str) -> bool:
    """Whether a rerouting value (``host:port:addr:port``, a proxy URL, an
    address) points at this machine."""
    for word in re.split(r"[:/@,\[\]\s]+", value):
        if word and _is_loopback(word):
            return True
    return "localhost" in value.lower() or "::1" in value


def _fetch(judge: _Judge, url: str, method: str, ports: frozenset[int], places: Places) -> None:
    """One URL. XO's own server is a loopback host on one of ``ports`` (all
    of loopback when no port is known); there only the routes the
    xo-projects skill documents are open to agents (ch 16 §7). Only http and
    https count as fetches: ``file://`` is a read, anything else asks hard."""
    target = url if "://" in url else f"http://{url}"
    scheme = target.split("://", 1)[0].lower()
    if scheme == "file":
        _read(judge, unquote(urlsplit(target).path), places, None)
        return
    if scheme not in ("http", "https"):
        judge.at_least(Tier.DESTRUCTIVE, f"it speaks {scheme}://, which can reach any service")
        return
    if "$" in target or "{" in target:           # a variable, or a pattern the shell or curl expands
        path = _route_path(urlsplit(re.sub(r"[${}]", "", target)).path)
        if path.startswith(("/api/", "/mcp/")) and not _agent_route(path):
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
    if _is_loopback(host) and (port in ports or not ports):
        path = _route_path(split.path)
        if not _agent_route(path):
            judge.at_least(Tier.DESTRUCTIVE, "it calls an XO route agents may not use")
        elif method == "GET":
            judge.category = XO_DOCUMENTED_GET
        else:
            judge.at_least(Tier.NOTABLE, f"it changes XO's {path.split('/')[2]} through the API")
        return
    if method != "GET":
        judge.at_least(Tier.DESTRUCTIVE, f"it sends data to {host or url} ({method})")
    elif host in KNOWN_HOSTS:
        judge.category = KNOWN_HOST_GET
    else:
        judge.at_least(Tier.NOTABLE, f"it fetches from {host or url}")


def _route_path(path: str) -> str:
    """The path a server routes: percent-decoded until it stops changing and
    with "." and ".." resolved, as curl and the server see it
    (``/api/inbox/../secrets/env`` is ``/api/secrets/env``)."""
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    return posixpath.normpath("/" + path.lstrip("/"))


def _agent_route(path: str) -> bool:
    return any(path == route or path.startswith(route + "/") for route in XO_AGENT_ROUTES)


def _is_loopback(host: str) -> bool:
    """Every spelling of this machine: ``localhost`` and ``*.localhost``,
    ``127.0.0.0/8`` (also as ``127.1``, ``0x7f.0.0.1``, ``2130706433``),
    ``0.0.0.0``, ``::1``, IPv4-mapped forms and ``/etc/hosts`` names."""
    host = host.strip("[]").rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host in _hosts_file_loopback_names():
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


@lru_cache(maxsize=1)
def _hosts_file_loopback_names() -> frozenset[str]:
    """Names ``/etc/hosts`` maps to loopback (``localhost.localdomain``, often
    the machine's own name on ``127.0.1.1``). Read once."""
    names: set[str] = set()
    try:
        text = Path("/etc/hosts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) < 2:
            continue
        try:
            address = ipaddress.ip_address(fields[0])
        except ValueError:
            continue
        if address.is_loopback or address.is_unspecified:
            names.update(name.lower().rstrip(".") for name in fields[1:])
    return frozenset(names)


# ── Interpreters and packages ────────────────────────────────────────────────


#: What lets an awk program write a file or run a command: output redirection,
#: pipes, system(). Matching them anywhere in the program is cruder than
#: parsing it (``$3 > 100`` matches too), and errs towards asking.
_AWK_WRITES = re.compile(r">|\||system")
#: The sed commands that only print or stop, and the s/// flags that only
#: choose what is replaced and printed. Every other command or flag (w, W, e,
#: r, s///w, s///e, …) counts as one that may write or run something.
_SED_PRINT_COMMANDS = frozenset("pPdDqQ=")
_SED_SAFE_FLAGS = frozenset("gpiI0123456789")


def _sed_only_prints(script: str) -> bool:
    """True when every command of ``script`` is an allowlisted one, after
    optional addresses (a line number, ``$``, or ``/regex/``) and ``!``."""
    i, n = 0, len(script)

    def past(delimiter: str, at: int) -> int:
        """The index just past the next unescaped ``delimiter``, or -1."""
        while at < n:
            if script[at] == "\\":
                at += 2
            elif script[at] == delimiter:
                return at + 1
            else:
                at += 1
        return -1

    while i < n:
        if script[i] in " \t\n;":
            i += 1
            continue
        for _ in range(2):                          # an address, or a range of two
            if i >= n:
                return False
            if script[i].isdigit():
                while i < n and script[i].isdigit():
                    i += 1
            elif script[i] == "$":
                i += 1
            elif script[i] == "/":
                if (i := past("/", i + 1)) < 0:
                    return False
            if i < n and script[i] == ",":
                i += 1
                continue
            break
        while i < n and script[i] in " !":
            i += 1
        if i >= n:
            return False
        if script[i] in _SED_PRINT_COMMANDS:
            i += 1
        elif script[i] == "s" and i + 1 < n and script[i + 1] not in "\\\n":
            delimiter = script[i + 1]
            if (i := past(delimiter, i + 2)) < 0 or (i := past(delimiter, i)) < 0:
                return False
            while i < n and script[i] in _SED_SAFE_FLAGS:
                i += 1
        else:
            return False
    return True
_SED_SPEC = _spec("efl", "--expression", "--file", "--line-length", optional="i")
_AWK_SPEC = _spec("fvFeil", "--file", "--assign", "--field-separator", "--source", "--include", "--load")


def _text_program(judge: _Judge, name: str, args: list[str], places: Places, where: Path | None) -> None:
    """sed and awk: a program that only reads and prints is notable; one that
    may write or run something, or that XO can't read, is unverifiable."""
    if name == "sed":
        options, only_reads = read_options(args, _SED_SPEC), _sed_only_prints
        given, hidden = options.values("-e", "--expression"), options.has("-f", "--file")
    else:
        options, only_reads = read_options(args, _AWK_SPEC), (lambda program: not _AWK_WRITES.search(program))
        given = options.values("-e", "--source")
        hidden = options.has("-f", "--file", "-i", "--include", "-l", "--load")
    operands = list(options.operands)
    programs = given if (given or hidden) else operands[:1]
    files = operands if (given or hidden) else operands[1:]
    if hidden or not programs or not all(only_reads(program) for program in programs):
        judge.wrote = True
        _cannot_check(judge, f"{name}'s program may write files or run commands where XO can't see")
        return
    in_place = name == "sed" and options.has("-i", "--in-place")
    for path in files:
        (_write if in_place else _read)(judge, path, places, where)
    judge.at_least(Tier.NOTABLE, f"{name} runs a program")


def _interpreter(judge: _Judge, name: str, args: list[str], places: Places, where: Path | None) -> None:
    if name in ("awk", "gawk", "sed"):
        return _text_program(judge, name, args, places, where)
    spec, inline = INTERPRETERS[name]
    options = read_options(args, spec)
    if options.names() & inline or (name == "deno" and options.operands[:1] == ("eval",)):
        judge.at_least(Tier.DESTRUCTIVE, f"{name} runs inline code XO can't check")
        return
    module = options.value("-m")
    if module in ("pip", "pip3"):
        return _packages(judge, "pip", list(options.operands), places, where)
    if module in ("pytest", "unittest"):
        judge.runs_repo_code = True
        judge.at_least(Tier.NOTABLE, "runs code from this repo")
        return
    script = module or (options.operands[0] if options.operands else None)
    if script is None:
        judge.at_least(Tier.DESTRUCTIVE, f"{name} runs code from its input, which XO can't check")
        return
    if module is None and places.place(script, where) is Place.INSIDE:
        judge.runs_repo_code = True
        judge.at_least(Tier.NOTABLE, "runs code from this repo")
    else:
        judge.at_least(Tier.NOTABLE, f"{name} runs {script}")


def _packages(judge: _Judge, name: str, args: list[str], places: Places, where: Path | None) -> None:
    options = read_options(args, PACKAGE_SPEC)
    action = options.operands[0] if options.operands else ""
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
        if options.names() & GLOBAL_FLAGS or options.value("--location") == "global" or name == "pipx" or (
                name in ("cargo", "go", "gem") and action == "install"):
            judge.at_least(Tier.DESTRUCTIVE, "it installs packages for the whole machine")
        if options.names() & OTHER_REGISTRY:
            judge.at_least(Tier.DESTRUCTIVE, "it installs from a registry other than the default")
        for flag, value in options.flags:
            if flag in INSTALL_INTO and value:
                _write(judge, value, places, where)
        return
    if action in ("ls", "list", "outdated", "view", "show", "freeze", "info", "why", "audit") and "fix" not in options.operands:
        judge.read_only = True
        return
    judge.at_least(Tier.NOTABLE, f"XO doesn't know what {name} {action} does".rstrip())

"""Which files of a project the brain reads, and what it keeps of them.

The chunk text is copied into the state root, so this module decides what
must never get there:

* git's view first: in a repository only tracked and not-ignored files
  are read (``git ls-files``), so ``.env`` and build output stay out;
* hidden paths (``.xo/``, ``.git/``, dotfiles), dependency and build
  folders, lockfiles and anything that looks like a credential file;
* binary files, minified files and files over ``BRAIN_MAX_FILE_BYTES``;
* inside what is kept, values that look like secrets are replaced by
  ``[redacted]`` before the text is stored.

Every command runs through ``utils.commands`` (argv only, bounded).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Optional

from services.brain import config
from utils.commands import run_sync

logger = logging.getLogger(__name__)

SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", "venv", ".venv", "env", "dist", "build", "target", "vendor",
    "coverage", "out", "bin", "obj", "site-packages", "bower_components", "Pods", "DerivedData",
})
SKIP_NAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Cargo.lock",
    "composer.lock", "Gemfile.lock", "go.sum", "uv.lock", "bun.lockb", "flake.lock",
})
_SECRET_NAME_RE = re.compile(
    r"(^\.env)|(\.(pem|key|p12|pfx|crt|cer|der|jks|keystore|kdbx|gpg|asc)$)|(^id_(rsa|dsa|ecdsa|ed25519))"
    r"|(secret|credential|password|passwd|token)s?\.(json|ya?ml|toml|txt|env|ini|cfg)$",
    re.IGNORECASE)
_BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff", ".svg", ".pdf", ".zip", ".gz",
    ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war", ".class", ".so", ".dylib", ".dll", ".exe",
    ".o", ".a", ".pyc", ".pyo", ".wasm", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4",
    ".mov", ".avi", ".webm", ".wav", ".flac", ".ogg", ".db", ".sqlite", ".sqlite3", ".parquet",
    ".psd", ".sketch", ".fig", ".xlsx", ".xls", ".docx", ".pptx", ".bin", ".dat", ".lockb",
})

_REDACTIONS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[redacted]"),
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "[redacted]"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), "[redacted]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[redacted]"),
    (re.compile(
        r"(?i)\b((?:api[_-]?key|secret|token|password|passwd|pwd|auth|authorization|bearer|credential|"
        r"private[_-]?key|access[_-]?key|client[_-]?secret)[\w-]*\s*[:=]\s*['\"]?)([^\s'\",;]{8,})"),
     r"\1[redacted]"),
)


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def _skipped(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    if not parts:
        return True
    if any(p.startswith(".") for p in parts):
        return True
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    if name in SKIP_NAMES or _SECRET_NAME_RE.search(name):
        return True
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in _BINARY_SUFFIXES or name.endswith((".min.js", ".min.css", ".map")):
        return True
    return False


def _git_files(root: Path) -> Optional[list[str]]:
    if not (root / ".git").exists():
        return None
    result = run_sync(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        timeout=30, separate_stderr=True, log_label="brain: list files")
    if not result.ok:
        logger.info("brain: git ls-files failed in %s; walking the folder instead", root)
        return None
    return [p for p in result.output.split("\x00") if p]


def _walk_files(root: Path) -> list[str]:
    out: list[str] = []
    limit = config.max_files() * 4
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(filenames):
            out.append(Path(dirpath, name).relative_to(root).as_posix())
            if len(out) >= limit:
                return out
    return out


def list_files(root: Path) -> list[str]:
    """Readable candidate files, relative POSIX paths, sorted, capped at
    ``BRAIN_MAX_FILES``."""
    listed = _git_files(root)
    if listed is None:
        listed = _walk_files(root)
    keep = sorted(p for p in listed if not _skipped(p))
    cap = config.max_files()
    if len(keep) > cap:
        logger.warning("brain: %s has %d readable files; reading the first %d (BRAIN_MAX_FILES)",
                       root, len(keep), cap)
        keep = keep[:cap]
    return keep


def read_text(root: Path, rel: str) -> Optional[tuple[str, str, int]]:
    """``(text, sha256, size)`` for a readable text file, else ``None``.
    The hash is of the raw bytes, so a change in redaction rules does not
    look like a change in the file."""
    path = root / rel
    try:
        if path.is_symlink() or not path.is_file():
            return None
        resolved = path.resolve()
        if root.resolve() not in resolved.parents:
            return None
        size = path.stat().st_size
        if size == 0 or size > config.max_file_bytes():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = text.splitlines() or [""]
    if sum(len(line) for line in lines) / len(lines) > 400:
        return None   # minified or generated
    return redact(text), hashlib.sha256(data).hexdigest(), size


def _stamp(root: Path, rels) -> str:
    """Size and nanosecond mtime of each path: an edit within the same
    second, or a second edit to an already modified file, still shows."""
    digest = hashlib.sha256()
    for rel in rels:
        try:
            st = (root / rel).stat()
        except OSError:
            continue
        digest.update(f"{rel}\x00{st.st_size}\x00{st.st_mtime_ns}\n".encode("utf-8", "replace"))
    return digest.hexdigest()[:16]


def signature(root: Path) -> str:
    """A cheap fingerprint of a folder's readable state, so the background
    tick can skip a source nothing changed in: git HEAD plus the working
    tree status and the dirty files' stamps in a repository, else every
    file's stamp."""
    if (root / ".git").exists():
        head = run_sync(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=15,
                        separate_stderr=True, log_label="brain: head")
        status = run_sync(["git", "-C", str(root), "status", "--porcelain", "-uall", "-z"], timeout=30,
                          separate_stderr=True, log_label="brain: status")
        if head.ok and status.ok:
            dirty = sorted({entry[3:] for entry in status.output.split("\x00") if len(entry) > 3})
            text = status.output + _stamp(root, dirty)
            return f"git:{head.output.strip()}:{hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:16]}"
    return "walk:" + _stamp(root, _walk_files(root))

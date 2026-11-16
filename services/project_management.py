"""Clone and remove local projects.

Cloning (``POST /api/xo-projects``) validates and reserves the folder name
first (:func:`prepare_clone`), then runs ``git clone --progress`` into a
private staging folder (:mod:`services.git_clone`: progress, a no-progress
timeout instead of a wall-clock one, and a typed error per failure), publishes
it with an exclusive rename and gives it its ``.xo/``. The blocking form
awaits that; :mod:`services.project_clones` runs it as a job the frontend
polls and can cancel.

Removal deletes only this Space's local copy, so it never consults project
sharing: the swarm membership of the repo is neither checked nor changed, and
other Spaces keep their own clones. What removal does check is local: the
folder's safety (worktrees, nested repositories, mounts, symlinks) and the
local collaborator roster. For a git project it writes the relay's removal
marker first, so the sharing relay does not clone the folder back.

Linked worktrees still block removal. The status lists them (``worktrees``):
XO's thread worktrees that hold nothing to lose are offered first, each
removed through ``DELETE /api/chat/worktrees/{chat}/{ts}``; then what blocks,
each with why (XO's worktrees that hold work, and any worktree XO didn't
make).
"""
from __future__ import annotations

import asyncio
import logging
import ctypes
import errno
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from services.cowork_agent import project_layout
from services.cowork_agent.project_sharing.repo_identity import normalize_repo
from services.cowork_agent.storm.worktrees import trees as worktrees
from services.cowork_agent.visualizer import peers_store
from services import git_clone
from services.errors import ServiceError
from services.git_clone import CLONE_IDLE_TIMEOUT, run_git_clone
from services.swarm_api import auth
from services.xo_structure import ensure_xo_structure
from utils.commands import run

log = logging.getLogger(__name__)

CHECK_TIMEOUT = 15.0
#: Bounds on the name and brief a clone request carries.
DISPLAY_NAME_MAX = 200
DESCRIPTION_MAX = 4000
_RESERVED = frozenset({"agents", "memory", "state", "projects"})
_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _error(code: str, message: str, status: int = 409) -> ServiceError:
    return ServiceError(code, message, status)


def _project_id(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 160
            or value != value.strip() or value.startswith((".", "-"))
            or value in _RESERVED or any(ord(c) < 32 for c in value)
            or any(c in value for c in "/\\\x00")):
        raise _error("invalid_project_id", "Enter a project folder name without slashes or leading dots.", 400)
    return value


def _root() -> Path:
    try:
        root = project_layout.xo_projects_root()
        if root.is_symlink() or root.resolve() != root or not root.is_dir():
            raise OSError()
        return root
    except OSError as exc:
        raise _error("projects_unavailable", "The projects folder is unavailable.", 503) from exc


def _same(a, b) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _git_config_stamp(path: Path) -> tuple | None:
    try:
        value = (path / ".git" / "config").lstat()
        return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    except FileNotFoundError:
        return None


@contextmanager
def _opened(project_id: str):
    project_id = _project_id(project_id)
    root = _root()
    root_fd = project_fd = None
    try:
        root_fd = os.open(root, _OPEN_DIR)
        project_fd = os.open(project_id, _OPEN_DIR, dir_fd=root_fd)
        if not _same(os.fstat(root_fd), root.stat()) or os.fstat(project_fd).st_dev != os.fstat(root_fd).st_dev:
            raise _error("unsafe_project", "This folder cannot be removed from Setup.")
        path = root / project_id
        # Never let the running application erase its own working directory.
        if any(p == path or path in p.parents for p in (Path.cwd().resolve(), Path(__file__).resolve())):
            raise _error("project_in_use", "This project contains the running Space server.")
        yield root, path, root_fd, project_fd
    except FileNotFoundError as exc:
        raise _error("project_not_found", "Project not found.", 404) from exc
    except OSError as exc:
        raise _error("unsafe_project", "The project must be a readable local folder, not a link or mounted folder.") from exc
    finally:
        if project_fd is not None:
            os.close(project_fd)
        if root_fd is not None:
            os.close(root_fd)


def _unchanged(root: Path, path: Path, root_fd: int, project_fd: int) -> None:
    if (not _same(root.lstat(), os.fstat(root_fd))
            or not _same(path.lstat(), os.fstat(project_fd))):
        raise _error("project_changed", "The project folder changed. Refresh and try again.")


def _local_checks(path: Path, project_fd: int) -> tuple[list[dict], bool]:
    """No linked worktrees, nested repositories, mount points or hidden rosters."""
    device = os.fstat(project_fd).st_dev
    git = path / ".git"
    has_git = git.exists() or git.is_symlink()
    if has_git and (git.is_symlink() or not git.is_dir()):
        raise _error("linked_worktree", "Remove linked worktrees with Git before removing this project.")
    worktrees = git / "worktrees"
    if worktrees.exists() and (worktrees.is_symlink() or not worktrees.is_dir() or any(worktrees.iterdir())):
        raise _error("linked_worktree", "Remove this repository's linked worktrees before removing the project.")
    for name in (".xo", ".xo/peers.json", ".git/config"):
        if (path / name).is_symlink():
            raise _error("unsafe_project", "Project metadata must be stored inside this folder.")
    # A shared repository nested below a project is a separate grant. Do not
    # assume checking only the outer origin covers it.
    for directory, dirs, files in os.walk(path, followlinks=False):
        current = Path(directory)
        if current != path and (current / ".xo" / "project.json").exists():
            raise _error("nested_project", "Move nested XO projects out before removing this project.")
        if current != path and ".git" in dirs + files:
            raise _error("nested_repository", "Move nested repositories out before removing this project.")
        for name in list(dirs):
            child = current / name
            if child.is_symlink():
                dirs.remove(name)
            elif child.stat().st_dev != device:
                raise _error("mounted_folder", "Unmount folders inside this project before removing it.")
        if ".git" in dirs:
            dirs.remove(".git")
    try:
        rows = peers_store.list_peers(path / ".xo" / "peers.json")
        if any(row.get("role") not in ("owner", "collaborator", "viewer") for row in rows):
            raise ValueError()
        return rows, has_git
    except Exception as exc:
        raise _error("peers_unavailable", "The collaborator roster could not be verified. Repair it before removing this project.") from exc


async def _origin(path: Path) -> str | None:
    result = await run(["git", "-C", str(path), "config", "--get", "remote.origin.url"], timeout=CHECK_TIMEOUT, separate_stderr=True)
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip() and not result.exception:
        return None  # Git's explicit 'key does not exist', not a failed command.
    if not result.ok or not result.stdout.strip():
        raise _error("sharing_unavailable", "The repository's sharing identity could not be verified.")
    repo = normalize_repo(result.stdout.strip())
    if not repo:
        raise _error("sharing_unavailable", "The repository's origin cannot be checked for sharing.")
    return repo


async def _other_peers(rows: list[dict]) -> list[dict]:
    own = None
    if rows:
        try:
            result = await asyncio.wait_for(auth.get_user_id(), CHECK_TIMEOUT)
            if result.ok and isinstance(result.data, dict) and isinstance(result.data.get("user_id"), str):
                own = result.data["user_id"]
        except Exception:
            pass  # No verified identity means every roster entry remains a blocker.
    return [{"user_id": row["user_id"], "role": row["role"],
             "label": row.get("label") if isinstance(row.get("label"), str) else None}
            for row in rows if not own or row["user_id"] != own]


async def _worktrees(path: Path) -> dict:
    """The project's linked worktrees for the removal flow (storm ch 13 §7.4):
    ``removable`` (XO's, nothing to lose: offered first), ``blocking`` (the
    rest, each with why)."""
    registry = path / ".git" / "worktrees"
    if registry.is_symlink() or not registry.is_dir() or not any(registry.iterdir()):
        return {"removable": [], "blocking": []}
    return await worktrees.survey(path)


async def _inspect(project_id: str, path: Path, project_fd: int, audit: dict | None = None) -> dict:
    result = {"project_id": project_id, "can_remove": False, "blockers": [], "members": [], "peers": [], "repo": None,
              "worktrees": {"removable": [], "blocking": []}}
    try:
        rows, has_git = _local_checks(path, project_fd)
        if audit is not None:
            audit.update(peers=rows, has_git=has_git, git_config=_git_config_stamp(path))
        result["peers"] = await _other_peers(rows)
        # The origin names the removal marker; "members" stays [] so the
        # response keeps its shape.
        if has_git:
            result["repo"] = await _origin(path)
        if result["peers"]:
            result["blockers"].append({"code": "project_collaborators", "message": "Remove each other collaborator from the local roster before removing this project."})
    except ServiceError as exc:
        result["blockers"].append({"code": exc.code, "message": exc.message})
    try:
        result["worktrees"] = await _worktrees(path)
    except (ServiceError, OSError) as exc:
        result["blockers"].append({"code": "worktrees_unavailable", "message": "This repository's worktrees could "
                                   f"not be listed ({getattr(exc, 'message', exc)}). Run `git worktree list` in the "
                                   "project to see why."})
    result["can_remove"] = not result["blockers"]
    return result


async def removal_status(project_id: str) -> dict:
    with _opened(project_id) as (root, path, root_fd, project_fd):
        result = await _inspect(project_id, path, project_fd)
        _unchanged(root, path, root_fd, project_fd)
        return result


def _erase_contents(fd: int, device: int) -> None:
    """Delete through pinned directory descriptors; never traverse symlinks."""
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, _OPEN_DIR, dir_fd=fd)
            try:
                if not _same(info, os.fstat(child)) or info.st_dev != device:
                    raise _error("project_changed", "The project folder changed during removal.")
                _erase_contents(child, device)
                if not _same(info, os.stat(name, dir_fd=fd, follow_symlinks=False)):
                    raise _error("project_changed", "The project folder changed during removal.")
                os.rmdir(name, dir_fd=fd)
            finally:
                os.close(child)
        else:
            os.unlink(name, dir_fd=fd)


#: Called after a project is removed, with its folder name and its pid (None
#: before it had one): what depends on the project lets go of it (storm
#: retires its chat, §0.102). This module imports none of them.
_removed_listeners: list[Callable[[str, str | None], Awaitable[None]]] = []


def on_removed(listener: Callable[[str, str | None], Awaitable[None]]) -> None:
    if listener not in _removed_listeners:
        _removed_listeners.append(listener)


async def remove_project(project_id: str, confirm_project_id: str) -> dict:
    if confirm_project_id != project_id:
        raise _error("confirmation_required", "Type the project folder name to confirm removal.", 400)
    with _opened(project_id) as (root, path, root_fd, project_fd):
        audit = {}
        result = await _inspect(project_id, path, project_fd, audit)  # Never trust a prior GET.
        if not result["can_remove"]:
            first = result["blockers"][0]
            raise _error(first["code"], first["message"])
        _unchanged(root, path, root_fd, project_fd)
        if audit["has_git"] and await _origin(path) != result["repo"]:
            raise _error("project_changed", "The repository origin changed. Refresh and try again.")
        # No await between this second local read and deletion: a newly added
        # roster entry, worktree or nested repository must not slip through.
        current_rows, has_git = _local_checks(path, project_fd)
        if _git_config_stamp(path) != audit["git_config"]:
            raise _error("project_changed", "The repository configuration changed. Refresh and try again.")
        if current_rows != audit["peers"] or has_git != audit["has_git"]:
            raise _error("project_changed", "The collaborator roster changed. Refresh and try again.")
        meta = project_layout.load_project(project_id) or {}
        pid = str(meta["pid"]) if meta.get("pid") and not meta.get("_template") else None
        if result["repo"]:
            from services.cowork_agent.project_sharing import state
            state.mark_removed(result["repo"], root)
        _unchanged(root, path, root_fd, project_fd)
        _erase_contents(project_fd, os.fstat(project_fd).st_dev)
        _unchanged(root, path, root_fd, project_fd)
        os.rmdir(project_id, dir_fd=root_fd)
    for listener in list(_removed_listeners):
        try:
            await listener(project_id, pid)
        except Exception:  # noqa: BLE001 - the project is gone either way; the log says what didn't follow
            log.exception("project %s was removed, but a listener failed to let go of it", project_id)
    return {"project_id": project_id, "removed": True}


_GITHUB_SHORTHAND = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_GITHUB_PAGE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/"
                          r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?(?:/[^?#]*)?(?:[?#].*)?", re.IGNORECASE)


def _github_url(value: str) -> str:
    """What people paste for a GitHub repository, as its clone URL:
    ``owner/repo``, ``github.com/owner/repo``, or a browser address such as
    ``https://github.com/owner/repo/tree/main?tab=readme``. Anything else,
    including a URL with credentials, is returned as it is, for
    :func:`_repository_url` to judge."""
    if _GITHUB_SHORTHAND.fullmatch(value) and not value.endswith((".", "/")):
        owner, repo = value.split("/")
        repo = repo[:-4] if repo.lower().endswith(".git") else repo
        if repo not in ("", ".", ".."):
            return f"https://github.com/{owner}/{repo}.git"
        return value
    match = _GITHUB_PAGE.fullmatch(value)
    if match and match["repo"] not in (".", ".."):
        return f"https://github.com/{match['owner']}/{match['repo']}.git"
    return value


def _repository_url(value: str) -> str:
    if isinstance(value, str):
        value = _github_url(value.strip())
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise _error("invalid_repository_url", "Enter an HTTPS or SSH Git repository URL.", 400)
    if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+", value):
        value = "ssh://" + value.replace(":", "/", 1)
    try:
        url = urlsplit(value)
        valid = (url.scheme in ("https", "ssh") and url.hostname
                 and re.fullmatch(r"[A-Za-z0-9.-]+", url.hostname)
                 and url.path not in ("", "/") and not url.query and not url.fragment
                 and not url.password and (not url.username or url.scheme == "ssh")
                 and (not url.username or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", url.username))
                 and all(part not in (".", "..") for part in url.path.split("/")))
        _ = url.port
        if not valid:
            raise ValueError()
    except ValueError as exc:
        raise _error("invalid_repository_url", "Use HTTPS or SSH without passwords, tokens, query strings or local paths.", 400) from exc
    return value


def _publish_clone(staging_fd: int, root_fd: int, name: str) -> None:
    """Atomic rename without replacement on the supported POSIX platforms.

    Python's rename silently replaces empty directories. The native exclusive
    flag is necessary here: a target created during cloning belongs to someone
    else even if it is empty. Both parent directories are already pinned.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    operation = getattr(libc, "renameat2", None)
    flags = 1  # Linux RENAME_NOREPLACE
    if operation is None:
        operation = getattr(libc, "renameatx_np", None)
        flags = 4  # Darwin RENAME_EXCL
    if operation is None:
        raise _error("clone_publish_unavailable", "This server cannot safely publish a cloned project.", 503)
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(staging_fd, b"project", root_fd, os.fsencode(name), flags) != 0:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise _error("project_exists", "A project with this folder name already exists.")
        raise OSError(code, "Could not publish cloned project")


class CloneError(ServiceError):
    """A failed clone: ``detail`` is git's last stderr line, scrubbed of
    credentials and private paths (``None`` when git said nothing)."""

    def __init__(self, code: str, message: str, status: int, detail: str | None = None) -> None:
        super().__init__(code, message, status)
        self.detail = detail


#: Folder names a clone is being made into, here or in a clone job, so a
#: second request for the same name fails at once instead of after a download.
_cloning: set[str] = set()


@dataclass
class CloneRequest:
    """A validated clone that holds its folder name until :meth:`release`."""

    project_id: str
    url: str
    display_name: str | None = None
    description: str | None = None
    _released: bool = field(default=False, repr=False)

    def release(self) -> None:
        if not self._released:
            self._released = True
            _cloning.discard(self.project_id)


def _text_field(value, *, label: str, limit: int) -> str | None:
    """Same meaning as on ``POST /api/files/mkdir`` with ``scaffold``: trimmed;
    blank means "not given" (the folder name, an empty brief)."""
    if value is None:
        return None
    if not isinstance(value, str) or any(ord(c) < 32 and c not in "\r\n\t" for c in value):
        raise _error("invalid_project_details", f"The project {label} must be plain text.", 400)
    value = value.strip()
    if len(value) > limit:
        raise _error("invalid_project_details", f"The project {label} can be at most {limit} characters.", 400)
    return value or None


_STAGING_PREFIX = ".space-clone-"
#: A staging folder older than this belongs to no running clone: the server
#: that made it stopped before it could clean up.
STALE_STAGING_S = 24 * 3600


def _sweep_stale_staging(root: Path) -> None:
    """Remove staging folders a crashed or killed server left behind; a large
    repository's half-download can be gigabytes. Never raises."""
    try:
        entries = [p for p in root.iterdir() if p.name.startswith(_STAGING_PREFIX)]
    except OSError:
        return
    cutoff = time.time() - STALE_STAGING_S
    for path in entries:
        root_fd = fd = None
        try:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mtime > cutoff:
                continue
            root_fd = os.open(root, _OPEN_DIR)
            fd = os.open(path.name, _OPEN_DIR, dir_fd=root_fd)
            if not _same(info, os.fstat(fd)):
                continue
            _erase_contents(fd, info.st_dev)
            os.rmdir(path.name, dir_fd=root_fd)
            log.info("removed stale clone staging folder %s", path.name)
        except FileNotFoundError:
            pass  # another clone's sweep got there first
        except (OSError, ServiceError) as exc:
            log.warning("could not remove stale clone staging folder %s: %s", path.name, exc)
        finally:
            for open_fd in (fd, root_fd):
                if open_fd is not None:
                    os.close(open_fd)


def prepare_clone(project_id: str, repository_url: str, *, display_name: str | None = None,
                  description: str | None = None) -> CloneRequest:
    """Everything that can be checked before git runs; reserves the folder name.

    Raises a :class:`ServiceError` (400/409/503) and reserves nothing on failure.
    """
    project_id, url = _project_id(project_id), _repository_url(repository_url)
    display_name = _text_field(display_name, label="name", limit=DISPLAY_NAME_MAX)
    description = _text_field(description, label="description", limit=DESCRIPTION_MAX)
    root = _root()
    target = root / project_id
    if target.exists() or target.is_symlink():
        raise _error("project_exists", "A project with this folder name already exists.")
    if project_id in _cloning:
        raise _error("clone_in_progress", "This project is already being cloned.")
    _cloning.add(project_id)
    return CloneRequest(project_id, url, display_name, description)


async def clone_project(project_id: str, repository_url: str, *, display_name: str | None = None,
                        description: str | None = None,
                        on_progress: Callable[[dict], None] | None = None) -> dict:
    """Clone into a new project and wait for it (``POST /api/xo-projects``)."""
    request = prepare_clone(project_id, repository_url, display_name=display_name, description=description)
    return await run_clone(request, on_progress=on_progress)


async def run_clone(request: CloneRequest, *, on_progress: Callable[[dict], None] | None = None) -> dict:
    """Clone a prepared request, publish it and give it its ``.xo/``.

    ``on_progress`` gets ``{phase, phase_percent, percent, received_bytes}``
    as git reports it. Cancelling stops git, removes the staging folder and
    publishes nothing. Always releases the request's folder name.
    """
    try:
        return await _run_clone(request, on_progress)
    finally:
        request.release()


async def _run_clone(request: CloneRequest, on_progress: Callable[[dict], None] | None) -> dict:
    project_id, url = request.project_id, request.url
    root = _root()
    await asyncio.to_thread(_sweep_stale_staging, root)
    # The private temporary directory is owned by this request alone.
    staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=root))
    root_fd = os.open(root, _OPEN_DIR)
    staging_fd = os.open(staging, _OPEN_DIR)
    progress = git_clone.CloneProgress()
    published = False

    def report(latest: git_clone.CloneProgress | None = None) -> None:
        nonlocal progress
        if latest is not None:
            progress = latest
        if on_progress is not None:
            try:
                on_progress(progress.as_dict())
            except Exception:  # noqa: BLE001 - a listener never fails the clone
                log.exception("clone progress listener failed")

    try:
        configs = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                   "-c", "protocol.ssh.allow=always", "-c", "protocol.file.allow=never",
                   "-c", "protocol.ext.allow=never", "-c", "core.hooksPath=/dev/null",
                   "-c", "http.followRedirects=false"]
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes"}
        secrets: list[str] = []
        if url.startswith("https://github.com/"):
            from services.cowork_agent.xo_projects_sync import github
            try:
                credential = await github.resolve_auth(read_only=True)
                index = int(environment.get("GIT_CONFIG_COUNT", "0"))
                if index < 0:
                    raise ValueError()
                header = github._git_extraheader(credential)
                environment.update({"GIT_CONFIG_COUNT": str(index + 1),
                                    f"GIT_CONFIG_KEY_{index}": "http.https://github.com/.extraheader",
                                    f"GIT_CONFIG_VALUE_{index}": header})
                secrets += [credential.token, header]
            except github.AuthMissingError:
                pass
        report()
        destination = staging / "project"
        result = await run_git_clone(
            ["git", *configs, "clone", "--progress", "--", url, str(destination)],
            cwd=root, env=environment, on_progress=report, idle_timeout=CLONE_IDLE_TIMEOUT,
        )
        if not result.ok:
            code, message = git_clone.classify_failure(result, credential_sent=bool(secrets),
                                                       ssh=url.startswith("ssh://"))
            detail = git_clone.failure_detail(result, secrets=secrets,
                                              replace=((str(destination), project_id), (str(staging), project_id)))
            log.info("clone of %s into %s failed: %s (%s)", url, project_id, code, detail)
            raise CloneError(code, message, git_clone.FAILURES[code][0], detail)
        progress.finishing()
        report()
        # Nothing below awaits: once git is done, the publish cannot be cancelled halfway.
        _unchanged(root, staging, root_fd, staging_fd)
        _publish_clone(staging_fd, root_fd, project_id)
        published = True
        # A cloned repository gets the same .xo/ as every other project. Only
        # .xo/ is touched and nothing raises: the clone is already published.
        ensure_xo_structure(project_id)
        response = {"project_id": project_id, "created": True}
        if request.display_name is not None or request.description is not None:
            try:
                if project_layout.describe_project(project_id, display_name=request.display_name,
                                                   description=request.description) is None:
                    raise ValueError("project.json does not parse")
            except Exception:  # noqa: BLE001 - published; say what didn't stick
                log.exception("clone of %s: name and description not recorded", project_id)
                response["warning"] = ("Project added, but its name and description could not be saved. "
                                       "Set them again from the project's settings.")
        repo = normalize_repo(url)
        if repo:
            from services.cowork_agent.project_sharing import state
            try:
                state.clear_removed(repo, root)
            except OSError:
                # The clone is already complete and visible. Do not report a
                # failed add (and invite a duplicate retry) for a local marker.
                response["warning"] = "Project added, but automatic restore remains paused because its removal marker could not be cleared."
        return response
    except ServiceError:
        raise
    except Exception as exc:
        raise CloneError("clone_failed", "Could not finish cloning the repository. Check the project folder and try again.", 502) from exc
    finally:
        # A failed checkout can leave a large tree: remove it off the event
        # loop, and finish even if the clone is cancelled meanwhile.
        # Once published, the clone has happened: a cancellation that lands
        # during this cleanup must not report it as cancelled.
        await _uninterrupted(asyncio.to_thread(_discard_staging, staging, staging_fd, root_fd),
                             pass_cancellation_on=not published)


def _discard_staging(staging: Path, staging_fd: int, root_fd: int) -> None:
    """Remove this clone's private staging folder through its own descriptor
    (never what a path now names) and close both descriptors. Never raises:
    a folder left behind is swept by a later clone."""
    try:
        _erase_contents(staging_fd, os.fstat(staging_fd).st_dev)
        with suppress(FileNotFoundError):  # gone already: nothing of ours to remove
            if _same(staging.lstat(), os.fstat(staging_fd)):
                os.rmdir(staging.name, dir_fd=root_fd)
    except Exception:  # noqa: BLE001 - never hide the clone's own outcome
        log.exception("clone staging folder %s was not removed; a later clone sweeps it", staging.name)
    finally:
        os.close(staging_fd)
        os.close(root_fd)


async def _uninterrupted(awaitable, *, pass_cancellation_on: bool = True) -> None:
    """Await ``awaitable`` to its end even through cancellation, then pass a
    cancellation that arrived meanwhile on (or, told not to, absorb it)."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        if pass_cancellation_on:
            raise asyncio.CancelledError
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()

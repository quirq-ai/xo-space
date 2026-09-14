"""Clone and remove local projects, with fresh sharing checks before removal.

Remote repositories and their memberships are never deleted or revoked here.
The local peer roster and the relay membership ledger are independent checks.
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from services.cowork_agent import project_layout
from services.cowork_agent.project_sharing import config
from services.cowork_agent.project_sharing.repo_identity import normalize_repo
from services.cowork_agent.visualizer import peers_store
from services.errors import ServiceError
from services.swarm_api import auth, project_sharing as swarm
from utils.commands import run

CHECK_TIMEOUT = 15.0
CLONE_TIMEOUT = 300.0
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


async def _member_rows(repo: str) -> list[dict]:
    try:
        ok, _, payload = await asyncio.wait_for(swarm.members(repo), CHECK_TIMEOUT)
        if not ok or not isinstance(payload, dict) or not isinstance(payload.get("members"), list):
            raise ValueError()
        rows = payload["members"]
        seen = set()
        own = config.workspace_id()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("workspace_id"), str)
                    or not row["workspace_id"].strip() or row["workspace_id"] in seen
                    or row.get("role") not in ("owner", "member")
                    or row.get("status") not in ("active", "revoked")):
                raise ValueError()
            seen.add(row["workspace_id"])
        owners = [row for row in rows if row["role"] == "owner" and row["status"] == "active"]
        if rows and len(owners) != 1:
            raise ValueError()
        i_own = any(row["workspace_id"] == own for row in owners)
        return [{"workspace_id": row["workspace_id"], "role": row["role"], "status": row["status"],
                 "is_self": bool(own and row["workspace_id"] == own),
                 "can_revoke": i_own and row["role"] != "owner" and row["status"] == "active"}
                for row in rows]
    except Exception as exc:
        raise _error("sharing_unavailable", "Sharing could not be verified. Reconnect to XO and try again.") from exc


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


async def _inspect(project_id: str, path: Path, project_fd: int, audit: dict | None = None) -> dict:
    result = {"project_id": project_id, "can_remove": False, "blockers": [], "members": [], "peers": [], "repo": None}
    try:
        rows, has_git = _local_checks(path, project_fd)
        if audit is not None:
            audit.update(peers=rows, has_git=has_git, git_config=_git_config_stamp(path))
        result["peers"] = await _other_peers(rows)
        if has_git:
            result["repo"] = await _origin(path)
        if result["repo"]:
            result["members"] = await _member_rows(result["repo"])
        if any(row["status"] == "active" and not row["is_self"] for row in result["members"]):
            owner = any(row["role"] == "owner" and row["is_self"] for row in result["members"])
            message = ("Revoke access for every other Space before removing this project." if owner else
                       "Ask the sharing owner to revoke the remaining access before removing this local project.")
            result["blockers"].append({"code": "shared_project", "message": message})
        if result["peers"]:
            result["blockers"].append({"code": "project_collaborators", "message": "Remove each other collaborator from the local roster before removing this project."})
    except ServiceError as exc:
        result["blockers"].append({"code": exc.code, "message": exc.message})
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
        if result["repo"]:
            current_members = await _member_rows(result["repo"])
            if any(row["status"] == "active" and not row["is_self"] for row in current_members):
                raise _error("shared_project", "Sharing changed. Revoke each remaining user's access before removing the project.")
        # No await between this second local read and deletion: a newly added
        # roster entry, worktree or nested repository must not slip through.
        current_rows, has_git = _local_checks(path, project_fd)
        if _git_config_stamp(path) != audit["git_config"]:
            raise _error("project_changed", "The repository configuration changed. Refresh and try again.")
        if current_rows != audit["peers"] or has_git != audit["has_git"]:
            raise _error("project_changed", "The collaborator roster changed. Refresh and try again.")
        if result["repo"]:
            from services.cowork_agent.project_sharing import state
            state.mark_removed(result["repo"], root)
        _unchanged(root, path, root_fd, project_fd)
        _erase_contents(project_fd, os.fstat(project_fd).st_dev)
        _unchanged(root, path, root_fd, project_fd)
        os.rmdir(project_id, dir_fd=root_fd)
    return {"project_id": project_id, "removed": True}


def _repository_url(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 2048
            or value != value.strip() or any(c.isspace() or ord(c) < 32 for c in value)):
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


async def clone_project(project_id: str, repository_url: str) -> dict:
    project_id, url = _project_id(project_id), _repository_url(repository_url)
    root = _root()
    target = root / project_id
    if target.exists() or target.is_symlink():
        raise _error("project_exists", "A project with this folder name already exists.")
    # The private temporary directory is owned by this request alone.
    staging = Path(tempfile.mkdtemp(prefix=".space-clone-", dir=root))
    root_fd = os.open(root, _OPEN_DIR)
    staging_fd = os.open(staging, _OPEN_DIR)
    try:
        configs = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                   "-c", "protocol.ssh.allow=always", "-c", "protocol.file.allow=never",
                   "-c", "protocol.ext.allow=never", "-c", "core.hooksPath=/dev/null",
                   "-c", "http.followRedirects=false"]
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes"}
        if url.startswith("https://github.com/"):
            from services.cowork_agent.xo_projects_sync import github
            try:
                credential = await github.resolve_auth(read_only=True)
                index = int(environment.get("GIT_CONFIG_COUNT", "0"))
                if index < 0:
                    raise ValueError()
                environment.update({"GIT_CONFIG_COUNT": str(index + 1),
                                    f"GIT_CONFIG_KEY_{index}": "http.https://github.com/.extraheader",
                                    f"GIT_CONFIG_VALUE_{index}": github._git_extraheader(credential)})
            except github.AuthMissingError:
                pass
        command_task = asyncio.create_task(run(
            ["git", *configs, "clone", "--", url, str(staging / "project")],
            cwd=root, timeout=CLONE_TIMEOUT, separate_stderr=True, env=environment,
        ))
        try:
            result = await asyncio.shield(command_task)
        except asyncio.CancelledError:
            # The shared runner kills its process tree on timeout, but not on
            # coroutine cancellation. Let that bounded runner finish before
            # deleting its working directory; never publish a cancelled add.
            while not command_task.done():
                try:
                    await asyncio.shield(command_task)
                except asyncio.CancelledError:
                    continue
            try:
                command_task.result()
            except Exception:
                pass
            raise
        if not result.ok:
            message = "Cloning timed out. Try again." if result.timed_out else "Could not clone the repository. Check its URL and your Git access."
            raise _error("clone_failed", message, 502)
        _unchanged(root, staging, root_fd, staging_fd)
        _publish_clone(staging_fd, root_fd, project_id)
        repo = normalize_repo(url)
        response = {"project_id": project_id, "created": True}
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
        raise _error("clone_failed", "Could not finish cloning the repository. Check the project folder and try again.", 502) from exc
    finally:
        try:
            # Even failed git and publish operations only clean this request's
            # private directory, through its original descriptor.
            _erase_contents(staging_fd, os.fstat(staging_fd).st_dev)
            if _same(staging.lstat(), os.fstat(staging_fd)):
                os.rmdir(staging.name, dir_fd=root_fd)
        finally:
            os.close(staging_fd)
            os.close(root_fd)

"""Folder-based routes: the folder is the URL.

Any package in the repo can be part of the API. List its folder in
``config/autoroutes.json`` and the loader walks it at boot; nothing is
exposed until a folder is switched on, and a ``false`` entry switches a
subtree off under a ``true`` parent (longest path wins). Underscores in
folder and module names become hyphens in the URL (``api/xo_projects`` is
``/api/xo-projects``); ``_private`` modules and packages are never walked.

    {
      "guard": true,
      "folders": {
        "api": true,
        "services/inbox": true,
        "services/inbox/store": false
      }
    }

A module contributes in one of two ways:

* **Route module**: it defines ``router: APIRouter``. The router is mounted
  at the module's folder URL, so ``api/files/routes.py`` declaring
  ``@router.post("/upload")`` serves ``POST /api/files/upload`` and an empty
  path is the folder itself. The module name is not a segment, so several
  modules can share one folder (``api/secrets/routes.py`` and
  ``api/secrets/env.py`` both serve under ``/api/secrets``). Modules in a
  folder mount in name order; ``MOUNT_ORDER`` (an int, default 0) sorts
  first when two modules register the same path and the first must win. A
  module may also define ``absolute_router`` for URLs fixed by a protocol
  or kept for compatibility (``/callback``, ``/.well-known/...``, legacy
  aliases): it is mounted with no prefix at all.

* **Plain module**: no ``router``. Every public module-level function is
  generated as a route at ``/<folder>/<module>/<function>``:
  ``services/inbox/service.py::list_items(status, limit)`` becomes
  ``POST /services/inbox/service/list_items`` with a JSON body typed from
  the signature (defaults kept); zero-parameter functions are ``GET``.
  ``__all__`` is honoured; functions taking ``*args``/``**kwargs`` are
  skipped with a log line. Coroutines are awaited, sync functions run in the
  threadpool, results go through ``jsonable_encoder`` and a
  :class:`services.errors.ServiceError` becomes the usual ``{code, message}``
  error. Generated routes can run whatever the folder does, so with
  ``guard`` on (the default) they answer only loopback callers whose Origin
  passes :func:`routers.browser_guard.is_local_mutation`. Route modules
  handle their own access rules, as they always have.

``server.py`` mounts the result after every hand-written router, so on a
path clash a hand-written route wins. :func:`mount_module` gives a test the
same folder prefix for one module.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import pkgutil
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, get_type_hints

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import create_model
from starlette.concurrency import run_in_threadpool

from routers.browser_guard import is_local_mutation
from routers.errors import http_error
from services.errors import ServiceError

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "config" / "autoroutes.json"


# ── Config ───────────────────────────────────────────────────────────────────


def load_config(path: Path = CONFIG_FILE) -> dict:
    """The config file as a dict; a missing file means nothing is exposed."""
    if not path.is_file():
        return {"guard": True, "folders": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"autoroutes: cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("folders", {}), dict):
        raise RuntimeError(f"autoroutes: {path} must be an object with a 'folders' object")
    data.setdefault("guard", True)
    data.setdefault("folders", {})
    return data


def _normalize(folder: str) -> str:
    return folder.strip().strip("/").replace("\\", "/")


def _enabled(folder: str, folders: dict[str, bool]) -> bool:
    """Longest configured ancestor decides; unlisted folders are off."""
    parts = _normalize(folder).split("/")
    for depth in range(len(parts), 0, -1):
        candidate = "/".join(parts[:depth])
        if candidate in folders:
            return bool(folders[candidate])
    return False


def enabled_folders(config: dict) -> list[str]:
    """The switched-on folders, deepest first so a child override is applied
    before its parent adds the rest of the tree."""
    folders = {_normalize(k): v for k, v in config.get("folders", {}).items()}
    return sorted((f for f, on in folders.items() if on), key=lambda f: -f.count("/"))


# ── URLs ─────────────────────────────────────────────────────────────────────


def url_for_folder(folder: str) -> str:
    """``api/xo_projects/sync`` -> ``/api/xo-projects/sync``."""
    return "/" + "/".join(part.replace("_", "-") for part in _normalize(folder).split("/"))


def folder_of(module: ModuleType | str) -> str:
    """The repo-relative folder a module lives in (``api/files`` for both
    ``api.files`` and ``api.files.routes``)."""
    name = module if isinstance(module, str) else module.__name__
    mod = importlib.import_module(name) if isinstance(module, str) else module
    if hasattr(mod, "__path__"):  # a package: its own folder
        return name.replace(".", "/")
    return name.rsplit(".", 1)[0].replace(".", "/")


# ── Plain modules: one generated route per public function ───────────────────


def _public_functions(module: ModuleType) -> list[tuple[str, Callable]]:
    names = getattr(module, "__all__", None)
    if names is None:
        names = [n for n in vars(module) if not n.startswith("_")]
    found = []
    for name in names:
        obj = getattr(module, name, None)
        if inspect.isfunction(obj) and getattr(obj, "__module__", None) == module.__name__:
            found.append((name, obj))
    return found


_UNMODELLABLE = object()


def _request_model(name: str, func: Callable):
    """A pydantic model with one field per parameter; None for a function
    without parameters; ``_UNMODELLABLE`` when the signature cannot be
    expressed as a JSON body (``*args`` / ``**kwargs``)."""
    try:
        hints = get_type_hints(func)
    except Exception:  # unresolvable forward refs: fall back to Any
        hints = {}
    fields: dict[str, Any] = {}
    for param in inspect.signature(func).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            return _UNMODELLABLE
        annotation = hints.get(param.name, Any)
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[param.name] = (annotation, default)
    if not fields:
        return None
    return create_model(f"{name}Body", **fields)


async def _call(func: Callable, kwargs: dict) -> Any:
    try:
        if inspect.iscoroutinefunction(func):
            result = await func(**kwargs)
        else:
            result = await run_in_threadpool(partial(func, **kwargs))
    except ServiceError as exc:
        raise http_error(exc) from exc
    return jsonable_encoder(result)


def _guarded(request: Request) -> None:
    if not is_local_mutation(request):
        raise HTTPException(
            status_code=403,
            detail={"code": "local_only", "message": "generated routes answer local callers only"},
        )


def _add_generated(router: APIRouter, path: str, func: Callable, *, guard: bool, tag: str) -> str | None:
    """Register ``func`` at ``path``; returns the method, or None when the
    function was skipped."""
    body_model = _request_model(func.__name__, func)
    if body_model is _UNMODELLABLE:
        logger.warning("autoroutes: %s.%s takes *args/**kwargs; skipped", func.__module__, func.__name__)
        return None
    summary = (inspect.getdoc(func) or "").split("\n", 1)[0] or None

    if body_model is None:
        async def endpoint(request: Request):
            if guard:
                _guarded(request)
            return await _call(func, {})

        router.add_api_route(path, endpoint, methods=["GET"], summary=summary, tags=[tag], name=path)
        return "GET"

    async def endpoint(request, body):
        if guard:
            _guarded(request)
        return await _call(func, body.model_dump())

    # Set explicitly: under `from __future__ import annotations` an inline
    # annotation is the string "body_model", which FastAPI cannot resolve.
    endpoint.__annotations__ = {"request": Request, "body": body_model}
    router.add_api_route(path, endpoint, methods=["POST"], summary=summary, tags=[tag], name=path)
    return "POST"


# ── Walking ──────────────────────────────────────────────────────────────────


def _is_route_module(module: ModuleType) -> bool:
    return isinstance(getattr(module, "router", None), APIRouter) or isinstance(
        getattr(module, "absolute_router", None), APIRouter
    )


def _modules_under(folder: str, folders: dict[str, bool]):
    """Yield ``(folder, module)`` for the package at ``folder`` and every
    enabled subpackage, in walk order: a folder's modules by
    ``(MOUNT_ORDER, name)``, then its subpackages by name. A folder without
    ``__init__.py`` is not a package and contributes nothing."""
    root = REPO_ROOT / folder
    if not (root / "__init__.py").is_file():
        logger.warning("autoroutes: %s is not a package (no __init__.py); skipped", folder)
        return
    package_name = folder.replace("/", ".")
    try:
        package = importlib.import_module(package_name)
    except Exception as exc:
        logger.warning("autoroutes: cannot import %s: %s", package_name, exc)
        return
    yield from _walk_package(folder, package, folders)


def _walk_package(folder: str, package: ModuleType, folders: dict[str, bool]):
    yield folder, package
    modules: list[ModuleType] = []
    subpackages: list[tuple[str, ModuleType]] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        child_folder = f"{folder}/{info.name}"
        if info.ispkg and not _enabled(child_folder, folders):
            continue
        qualified = f"{package.__name__}.{info.name}"
        try:
            module = importlib.import_module(qualified)
        except Exception as exc:
            logger.warning("autoroutes: cannot import %s: %s", qualified, exc)
            continue
        if info.ispkg:
            subpackages.append((child_folder, module))
        else:
            modules.append(module)
    modules.sort(key=lambda m: (getattr(m, "MOUNT_ORDER", 0), m.__name__))
    for module in modules:
        yield folder, module
    for child_folder, subpackage in subpackages:
        yield from _walk_package(child_folder, subpackage, folders)


def _mount_route_module(router: APIRouter, folder: str, module: ModuleType, table: list[dict]) -> None:
    own = getattr(module, "router", None)
    if isinstance(own, APIRouter):
        prefix = url_for_folder(folder)
        router.include_router(own, prefix=prefix)
        for route in own.routes:
            for method in sorted(getattr(route, "methods", None) or []):
                table.append({"method": method, "path": prefix + route.path, "folder": folder, "module": module.__name__})
    absolute = getattr(module, "absolute_router", None)
    if isinstance(absolute, APIRouter):
        router.include_router(absolute)
        for route in absolute.routes:
            for method in sorted(getattr(route, "methods", None) or []):
                table.append({"method": method, "path": route.path, "folder": folder, "module": module.__name__})


def _mount_plain_module(router: APIRouter, folder: str, module: ModuleType, *, guard: bool, table: list[dict], seen: set[str]) -> None:
    if hasattr(module, "__path__"):
        prefix = url_for_folder(folder)
    else:
        prefix = url_for_folder(folder) + "/" + module.__name__.rsplit(".", 1)[-1].replace("_", "-")
    for name, func in _public_functions(module):
        path = f"{prefix}/{name}"
        if path in seen:
            continue
        method = _add_generated(router, path, func, guard=guard, tag=folder)
        if method is None:
            continue
        seen.add(path)
        table.append({"method": method, "path": path, "folder": folder, "module": module.__name__, "function": name})


def build_router(config: dict | None = None) -> tuple[APIRouter, list[dict]]:
    """The generated router plus its route table
    (``[{method, path, folder, module, function?}]``), for logs and tooling."""
    config = load_config() if config is None else config
    guard = bool(config.get("guard", True))
    folders = {_normalize(k): bool(v) for k, v in config.get("folders", {}).items()}
    router = APIRouter()
    table: list[dict] = []
    seen: set[str] = set()
    for folder in enabled_folders(config):
        for module_folder, module in _modules_under(folder, folders):
            if _is_route_module(module):
                _mount_route_module(router, module_folder, module, table)
            else:
                _mount_plain_module(router, module_folder, module, guard=guard, table=table, seen=seen)
    return router, table


def mount(app) -> list[dict]:
    """Build from the config file and mount; returns the route table."""
    router, table = build_router()
    if table:
        app.include_router(router)
    return table


def mount_module(app, module: ModuleType) -> None:
    """Mount one route module the way the loader would (its folder as the
    prefix, ``absolute_router`` as is). For tests that exercise a single
    module against a bare ``FastAPI()``."""
    router = APIRouter()
    _mount_route_module(router, folder_of(module), module, [])
    app.include_router(router)

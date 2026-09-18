"""Folder-based routes generated at boot from ``config/autoroutes.json``.

Any package in the repo can become part of the API without a route module:
list its folder in the config and every public module-level function in it
is served at its own path. ``services/inbox/service.py::list_items`` becomes
``POST /services/inbox/service/list_items``; a function in a folder's
``__init__.py`` sits at the folder path itself. Zero-parameter functions are
``GET``; every other function is ``POST`` with a JSON body whose fields are
the function's parameters (typed from the annotations, defaults kept).

Nothing is exposed until a folder is switched on, and a ``false`` entry
switches a subtree off under a ``true`` parent (longest path wins):

    {
      "guard": true,
      "folders": {
        "services/inbox": true,
        "services/inbox/store": false
      }
    }

Generated routes can run whatever the folder does, so with ``guard`` on
(the default) they answer only loopback callers whose Origin passes
:func:`routers.browser_guard.is_local_mutation`. ``server.py`` mounts the
generated router after every hand-written one, so on a path clash the
hand-written route wins.

Coroutine functions are awaited; sync functions run in the threadpool.
Return values go through ``jsonable_encoder``; a
:class:`services.errors.ServiceError` becomes the usual ``{code, message}``
error. Functions taking ``*args``/``**kwargs`` cannot be modelled and are
skipped with a log line, as is a module that fails to import.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import pkgutil
from functools import partial
from pathlib import Path
from typing import Any, Callable, get_type_hints

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import create_model
from starlette.concurrency import run_in_threadpool

from routers.browser_guard import is_local_mutation
from routers.cowork_agent.bff.errors import http_error
from services.errors import ServiceError

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "config" / "autoroutes.json"


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


def _public_functions(module) -> list[tuple[str, Callable]]:
    names = getattr(module, "__all__", None)
    if names is None:
        names = [n for n in vars(module) if not n.startswith("_")]
    found = []
    for name in names:
        obj = getattr(module, name, None)
        if (
            inspect.isfunction(obj)
            and getattr(obj, "__module__", None) == module.__name__
        ):
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


def _add_route(router: APIRouter, path: str, func: Callable, *, guard: bool, tag: str) -> str | None:
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


def _modules_under(folder: str, folders: dict[str, bool]):
    """Yield ``(url_prefix, module)`` for the package at ``folder`` and every
    enabled subpackage. A folder without ``__init__.py`` is not a package and
    contributes nothing."""
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
    yield f"/{folder}", package
    for info in pkgutil.walk_packages(package.__path__, prefix=package_name + "."):
        relative = info.name[len(package_name) + 1:].replace(".", "/")
        module_folder = f"{folder}/{relative}" if info.ispkg else f"{folder}/{relative}".rsplit("/", 1)[0]
        if not _enabled(module_folder, folders):
            continue
        if not info.ispkg and relative.rsplit("/", 1)[-1].startswith("_"):
            continue
        try:
            module = importlib.import_module(info.name)
        except Exception as exc:
            logger.warning("autoroutes: cannot import %s: %s", info.name, exc)
            continue
        yield f"/{folder}/{relative}", module


def build_router(config: dict | None = None) -> tuple[APIRouter, list[dict]]:
    """The generated router plus its route table
    (``[{method, path, folder, function}]``), for logs and tooling."""
    config = load_config() if config is None else config
    guard = bool(config.get("guard", True))
    folders = {_normalize(k): bool(v) for k, v in config.get("folders", {}).items()}
    router = APIRouter()
    table: list[dict] = []
    seen: set[str] = set()
    for folder in enabled_folders(config):
        for prefix, module in _modules_under(folder, folders):
            for name, func in _public_functions(module):
                path = f"{prefix}/{name}"
                if path in seen:
                    continue
                method = _add_route(router, path, func, guard=guard, tag=folder)
                if method is None:
                    continue
                seen.add(path)
                table.append({"method": method, "path": path, "folder": folder, "function": f"{module.__name__}.{name}"})
    return router, table


def mount(app) -> list[dict]:
    """Build from the config file and mount; returns the route table."""
    router, table = build_router()
    if table:
        app.include_router(router)
    return table

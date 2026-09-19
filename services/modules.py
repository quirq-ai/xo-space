"""The module registry: every folder under ``modules/`` with a ``module.json``.

A module declares what it exposes in its manifest; fixed file names
implement each kind; this registry discovers both and checks that they
agree, the way ``adapters/loader.py`` discovers an agent's capabilities.

::

    modules/<name>/
      module.json      the manifest (services/schema/module.schema.json)
      store.py         FILES
      events.py        TYPES, SIGNALS
      service.py       the only surface other modules call
      routes.py        router          kind "api"
      stream.py        STREAMS         kind "stream"
      tasks.py         TASKS           kind "tasks"
      listeners.py     LISTENERS       kind "listeners"
      commands.py      COMMANDS        kind "commands"
      pages/*.json     page specs      kind "pages"

Switches: a manifest gives each kind (and each task or page) its default;
``settings/modules.json`` holds the person's overrides; the effective state
is the merge, kept in memory and re-read when that file changes.
:func:`enabled` answers a switch; :func:`gate` is the dependency the app
attaches to a module's routes; :func:`override` writes a change and
reconciles the supervisor. A switch gates what a module exposes, never
what it stores or what another module reads through its ``service``.

This module imports no FastAPI and no module code at load time.
"""

from __future__ import annotations

import copy
import importlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from services.errors import NotFound, ServiceError
from services.storage.document import Document
from services.storage.files import File
from services.storage.layout import settings_dir
from services.supervisor import Task, TaskSpec

logger = logging.getLogger("xo_space.modules")

REPO = Path(__file__).resolve().parents[1]
#: Tests patch these two together to point the registry at a temporary tree.
MODULES_DIR = REPO / "modules"
MODULES_PACKAGE = "modules"
SCHEMA_DIR = REPO / "services" / "schema"

KINDS = ("api", "stream", "tasks", "listeners", "commands", "pages")
#: The file each kind is implemented in (``pages`` is a folder of specs).
CONTRACT = {"api": "routes", "stream": "stream", "tasks": "tasks",
            "listeners": "listeners", "commands": "commands", "pages": "pages"}
EXPORTS = {"api": "router", "stream": "STREAMS", "tasks": "TASKS",
           "listeners": "LISTENERS", "commands": "COMMANDS"}
#: Kinds whose switch is per item.
ITEMIZED = ("tasks", "pages")

MANIFEST = "module.json"
UI_FILE = "ui.json"


# ── The manifest ─────────────────────────────────────────────────────────────


def _switch(value: Any, *, itemized: bool) -> dict:
    """Normalise one capability's manifest value to
    ``{"enabled": bool, "settings": {...}, "items": {name: {"enabled", "settings"}}}``."""
    if isinstance(value, bool):
        return {"enabled": value, "settings": {}, "items": {}}
    if not isinstance(value, dict):
        return {"enabled": False, "settings": {}, "items": {}}
    if itemized:
        items = {}
        for item, raw in value.items():
            if item == "enabled":
                continue
            items[item] = _switch(raw, itemized=False)
            items[item].pop("items", None)
        return {"enabled": bool(value.get("enabled", True)), "settings": {}, "items": items}
    settings = {k: v for k, v in value.items() if k != "enabled"}
    return {"enabled": bool(value.get("enabled", True)), "settings": settings, "items": {}}


@dataclass(frozen=True)
class Module:
    name: str
    title: str
    description: str
    folder: Optional[str]
    depends: tuple[str, ...]
    aliases: tuple[str, ...]
    enabled_default: bool
    manifest: dict
    path: Path
    #: kind -> normalised default switch; only declared kinds are present.
    caps: dict = field(default_factory=dict, compare=False)

    def declares(self, kind: str) -> bool:
        return kind in self.caps

    @property
    def package(self) -> str:
        return f"{MODULES_PACKAGE}.{self.name}"


def _load_manifest(path: Path) -> Module:
    raw = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path / MANIFEST}: not an object")
    name = raw.get("name")
    if name != path.name:
        raise ValueError(f"{path / MANIFEST}: name {name!r} must equal the folder name {path.name!r}")
    if raw.get("schema") != 1:
        raise ValueError(f"{path / MANIFEST}: schema must be 1")
    caps = {}
    for kind in KINDS:
        value = raw.get(kind)
        if value is None or value is False:
            continue
        caps[kind] = _switch(value, itemized=kind in ITEMIZED)
    return Module(
        name=name,
        title=str(raw.get("title") or name),
        description=str(raw.get("description") or ""),
        folder=raw.get("folder"),
        depends=tuple(raw.get("depends") or ()),
        aliases=tuple(raw.get("aliases") or ()),
        enabled_default=bool(raw.get("enabled", True)),
        manifest=raw,
        path=path,
        caps=caps,
    )


_modules_cache: Optional[list[Module]] = None


def modules() -> list[Module]:
    """Every module, by name. A folder whose manifest does not load is
    logged and skipped (the tests fail on it; the server keeps booting)."""
    global _modules_cache
    if _modules_cache is not None:
        return _modules_cache
    found: list[Module] = []
    if MODULES_DIR.is_dir():
        for entry in sorted(MODULES_DIR.iterdir()):
            if not entry.is_dir() or not (entry / MANIFEST).is_file():
                continue
            try:
                found.append(_load_manifest(entry))
            except Exception:  # noqa: BLE001 - one bad manifest must not hide the others
                logger.exception("modules: %s has an unreadable %s; skipped", entry.name, MANIFEST)
    _modules_cache = found
    return found


def names() -> list[str]:
    return [m.name for m in modules()]


def get(name: str) -> Module:
    for m in modules():
        if m.name == name:
            return m
    raise NotFound("unknown_module", f"Unknown module {name!r}.")


def find(name: str) -> Optional[Module]:
    return next((m for m in modules() if m.name == name), None)


def reset_for_tests() -> None:
    """Forget the discovered modules, the pages and the switch cache."""
    global _modules_cache, _pages_cache, _overrides_cache
    _modules_cache = None
    _pages_cache = {}
    _overrides_cache = None


# ── Capabilities ─────────────────────────────────────────────────────────────


def capability(name: str, kind: str) -> Optional[Any]:
    """Import ``modules.<name>.<file>`` for ``kind`` when the manifest
    declares it; ``None`` when it does not. A declared kind whose file is
    missing raises ``ModuleNotFoundError`` (a broken module, never a
    silent "unsupported"); an import error inside the file propagates."""
    module = get(name)
    if kind not in CONTRACT:
        raise ValueError(f"unknown capability kind {kind!r}")
    if not module.declares(kind):
        return None
    if kind == "pages":
        return module.path / "pages"
    target = f"{module.package}.{CONTRACT[kind]}"
    try:
        return importlib.import_module(target)
    except ModuleNotFoundError as exc:
        if exc.name in {target, module.package}:
            raise ModuleNotFoundError(
                f"module {name!r} declares {kind!r} in {MANIFEST} but has no "
                f"{CONTRACT[kind]}.py (expected {target})", name=target) from exc
        raise


def implements(name: str, kind: str) -> bool:
    """Whether the file for ``kind`` exists, declared or not."""
    module = get(name)
    if kind == "pages":
        folder = module.path / "pages"
        return folder.is_dir() and any(folder.glob("*.json"))
    return (module.path / f"{CONTRACT[kind]}.py").is_file()


def _export(name: str, kind: str, default: Any) -> Any:
    mod = capability(name, kind)
    if mod is None:
        return default
    value = getattr(mod, EXPORTS[kind], None)
    if value is None:
        raise AttributeError(f"modules/{name}/{CONTRACT[kind]}.py must define {EXPORTS[kind]}")
    return value


def routers() -> list[tuple[Module, Any]]:
    """``(module, router)`` for every module declaring ``api``, by name. A
    module whose routes fail to import is logged and skipped."""
    out = []
    for module in modules():
        if not module.declares("api"):
            continue
        try:
            out.append((module, _export(module.name, "api", None)))
        except Exception:  # noqa: BLE001 - a broken module must not stop the boot
            logger.exception("modules: %s routes failed to load; not mounted", module.name)
    return out


def tasks() -> list[TaskSpec]:
    out: list[TaskSpec] = []
    for module in modules():
        if not module.declares("tasks"):
            continue
        try:
            declared = _export(module.name, "tasks", [])
        except Exception:  # noqa: BLE001
            logger.exception("modules: %s tasks failed to load; none started", module.name)
            continue
        for task in declared:
            if isinstance(task, Task):
                out.append(TaskSpec(module.name, task))
    return out


def streams() -> list[tuple[Module, str, Callable[..., Any]]]:
    out = []
    for module in modules():
        if not module.declares("stream"):
            continue
        try:
            declared = _export(module.name, "stream", {})
        except Exception:  # noqa: BLE001
            logger.exception("modules: %s streams failed to load; none mounted", module.name)
            continue
        for stream_name, fn in dict(declared).items():
            out.append((module, str(stream_name), fn))
    return out


def listeners() -> dict[str, list[tuple[str, Callable[..., Any]]]]:
    """signal -> ``[(module, fn)]`` over every module declaring listeners."""
    out: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
    for module in modules():
        if not module.declares("listeners"):
            continue
        try:
            declared = _export(module.name, "listeners", {})
        except Exception:  # noqa: BLE001
            logger.exception("modules: %s listeners failed to load; none registered", module.name)
            continue
        for signal, fn in dict(declared).items():
            out.setdefault(str(signal), []).append((module.name, fn))
    return out


def commands() -> dict[str, dict[str, Callable[..., Any]]]:
    out: dict[str, dict[str, Callable[..., Any]]] = {}
    for module in modules():
        if not module.declares("commands"):
            continue
        try:
            out[module.name] = dict(_export(module.name, "commands", {}))
        except Exception:  # noqa: BLE001
            logger.exception("modules: %s commands failed to load", module.name)
    return out


def files() -> list[tuple[str, File]]:
    """``(module, File)`` for every declared file, over every module's
    ``store.py`` (a module without one declares nothing)."""
    out: list[tuple[str, File]] = []
    for module in modules():
        if not (module.path / "store.py").is_file():
            continue
        try:
            store = importlib.import_module(f"{module.package}.store")
        except Exception:  # noqa: BLE001
            logger.exception("modules: %s store failed to load", module.name)
            continue
        for spec in getattr(store, "FILES", ()) or ():
            if isinstance(spec, File):
                out.append((module.name, spec))
    return out


def _events_module(module: Module) -> Optional[Any]:
    if not (module.path / "events.py").is_file():
        return None
    return importlib.import_module(f"{module.package}.events")


def event_types() -> dict[str, str]:
    """event type -> the module that declares it (``events.TYPES``)."""
    out: dict[str, str] = {}
    for module in modules():
        events = _events_module(module)
        for event_type in getattr(events, "TYPES", ()) or () if events else ():
            if event_type in out and out[event_type] != module.name:
                logger.error("modules: event type %r declared by both %s and %s", event_type, out[event_type], module.name)
            out.setdefault(str(event_type), module.name)
    return out


def signals_declared() -> set[str]:
    """Every ``<module>.<signal>`` some module's ``events.SIGNALS`` names."""
    out: set[str] = set()
    for module in modules():
        events = _events_module(module)
        for signal in getattr(events, "SIGNALS", ()) or () if events else ():
            out.add(f"{module.name}.{signal}")
    return out


# ── Pages ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Page:
    module: str
    id: str
    spec: dict
    path: Path

    @property
    def route(self) -> str:
        return str(self.spec.get("route") or f"{self.module}/{self.id}")

    @property
    def tab(self) -> str:
        return str(self.spec.get("tab") or "")

    @property
    def order(self) -> int:
        return int(self.spec.get("order") or 0)


_pages_cache: dict[str, list[Page]] = {}


def pages(name: Optional[str] = None) -> list[Page]:
    """Every page spec a module ships (declared ``pages`` or not: the test
    that manifests and files agree uses this too)."""
    out: list[Page] = []
    for module in modules():
        if name is not None and module.name != name:
            continue
        if module.name not in _pages_cache:
            found: list[Page] = []
            folder = module.path / "pages"
            if folder.is_dir():
                for path in sorted(folder.glob("*.json")):
                    try:
                        spec = json.loads(path.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001
                        logger.exception("modules: %s/pages/%s is not JSON; skipped", module.name, path.name)
                        continue
                    if isinstance(spec, dict):
                        found.append(Page(module.name, str(spec.get("id") or path.stem), spec, path))
            _pages_cache[module.name] = found
        out.extend(_pages_cache[module.name])
    return out


def tabs() -> list[dict]:
    path = MODULES_DIR / UI_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("modules: %s unreadable; no tabs", path)
        return []
    return [dict(t) for t in raw.get("tabs", []) if isinstance(t, dict) and t.get("id")]


def ui() -> dict:
    """What the shell renders: the tabs, in order, and every page of every
    enabled module whose page switch is on, with its spec inline."""
    tab_order = {t["id"]: i for i, t in enumerate(tabs())}
    out = []
    for page in pages():
        if not enabled(page.module, "pages", page.id):
            continue
        out.append({
            "module": page.module, "id": page.id, "route": page.route, "tab": page.tab,
            "label": page.spec.get("label") or page.id, "order": page.order,
            "aliases": list(page.spec.get("aliases") or []), "spec": page.spec,
        })
    out.sort(key=lambda p: (tab_order.get(p["tab"], 99), p["order"], p["route"]))
    return {"schema": 1, "tabs": tabs(), "pages": out}


# ── Switches ─────────────────────────────────────────────────────────────────


def settings_path() -> Path:
    return settings_dir() / "modules.json"


def _settings_document() -> Document:
    return Document(settings_path(), schema=1, empty=lambda: {"modules": {}},
                    normalize=_normalize_settings, name="modules.json")


def _normalize_settings(doc: dict) -> dict:
    if not isinstance(doc.get("modules"), dict):
        doc["modules"] = {}
    return doc


_overrides_cache: Optional[tuple[tuple[int, int], dict]] = None


def overrides() -> dict:
    """The person's overrides, re-read when the file changes on disk."""
    global _overrides_cache
    path = settings_path()
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (0, 0)
    if _overrides_cache is not None and _overrides_cache[0] == stamp:
        return _overrides_cache[1]
    doc, ok = _settings_document().read()
    if not ok:
        logger.warning("modules: %s is not readable; no overrides apply", path)
    overrides_by_module = {k: v for k, v in doc.get("modules", {}).items() if isinstance(v, dict)}
    _overrides_cache = (stamp, overrides_by_module)
    return overrides_by_module


def _items_known(module: Module, kind: str) -> set[str]:
    if kind == "tasks":
        try:
            return {spec.task.name for spec in tasks() if spec.module == module.name}
        except Exception:  # noqa: BLE001
            return set()
    if kind == "pages":
        return {page.id for page in pages(module.name)}
    return set()


def _merge_switch(base: dict, patch: Any, *, itemized: bool) -> dict:
    """``base`` (normalised) with ``patch`` (manifest-shaped) on top."""
    out = copy.deepcopy(base)
    if isinstance(patch, bool):
        out["enabled"] = patch
        return out
    if not isinstance(patch, dict):
        return out
    if "enabled" in patch:
        out["enabled"] = bool(patch["enabled"])
    for key, value in patch.items():
        if key == "enabled":
            continue
        if itemized:
            item = out["items"].setdefault(key, {"enabled": True, "settings": {}})
            merged = _merge_switch(item, value, itemized=False)
            merged.pop("items", None)
            out["items"][key] = merged
        else:
            out["settings"][key] = value
    return out


def effective(name: str) -> dict:
    """The module's switches after overrides:
    ``{"enabled": bool, "caps": {kind: {"enabled", "settings", "items"}}}``."""
    module = get(name)
    patch = overrides().get(name, {})
    caps = {}
    for kind, base in module.caps.items():
        caps[kind] = _merge_switch(base, patch.get(kind), itemized=kind in ITEMIZED)
    return {"enabled": bool(patch.get("enabled", module.enabled_default)), "caps": caps}


def enabled(name: str, kind: Optional[str] = None, item: Optional[str] = None) -> bool:
    module = find(name)
    if module is None:
        return False
    state = effective(name)
    if not state["enabled"]:
        return False
    if kind is None:
        return True
    cap = state["caps"].get(kind)
    if cap is None or not cap["enabled"]:
        return False
    if item is None:
        return True
    return bool(cap["items"].get(item, {"enabled": True})["enabled"])


def settings(name: str, kind: str, item: Optional[str] = None) -> dict:
    """The merged settings of a capability (or of one task or page)."""
    cap = effective(name)["caps"].get(kind)
    if cap is None:
        return {}
    if item is None:
        return dict(cap["settings"])
    entry = cap["items"].get(item)
    return dict(entry["settings"]) if entry else {}


def _validate_patch(module: Module, patch: Any) -> dict:
    if not isinstance(patch, dict):
        raise ServiceError("invalid_value", "The body must be an object of switches.")
    clean: dict = {}
    for key, value in patch.items():
        if key == "enabled":
            if not isinstance(value, bool):
                raise ServiceError("invalid_value", "enabled must be true or false.")
            clean["enabled"] = value
            continue
        if key not in KINDS:
            raise ServiceError("invalid_value", f"Unknown switch {key!r}; expected one of enabled, {', '.join(KINDS)}.")
        if not module.declares(key):
            raise ServiceError("invalid_value", f"{module.title} has no {key}.")
        if isinstance(value, bool):
            clean[key] = value
            continue
        if not isinstance(value, dict):
            raise ServiceError("invalid_value", f"{key} must be true, false or an object.")
        entry: dict = {}
        for sub, sub_value in value.items():
            if sub == "enabled":
                if not isinstance(sub_value, bool):
                    raise ServiceError("invalid_value", f"{key}.enabled must be true or false.")
                entry["enabled"] = sub_value
            elif key in ITEMIZED:
                if sub not in _items_known(module, key):
                    raise ServiceError("invalid_value", f"{module.title} has no {key[:-1]} {sub!r}.")
                if isinstance(sub_value, bool):
                    entry[sub] = sub_value
                elif isinstance(sub_value, dict):
                    if "enabled" in sub_value and not isinstance(sub_value["enabled"], bool):
                        raise ServiceError("invalid_value", f"{key}.{sub}.enabled must be true or false.")
                    entry[sub] = {k: v for k, v in sub_value.items() if isinstance(v, (bool, int, float, str, type(None)))}
                else:
                    raise ServiceError("invalid_value", f"{key}.{sub} must be true, false or an object.")
            else:
                if not isinstance(sub_value, (bool, int, float, str, type(None))):
                    raise ServiceError("invalid_value", f"{key}.{sub} must be a scalar.")
                entry[sub] = sub_value
        clean[key] = entry
    return clean


def override(name: str, patch: Any) -> dict:
    """Merge ``patch`` into the person's overrides for ``name``, write the
    file, refresh the cache. Returns :func:`describe_one`. The caller
    reconciles the supervisor (``routers/kernel.py`` does)."""
    module = get(name)
    clean = _validate_patch(module, patch)

    def apply(doc: dict) -> bool:
        current = doc["modules"].setdefault(name, {})
        before = copy.deepcopy(current)
        for key, value in clean.items():
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                for sub, sub_value in value.items():
                    if isinstance(sub_value, dict) and isinstance(current[key].get(sub), dict):
                        current[key][sub].update(sub_value)
                    else:
                        current[key][sub] = sub_value
            else:
                current[key] = value
        return current != before

    _settings_document().modify(apply)
    global _overrides_cache
    _overrides_cache = None
    logger.info("modules: %s switches changed: %s", name, json.dumps(clean, sort_keys=True))
    return describe_one(name)


# ── Description (GET /api/modules) ───────────────────────────────────────────


def describe_one(name: str) -> dict:
    from services.supervisor import supervisor

    module = get(name)
    state = effective(name)
    caps: dict = {}
    for kind, cap in state["caps"].items():
        entry = {"enabled": cap["enabled"], "settings": cap["settings"], "implemented": implements(name, kind)}
        if kind in ITEMIZED:
            known = sorted(_items_known(module, kind))
            items = {}
            for item in known:
                item_state = cap["items"].get(item, {"enabled": True, "settings": {}})
                row = {"enabled": item_state["enabled"], "settings": item_state["settings"]}
                if kind == "tasks":
                    row["status"] = supervisor.status(f"{name}.{item}")
                    spec = next((s for s in tasks() if s.module == name and s.task.name == item), None)
                    row["description"] = spec.task.description if spec else ""
                items[item] = row
            entry["items"] = items
        caps[kind] = entry
    warnings = [f"depends on {dep}, which is off" for dep in module.depends
                if find(dep) is not None and not enabled(dep)]
    warnings += [f"depends on {dep}, which is not installed" for dep in module.depends if find(dep) is None]
    return {
        "name": name, "title": module.title, "description": module.description,
        "folder": module.folder, "depends": list(module.depends), "aliases": list(module.aliases),
        "enabled": state["enabled"], "capabilities": caps, "warnings": warnings,
    }


def describe() -> dict:
    return {"schema": 1, "settings_file": str(settings_path()),
            "modules": [describe_one(m.name) for m in modules()]}


# ── The gate ─────────────────────────────────────────────────────────────────


def gate(name: str, kind: str) -> Callable[[], Any]:
    """A FastAPI dependency: raises 404 ``module_disabled`` while the
    module or the capability is off. Attached by ``server.py`` when
    mounting; a module never adds it itself."""
    title = get(name).title

    async def _gate() -> None:
        if not enabled(name, kind):
            raise NotFound("module_disabled", f"{title} is off. Turn it on in Setup, under Modules.")

    _gate.__name__ = f"gate_{name}_{kind}"
    return _gate

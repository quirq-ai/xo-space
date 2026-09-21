"""A fact and the work item it becomes (docs/work-and-workitems.md section 18).

A fact is what a feeder or ``POST /api/inbox`` hands the Inbox: a title, a
body, a kind, a producer timestamp, an optional url and in-Space link, the
project it names (if any), the section it belongs to and the entity inside
that section (the toolkit, the repo, the project, the agent), and the work
item ``source`` block that carries its dedup key. :func:`ingest` turns it
into a work item in the right project through the existing store
(``services/cowork_agent/visualizer/workitems_store.py``), idempotent on
the key, and writes the fact beside the project's claims file, under the
runtime root, so a mail body never lands in ``workitems.json``, a project
file the relay may share::

    ~/.quirq/projects/<pid>/workitems/<workitem-id>/fact.json

The runner's sidecars (``session.json``, ``outcome.json``) live in the same
folder (:mod:`services.work.items`). ``sidecar_dir`` is defined here so the
two packages agree on it; the dependency points work -> inbox, as it always
has.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from services.cowork_agent import project_layout
from services.cowork_agent.scopes import VisualizerScope
from services.cowork_agent.visualizer import workitems_store
from services.cowork_agent.visualizer.workspace_index import list_project_pids
from services.storage.atomic_write import write_json_atomic
from services.storage.reader import read_json
from services.timestamps import now_iso, parse_ts

from .ledger import InboxError

logger = logging.getLogger(__name__)

SCHEMA = 1
SECTIONS = ("connections", "projects", "issues", "agents")
SECTION_LABELS = {"connections": "Connections", "projects": "Projects", "issues": "Issues", "agents": "Agents"}
#: A work item's section follows its source kind: it decides which policy
#: governs its sessions and where a fact without a project runs.
SECTION_OF_KIND = {"connection": "connections", "sharing": "projects", "github": "issues", "post": "agents", "local": "agents"}
PROJECT_PREFIX = "inbox-"
RUNTIME = "inbox"
TITLE_MAX, BODY_MAX, URL_MAX, PATH_MAX = 300, 4000, 2000, 500
KIND_RE = re.compile(r"[a-z0-9_.:-]{1,60}")
VIEW_RE = re.compile(r"[a-z0-9_-]{1,40}")
PROJECT_ID_RE = re.compile(r"[A-Za-z0-9_:\-\.]{1,200}")
ENTITY_RE = re.compile(r"[^\s\x00-\x1f\x7f]{1,200}")
WORKITEM_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


# ── Names ───────────────────────────────────────────────────────────────────


def section_of_kind(kind) -> str:
    return SECTION_OF_KIND.get(kind if isinstance(kind, str) else "", "agents")


def check_section(value) -> str:
    if not isinstance(value, str) or value not in SECTIONS:
        raise InboxError("invalid_section", f"section must be one of {list(SECTIONS)}.", 404)
    return value


def check_project_id(value) -> str:
    if not is_project_id(value):
        raise InboxError("invalid_project_id", "project_id must be a project folder name.")
    return value


def check_workitem_id(value) -> str:
    if not isinstance(value, str) or WORKITEM_ID_RE.fullmatch(value) is None:
        raise InboxError("workitem_not_found", "No such work item.", 404)
    return value


def is_project_id(value) -> bool:
    return isinstance(value, str) and PROJECT_ID_RE.fullmatch(value) is not None and ".." not in value.split("/")


def is_url(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= URL_MAX and value.lower().startswith(("http://", "https://")) \
        and not any(ch.isspace() for ch in value)


def is_link_path(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= PATH_MAX and not value.startswith("/") \
        and "\\" not in value and ".." not in value.split("/")


def validate_link(link, *, strict: bool = False) -> Optional[dict]:
    """``{view?, project?, path?}``: a view id, a project folder name and a
    relative path inside it. Strict raises on a bad value; lenient drops it."""
    if link is None:
        return None
    if not isinstance(link, dict):
        if strict:
            raise InboxError("invalid_link", "link must be an object.")
        return None
    out: dict = {}
    if "view" in link:
        view = link["view"]
        if isinstance(view, str) and VIEW_RE.fullmatch(view):
            out["view"] = view
        elif strict:
            raise InboxError("invalid_link", "link.view must be a view id.")
    if "project" in link:
        project = link["project"]
        if is_project_id(project):
            out["project"] = project
        elif strict:
            raise InboxError("invalid_link", "link.project must be a project folder name.")
    if "path" in link:
        path = link["path"]
        if is_link_path(path) and "project" in out:
            out["path"] = path
        elif strict:
            raise InboxError("invalid_link", "link.path must be a relative path inside link.project.")
    unknown = sorted(set(link) - {"view", "project", "path"})
    if unknown and strict:
        raise InboxError("invalid_link", f"link carries unknown key(s) {unknown}.")
    return out or None


def project_id_for(section: str) -> str:
    """The section's own project, where facts that name no project run."""
    return PROJECT_PREFIX + check_section(section)


# ── The fact ────────────────────────────────────────────────────────────────


def build_fact(*, title, body="", kind="note", source, section: str, entity=None, project_id=None, link=None,
               url=None, ts=None) -> dict:
    """Validate and shape a fact. ``source`` is the work item source block
    (``{"kind": ..., "key": ..., <kind block>}``); the store validates it
    again when the work item is written. Raises ``InboxError``."""
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_MAX:
        raise InboxError("invalid_value", f"title is required (1 to {TITLE_MAX} chars).")
    if not isinstance(body, str) or len(body) > BODY_MAX:
        raise InboxError("invalid_value", f"body must be a string of at most {BODY_MAX} chars.")
    if not isinstance(kind, str) or not KIND_RE.fullmatch(kind):
        raise InboxError("invalid_value", "kind must match [a-z0-9_.:-] (1 to 60 chars).")
    if not isinstance(source, dict) or source.get("kind") not in workitems_store.VALID_SOURCE_KINDS:
        raise InboxError("invalid_value", f"source.kind must be one of {sorted(workitems_store.VALID_SOURCE_KINDS)}.")
    check_section(section)
    if project_id is not None and not is_project_id(project_id):
        raise InboxError("invalid_project_id", "project_id must be a project folder name.")
    if url is not None and not is_url(url):
        raise InboxError("invalid_value", f"url must start with http:// or https:// and be at most {URL_MAX} chars.")
    if entity is not None and (not isinstance(entity, str) or not ENTITY_RE.fullmatch(entity)):
        raise InboxError("invalid_value", "entity must be a short one-line name.")
    if ts is not None and (not isinstance(ts, str) or parse_ts(ts) is None):
        raise InboxError("invalid_value", "ts must be an ISO-8601 timestamp.")
    return {"schema": SCHEMA, "title": " ".join(title.split())[:TITLE_MAX], "body": body, "kind": kind,
            "ts": ts or now_iso(), "url": url, "link": validate_link(link, strict=True), "project_id": project_id,
            "section": section, "entity": entity, "source": dict(source)}


def fact_entity(fact: dict) -> Optional[str]:
    """The entity the fact groups under, from the fact or its source block."""
    if isinstance(fact.get("entity"), str) and fact["entity"]:
        return fact["entity"]
    source = fact.get("source") if isinstance(fact.get("source"), dict) else {}
    kind = source.get("kind")
    block = source.get(kind) if isinstance(source.get(kind), dict) else {}
    if kind == "connection":
        return block.get("toolkit") or None
    if kind == "sharing":
        return block.get("repo") or None
    if kind == "github":
        return block.get("repo") or None
    if kind == "post":
        return block.get("agent") or None
    return None


# ── Where the work item goes ────────────────────────────────────────────────


def target_project(fact: dict) -> str:
    """The fact's own project when it names one that exists, else the
    section's project (made on first use by :func:`ensure_project`)."""
    named = fact.get("project_id")
    if is_project_id(named) and named in list_project_pids():
        return named
    return project_id_for(fact["section"])


def ensure_project(project_id: str) -> str:
    """Scaffold a section project the first time a fact lands in it. A
    named project that is missing is never made here: the caller fell
    back to the section project already."""
    if project_layout.load_project(project_id) is None:
        if not project_id.startswith(PROJECT_PREFIX):
            raise InboxError("project_not_found", f"project {project_id} does not exist.", 404)
        section = project_id[len(PROJECT_PREFIX):]
        project_layout.scaffold_project(
            project_id, display_name=f"Inbox: {section}",
            description=f"Work items of the Inbox's {section} section that name no project of their own, and the sessions that handle them.")
        logger.info("inbox: scaffolded %s", project_id)
    return project_id


# ── The sidecar folder ──────────────────────────────────────────────────────


def sidecar_dir(project_id: str, workitem_id: str, *, create: bool = False) -> Optional[Path]:
    """``~/.quirq/projects/<pid>/workitems/<workitem-id>/``, or ``None`` for a
    project with no runtime home (one that does not exist)."""
    root = project_layout.runtime_dir_for_project(project_id, create=create)
    if root is None:
        return None
    return root / "workitems" / check_workitem_id(workitem_id)


def fact_path(project_id: str, workitem_id: str) -> Optional[Path]:
    folder = sidecar_dir(project_id, workitem_id)
    return folder / "fact.json" if folder is not None else None


def read_fact(project_id: str, workitem_id: str) -> Optional[dict]:
    path = fact_path(project_id, workitem_id)
    doc = read_json(path) if path is not None else None
    return doc if isinstance(doc, dict) else None


def write_fact(project_id: str, workitem_id: str, fact: dict) -> Optional[Path]:
    folder = sidecar_dir(project_id, workitem_id, create=True)
    if folder is None:
        logger.warning("inbox: %s has no runtime home; the fact for %s is not kept", project_id, workitem_id)
        return None
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "fact.json"
    write_json_atomic(path, fact)
    return path


# ── Ingest ──────────────────────────────────────────────────────────────────


def find_by_key(scope: VisualizerScope, key: str) -> Optional[dict]:
    """The project's work item carrying ``source.key``, if any."""
    for record in scope.list_workitems():
        if workitems_store.source_key(record) == key:
            return record
    return None


def ingest(fact: dict) -> tuple[dict, bool, str]:
    """The fact as a work item: ``(record, created, project_id)``. Idempotent
    on the source key (a GitHub issue on its node id); a fact seen again
    refreshes ``fact.json`` and returns the existing record."""
    project_id = ensure_project(target_project(fact))
    scope = VisualizerScope(project_id)
    source = fact["source"]
    labels = ["inbox", fact["section"]]
    try:
        if source.get("kind") == "github":
            record, created = scope.adopt_workitem(runtime=RUNTIME, github=source["github"], title=fact["title"], labels=labels)
        else:
            key = source.get("key") if isinstance(source.get("key"), str) else None
            existing = find_by_key(scope, key) if key else None
            if existing is not None:
                record, created = existing, False
            else:
                record = scope.create_workitem(runtime=RUNTIME, title=fact["title"], body=None, labels=labels, source=source)
                created = True
    except workitems_store.WorkitemsStoreError as exc:
        raise InboxError(exc.code, str(exc), 409 if exc.code in ("already_adopted", "corrupt_document") else 400) from exc
    previous = read_fact(project_id, record["id"]) or {}
    stored = {**fact, "workitem_id": record["id"], "project_id": project_id,
              "entity": fact_entity(fact), "ingested_at": previous.get("ingested_at") or now_iso()}
    write_fact(project_id, record["id"], stored)
    return record, created, project_id

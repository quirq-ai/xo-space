"""Categorized XO-project graph used by the Space Dashboard.

The ordinary Space graph expands projects into folders and files. This
projection answers a different question: what is each project for? It reuses
the bounded ``build_space_data`` scan, collapses every project to one node,
and classifies those nodes into the same five purpose categories used by the
newer UI.

The builder is read-only. A manual ``category`` in ``.xo/project.json`` (or
an existing watcher-produced ``classification`` block) takes precedence over
filename heuristics, so users can correct an ambiguous project without
renaming files.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath

from services.cowork_agent.project_layout import project_dir
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.space_index import build_space_data


CATEGORIES = (
    "engineering",
    "ops",
    "documentation",
    "research",
    "marketing",
)

_LABELS = {
    "engineering": "Engineering",
    "ops": "Ops",
    "documentation": "Documentation",
    "research": "Research",
    "marketing": "Marketing",
}
_COLORS = {
    "engineering": "#6fb7e0",
    "ops": "#e8a15c",
    "documentation": "#c792ea",
    "research": "#7fd0a8",
    "marketing": "#e0708a",
}
_DESCRIPTIONS = {
    "engineering": "Apps, services, and libraries that are built and shipped.",
    "ops": "Infrastructure-as-code and operational tooling.",
    "documentation": "Docs sites, wikis, notes, and one-pagers.",
    "research": "Papers, notebooks, studies, and exploratory work.",
    "marketing": "Decks, brand assets, proposals, and outward-facing material.",
}
_ALIASES = {
    "app": "engineering",
    "eng": "engineering",
    "docs": "documentation",
    "wiki": "documentation",
    "customer": "marketing",
}

_CODE_EXTENSIONS = {
    ".c",
    ".cpp",
    ".go",
    ".h",
    ".java",
    ".js",
    ".jsx",
    ".mjs",
    ".py",
    ".rs",
    ".sh",
    ".ts",
    ".tsx",
}
_WRITING_EXTENSIONS = {".md", ".mdx", ".pdf", ".rst", ".txt", ".docx"}
_RESEARCH_EXTENSIONS = {".bib", ".ipynb", ".rmd", ".tex"}
_SLIDE_EXTENSIONS = {".key", ".odp", ".pptx"}
_IMAGE_EXTENSIONS = {
    ".ai",
    ".bmp",
    ".eps",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".png",
    ".psd",
    ".svg",
    ".tiff",
    ".webp",
}
_APP_MANIFESTS = {
    "cargo.toml",
    "go.mod",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
}
_OPS_FILES = {
    "ansible.cfg",
    "chart.yaml",
    "chart.yml",
    "pulumi.yaml",
    "pulumi.yml",
}
_OPS_EXTENSIONS = {".tf", ".tfvars"}
_DOCS_SITE_FILES = {
    "mkdocs.yml",
    "mkdocs.yaml",
    "source.config.ts",
    "source.config.tsx",
}
_MARKETING_TOKENS = {
    "brand",
    "campaign",
    "contract",
    "customer",
    "deck",
    "invoice",
    "marketing",
    "msa",
    "pitch",
    "proposal",
    "sales",
    "slides",
    "sow",
    "statement-of-work",
}
_RESEARCH_TOKENS = {
    "experiment",
    "paper",
    "research",
    "study",
    "thesis",
    "whitepaper",
}
_DOCUMENTATION_TOKENS = {"docs", "documentation", "handbook", "notes", "wiki"}

def _normalized_category(value: object) -> str | None:
    raw = str(value or "").strip().lower()
    category = _ALIASES.get(raw, raw)
    return category if category in CATEGORIES else None


def _saved_memberships(project_id: str) -> list[str]:
    """Return valid saved categories, with a manual override first."""
    document = read_json(project_dir(project_id) / ".xo" / "project.json") or {}
    classification = document.get("classification")
    raw = []
    if isinstance(classification, dict):
        raw = classification.get("categories") or [
            classification.get("category")
        ]

    memberships: list[str] = []
    for value in raw:
        category = _normalized_category(value)
        if category and category not in memberships:
            memberships.append(category)

    manual = _normalized_category(document.get("category"))
    if manual:
        memberships = [manual] + [
            category for category in memberships if category != manual
        ]
    return memberships


def _contains_token(path: str, tokens: set[str]) -> bool:
    lowered = path.lower().replace("_", "-")
    parts = {
        part
        for segment in lowered.split("/")
        for part in segment.replace(".", "-").split("-")
        if part
    }
    return bool(parts & tokens) or any(token in lowered for token in tokens)


def classify_project(project_id: str, paths: list[str]) -> list[str]:
    """Classify one project; the first category is its primary cluster."""
    saved = _saved_memberships(project_id)

    normalized = [path.replace("\\", "/") for path in paths]
    names = [PurePosixPath(path).name.lower() for path in normalized]
    extensions = [PurePosixPath(name).suffix.lower() for name in names]
    search_paths = [project_id, *normalized]

    ops = any(
        name in _OPS_FILES or extension in _OPS_EXTENSIONS
        for name, extension in zip(names, extensions)
    ) or any(
        _contains_token(path, {"ansible", "helm", "pulumi", "terraform"})
        for path in search_paths
    )

    slide_signal = any(
        extension in _SLIDE_EXTENSIONS
        or _contains_token(path, {"deck", "slides"})
        for path, extension in zip(normalized, extensions)
    )
    image_count = sum(extension in _IMAGE_EXTENSIONS for extension in extensions)
    image_majority = bool(paths) and image_count / len(paths) >= 0.5
    marketing = (
        slide_signal
        or image_majority
        or any(_contains_token(path, _MARKETING_TOKENS) for path in search_paths)
    )

    research = any(
        extension in _RESEARCH_EXTENSIONS for extension in extensions
    ) or any(_contains_token(path, _RESEARCH_TOKENS) for path in search_paths)

    writing_count = sum(
        extension in _WRITING_EXTENSIONS for extension in extensions
    )
    code_count = sum(extension in _CODE_EXTENSIONS for extension in extensions)
    docs_site = any(
        name in _DOCS_SITE_FILES or name.startswith("docusaurus.config")
        for name in names
    )
    documentation = (
        docs_site
        or writing_count >= max(2, code_count)
        or any(
            _contains_token(path, _DOCUMENTATION_TOKENS)
            for path in search_paths
        )
    )

    engineering = (
        any(name in _APP_MANIFESTS for name in names)
        or code_count >= 2
        or not (ops or marketing or research or documentation)
    )

    inferred = [
        category
        for category, active in (
            ("ops", ops),
            ("marketing", marketing),
            ("research", research),
            ("documentation", documentation),
            ("engineering", engineering),
        )
        if active
    ]

    memberships = saved + [
        category for category in inferred if category not in saved
    ]
    return memberships or ["documentation"]


def _project_shape(paths: list[str]) -> tuple[str, str]:
    """Describe project form independently from its environment memberships."""
    normalized = [path.replace("\\", "/") for path in paths]
    names = [PurePosixPath(path).name.lower() for path in normalized]
    extensions = [PurePosixPath(name).suffix.lower() for name in names]
    writing_count = sum(
        extension in _WRITING_EXTENSIONS for extension in extensions
    )
    code_count = sum(extension in _CODE_EXTENSIONS for extension in extensions)

    if any(
        extension in _SLIDE_EXTENSIONS
        or _contains_token(path, {"deck", "slides"})
        for path, extension in zip(normalized, extensions)
    ):
        return "slab", "Slides"
    if any(
        name in _DOCS_SITE_FILES or name.startswith("docusaurus.config")
        for name in names
    ) or writing_count >= 2:
        return "stack", "Docs"
    if any(name in _APP_MANIFESTS for name in names) or code_count >= 2:
        return "disc", "App"
    if writing_count:
        return "ring", "One-pager"
    return "diamond", "Unknown"


def _project_id_from_category(category: str) -> str:
    return category[2:] if category.startswith("p_") else category


def _now_iso() -> str:
    """This projection's freshness stamp — UTC, ISO-8601 with a ``Z``."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_categorized_graph(source: dict | None = None) -> dict:
    """Collapse the space graph into the five purpose categories.

    ``source`` lets a caller that has already built the graph hand it over —
    the workspace document builds both views from one scan. Passing ``None``
    keeps the original standalone behaviour.
    """
    if source is None:
        source = build_space_data()
    source_groups = source.get("groups") or []
    source_leaves = source.get("leaves") or []

    group_ids_by_category: dict[str, set[str]] = {}
    for group in source_groups:
        group_ids_by_category.setdefault(str(group.get("cat") or ""), set()).add(
            str(group.get("id") or "")
        )

    categories = {
        category: {"name": _LABELS[category], "color": _COLORS[category]}
        for category in CATEGORIES
    }
    hub_angles = {
        category: -math.pi / 2 + index * 2 * math.pi / len(CATEGORIES)
        for index, category in enumerate(CATEGORIES)
    }
    counts = {category: 0 for category in CATEGORIES}
    groups = [
        {
            "id": f"g_{category}",
            "cat": category,
            "label": _LABELS[category],
            "blurb": "",
        }
        for category in CATEGORIES
    ]

    leaves: list[dict] = []
    ties: list[dict] = []
    milestones: list[dict] = []
    for hub in source.get("hubs") or []:
        source_category = str(hub.get("cat") or hub.get("id") or "")
        project_id = _project_id_from_category(source_category)
        group_ids = group_ids_by_category.get(source_category, set())
        project_leaves = [
            leaf
            for leaf in source_leaves
            if str(leaf.get("group") or "") in group_ids
        ]
        paths = [
            str(leaf.get("path") or leaf.get("label") or "")
            for leaf in project_leaves
        ]
        memberships = classify_project(project_id, paths)
        primary = memberships[0]
        for category in memberships:
            counts[category] += 1

        dates = sorted(
            str(leaf.get("date") or "")
            for leaf in project_leaves
            if leaf.get("date")
        )
        latest = dates[-1] if dates else date.today().isoformat()
        earliest = dates[0] if dates else None
        shape, tag = _project_shape(paths)
        label = str(hub.get("label") or project_id)
        category_labels = " + ".join(_LABELS[item] for item in memberships)
        leaves.append(
            {
                "id": project_id,
                "group": f"g_{primary}",
                "shape": shape,
                "tag": tag,
                "label": label,
                "date": latest,
                "blurb": (
                    f"{len(project_leaves)} mapped files · {category_labels}"
                ),
                "path": project_id,
                "clusters": memberships,
                "xotype": "output",
            }
        )
        for secondary in memberships[1:]:
            ties.append(
                {
                    "s": project_id,
                    "t": f"g_{secondary}",
                    "label": "also in",
                }
            )
        if earliest:
            milestones.append({"d": earliest, "t": f"{label} first mapped work"})

    for group in groups:
        count = counts[group["cat"]]
        group["blurb"] = f"{count} project{'s' if count != 1 else ''}"

    hubs = [
        {
            "id": category,
            "cat": category,
            "label": _LABELS[category],
            "blurb": (
                f"{counts[category]} project"
                f"{'s' if counts[category] != 1 else ''} · "
                f"{_DESCRIPTIONS[category]}"
            ),
        }
        for category in CATEGORIES
    ]

    today = date.today()
    project_dates = sorted(leaf["date"] for leaf in leaves)
    if project_dates:
        timeline_start = (
            date.fromisoformat(project_dates[0]) - timedelta(days=7)
        ).isoformat()
        timeline_end = (
            date.fromisoformat(project_dates[-1]) + timedelta(days=7)
        ).isoformat()
    else:
        timeline_start = (today - timedelta(days=7)).isoformat()
        timeline_end = (today + timedelta(days=7)).isoformat()

    project_count = len(leaves)
    return {
        "meta": {
            "title": "Dashboard",
            "tagline": "projects gathered into purpose environments",
            "mappedOn": today.strftime("%d %B %Y"),
            # This projection's own stamp, not the source graph's: it is a
            # separate file with a separate freshness (syncplan T25).
            "generated_at": _now_iso(),
            "workspace": (source.get("meta") or {}).get("workspace"),
            "noun": "projects",
            "collectionLabel": "environments",
            "rootEdgeLabel": "an environment of this workspace",
            "hubLabel": "Environment",
            "timelineTitle": "The workspace, by purpose, over time.",
            "timelineSub": (
                "Scrub through the project map and trace an environment."
            ),
            "enclose": True,
            "tieSpring": {"d": 80, "k": 0.07},
            "shapeLegend": [
                {"shape": "disc", "label": "App"},
                {"shape": "ring", "label": "One-pager"},
                {"shape": "stack", "label": "Docs"},
                {"shape": "slab", "label": "Slides"},
                {"shape": "diamond", "label": "Unknown"},
            ],
            "typeLegend": [
                {"id": "output", "label": "Output"},
                {"id": "inbox", "label": "Inbox"},
                {"id": "session", "label": "Sessions", "weight": "dim"},
                {"id": "system", "label": "System", "weight": "dim"},
            ],
        },
        "categories": categories,
        "hubAngles": hub_angles,
        "timeline": {"start": timeline_start, "end": timeline_end},
        "root": {
            "id": "environments-root",
            "label": "Environments",
            "blurb": f"{project_count} projects across 5 environments",
        },
        "hubs": hubs,
        "groups": groups,
        "leaves": leaves,
        "ties": ties,
        "milestones": milestones,
    }

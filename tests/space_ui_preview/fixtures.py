"""Synthetic, deterministic API payloads for reviewing the real Space UI.

All projects, people, paths and activity below are fictional. This module
never imports the application, reads a workspace, or connects to a service.
Shapes follow categorized_graph.py, space_index.py and the project BFFs.
"""

from datetime import datetime, timedelta, timezone
import math

NOW = datetime(2026, 9, 14, 10, tzinfo=timezone.utc)
WORKSPACE = "/demo/xo-projects"
WORKSPACE_ID = "demo-workspace-aurora"
CATEGORIES = {
    "engineering": {"name": "Engineering", "color": "#6fb7e0"},
    "ops": {"name": "Ops", "color": "#e8a15c"},
    "documentation": {"name": "Documentation", "color": "#c792ea"},
    "research": {"name": "Research", "color": "#7fd0a8"},
    "marketing": {"name": "Marketing", "color": "#e0708a"},
}
PROJECTS = [
    ("aurora-console", "Aurora Console", "A calm control room for releases, services, and customer signals.", "engineering", "disc", "App"),
    ("orbit-api", "Orbit API", "Typed service contracts and a reliable event delivery pipeline.", "engineering", "disc", "App"),
    ("field-notes", "Field Notes", "An offline notebook for teams working beyond the office.", "engineering", "disc", "App"),
    ("harbor-infra", "Harbor Infrastructure", "Repeatable environments, deployment recipes, and recovery plans.", "ops", "diamond", "Unknown"),
    ("signal-watch", "Signal Watch", "Useful alerts and runbooks for the systems people depend on.", "ops", "disc", "App"),
    ("atlas-handbook", "Atlas Handbook", "The team's working agreements, onboarding guide, and decisions.", "documentation", "stack", "Docs"),
    ("developer-guide", "Developer Guide", "Practical examples that turn a first API call into a shipped feature.", "documentation", "stack", "Docs"),
    ("retrieval-lab", "Retrieval Lab", "Small, reproducible studies of search quality and citation accuracy.", "research", "diamond", "Unknown"),
    ("latency-study", "Latency Study", "Measure where requests wait and test the changes that help.", "research", "ring", "One-pager"),
    ("launch-studio", "Launch Studio", "Product stories, release notes, and the next launch presentation.", "marketing", "slab", "Slides"),
]


def stamp(minutes=0):
    return (NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def native_connectors():
    """Read-only connection examples; never use real credentials or sessions."""
    return {
        "/api/connectors/github/status": {"status": "connected", "username": "demo-developer", "auth_method": "pat"},
        "/api/connectors/magicpath/status": {
            "skill_installed": True, "cli_installed": True, "cli_version": "1.0.0",
            "logged_in": False, "user": None,
        },
        "/api/connectors/vercel/status": {"status": "needs_auth"},
        "/api/connectors/gdrive/remotes": {"remotes": [
            {"name": "design-files", "type": "drive", "scope": "drive.file", "complete": True},
        ]},
        "/api/connectors/onedrive/remotes": {"remotes": []},
    }


def catalog():
    return {"items": [
        {"id": pid, "display_name": name, "description": description,
         "path": f"{WORKSPACE}/{pid}", "created_at": stamp(1440 * (65 - i * 4)),
         "unscaffolded": False}
        for i, (pid, name, description, *_rest) in enumerate(PROJECTS)
    ]}


def project_removal(project_id):
    """Fictional access review only; the preview never removes project files."""
    shared = project_id == "aurora-console"
    members = [{"workspace_id": WORKSPACE_ID, "role": "owner", "status": "active",
                "is_self": True, "can_revoke": False}]
    if shared:
        members.extend({"workspace_id": workspace, "role": "member", "status": "active",
                        "is_self": False, "can_revoke": True}
                       for workspace in ("demo-workspace-summit", "demo-workspace-coast"))
    return {"project_id": project_id, "can_remove": not shared,
            "repo": f"github.com/fictional-workspace/{project_id}", "members": members,
            "peers": [], "blockers": [{"code": "shared_project",
                "message": "Revoke access for every other Space before removing this project."}] if shared else []}


def paths_for(project):
    kind = project[3]
    common = ["README.md", "PLAN.md", "AGENTS.md"]
    content = {
        "engineering": ["src/main.ts", "src/client.ts", "src/config.ts", "tests/client.test.ts", "docs/architecture.md", "package.json"],
        "ops": ["infra/main.tf", "infra/variables.tf", "runbooks/deploy.md", "runbooks/recovery.md", "scripts/check.sh"],
        "documentation": ["guides/getting-started.md", "guides/conventions.md", "decisions/001-layout.md", "reference/api.md", "reference/glossary.md"],
        "research": ["notebooks/baseline.ipynb", "notebooks/evaluation.ipynb", "notes/method.md", "notes/findings.md", "results/summary.csv"],
        "marketing": ["slides/launch.pptx", "copy/announcement.md", "copy/release-notes.md", "brand/palette.svg", "briefs/audience.md"],
    }
    return common + content[kind]


def graph():
    hubs, groups, leaves, ties = [], [], [], []
    categories, history = {}, {}
    for i, project in enumerate(PROJECTS):
        pid, name, description, purpose, *_ = project
        cat = "p_" + pid
        categories[cat] = {"name": name, "color": CATEGORIES[purpose]["color"]}
        hubs.append({"id": cat, "cat": cat, "label": name, "blurb": description})
        folders = dict.fromkeys(path.split("/")[0] if "/" in path else "root" for path in paths_for(project))
        for folder in folders:
            groups.append({"id": f"g_{pid}_{folder}", "cat": cat,
                           "label": pid if folder == "root" else folder, "blurb": f"{name} / {folder}"})
        for j, path in enumerate(paths_for(project)):
            folder = path.split("/")[0] if "/" in path else "root"
            ext = path.rsplit(".", 1)[-1]
            shape = "ring" if ext == "md" else "disc" if ext in ("ts", "sh") else "diamond"
            leaves.append({"id": f"f_{pid}_{j}", "group": f"g_{pid}_{folder}", "shape": shape,
                           "tag": "Document" if ext == "md" else "Code" if shape == "disc" else "File",
                           "label": path.rsplit("/", 1)[-1], "date": f"2026-09-{2 + (i + j) % 12:02}",
                           "blurb": f"{name}: {path}", "path": f"{pid}/{path}"})
        ties.append({"s": f"f_{pid}_0", "t": f"f_{pid}_1", "label": "references"})
        history[cat] = [{"d": f"2026-09-{d:02}", "n": 1 + (d + i) % 5,
                         "s": ["Clarify the next milestone", "Improve the project guide"]} for d in (3, 7, 10, 13)]
    return {"meta": {"title": "XO Space", "tagline": "A fictional review workspace",
                     "mappedOn": "14 September 2026", "generated_at": stamp(), "workspace": WORKSPACE,
                     "hubLabel": "Project", "rootEdgeLabel": "a project in this workspace"},
            "categories": categories,
            "hubAngles": {cat: -math.pi / 2 + i * math.tau / len(categories) for i, cat in enumerate(categories)},
            "timeline": {"start": "2026-09-01", "end": "2026-09-15"},
            "root": {"id": "xo", "label": "XO", "blurb": "Ten fictional projects for reviewing Space UI"},
            "hubs": hubs, "groups": groups, "leaves": leaves, "ties": ties,
            "milestones": [{"d": "2026-09-03", "t": "First workspace milestone"}], "gitHistory": history}


def dashboard():
    leaves = [{"id": pid, "group": f"g_{cat}", "shape": shape, "tag": tag,
               "label": name, "date": "2026-09-13", "blurb": description,
               "path": pid, "clusters": [cat], "xotype": "output"}
              for pid, name, description, cat, shape, tag in PROJECTS]
    return {"meta": {"title": "Dashboard", "tagline": "projects gathered into purpose environments",
                     "mappedOn": "14 September 2026", "generated_at": stamp(), "workspace": WORKSPACE,
                     "noun": "projects", "collectionLabel": "environments", "hubLabel": "Environment",
                     "rootEdgeLabel": "an environment of this workspace", "enclose": True,
                     "tieSpring": {"d": 80, "k": 0.07},
                     "shapeLegend": [{"shape": s, "label": label} for s, label in
                                     [("disc", "App"), ("ring", "One-pager"), ("stack", "Docs"), ("slab", "Slides"), ("diamond", "Unknown")]],
                     "typeLegend": [{"id": "output", "label": "Output"}, {"id": "inbox", "label": "Inbox"},
                                    {"id": "session", "label": "Sessions", "weight": "dim"}, {"id": "system", "label": "System", "weight": "dim"}]},
            "categories": CATEGORIES,
            "hubAngles": {cat: -math.pi / 2 + i * math.tau / len(CATEGORIES) for i, cat in enumerate(CATEGORIES)},
            "timeline": {"start": "2026-09-01", "end": "2026-09-20"},
            "root": {"id": "environments-root", "label": "Environments", "blurb": "10 projects across 5 environments"},
            "hubs": [{"id": cat, "cat": cat, "label": value["name"], "blurb": f"{sum(p[3] == cat for p in PROJECTS)} projects"}
                     for cat, value in CATEGORIES.items()],
            "groups": [{"id": "g_" + cat, "cat": cat, "label": value["name"], "blurb": "Related projects"}
                       for cat, value in CATEGORIES.items()],
            "leaves": leaves, "ties": [], "milestones": []}


def activity(pid=None):
    sessions = [{"session_id": f"demo-session-{i}", "project_id": project[0], "agent": "workspace",
                 "runtime": "local", "opened_at": stamp(90 + i * 20), "last_activity_at": stamp(4 + i * 3)}
                for i, project in enumerate(PROJECTS[:3])]
    return {"open_sessions": [s for s in sessions if not pid or s["project_id"] == pid]}


def timeline(pid=None):
    return {"events": [{"id": f"demo-event-{i}", "project_id": p[0], "ts": stamp(12 + i * 67),
                        "type": "file.edited", "runtime": "local", "path": "README.md"}
                       for i, p in enumerate(PROJECTS) if not pid or p[0] == pid]}


def todos(pid):
    return {"project_id": pid, "updated_at": stamp(12), "sessions": {"demo-session": {"runtime": "local", "todos": [
        {"id": "t1", "content": "Finish the next milestone and document the review", "status": "in_progress"},
        {"id": "t2", "content": "Check keyboard navigation and narrow layouts", "status": "pending"},
        {"id": "t3", "content": "Agree on the acceptance criteria", "status": "completed"},
    ]}}}


def tree(pid, relative_path):
    project = next(p for p in PROJECTS if p[0] == pid)
    prefix = relative_path.rstrip("/") + "/" if relative_path else ""
    dirs, files = {}, []
    for path in paths_for(project):
        if not path.startswith(prefix):
            continue
        tail = path[len(prefix):]
        if "/" in tail:
            folder = tail.split("/", 1)[0]
            dirs[folder] = dirs.get(folder, 0) + 1
        else:
            files.append({"name": tail, "relative_path": path, "size_bytes": 1024 + 47 * len(path), "modified_at": stamp(16)})
    return {"project_id": pid, "relative_path": relative_path,
            "parent_relative_path": relative_path.rpartition("/")[0],
            "dirs": [{"name": name, "relative_path": prefix + name, "entries": count} for name, count in dirs.items()], "files": files}


def file_payload(pid, path, commit=None):
    name = next(p[1] for p in PROJECTS if p[0] == pid)
    content = f"# {name}\n\nA fictional workspace project used for reviewing Space UI.\n\n## This week's focus\n\n- Make the important work easy to find.\n- Keep context when moving between project views.\n- Document decisions alongside the work.\n\n## Review checklist\n\nThe **Dashboard**, **List**, **Graph**, **Tree**, and **Sharing** lenses provide different ways to explore the same workspace.\n\nOpen a file once, then move between lenses without losing your place.\n"
    if commit:
        content = f"# {name}\n\nAn earlier version of this fictional project brief.\n\n## First milestone\n\nAgree on a clear scope and record the first design decisions.\n"
    return {"project_id": pid, "relative_path": path, "name": path.rsplit("/", 1)[-1],
            "kind": "markdown", "content": content, "size_bytes": len(content.encode()), "modified_at": stamp(16), "truncated": False}


def commits(pid):
    return {"project_id": pid, "branch": "main", "source": "origin/main", "behind": 2 if pid == "aurora-console" else 0,
            "path": f"{WORKSPACE}/{pid}", "commits": [
                {"hash": "a1b2c3d" + "0" * 33, "short_hash": "a1b2c3d", "subject": "Clarify the project review checklist", "author": "Alex Example", "date": stamp(35), "path": "README.md"},
                {"hash": "d4e5f6a" + "0" * 33, "short_hash": "d4e5f6a", "subject": "Record the first milestone", "author": "Sam Example", "date": stamp(180), "path": "README.md"}]}


def sharing():
    return {"cadence": "active", "last_poll_ok": True, "last_poll_at": stamp(1), "watch_branch": "main",
            "own_workspace_id": WORKSPACE_ID, "projects_root": WORKSPACE, "recent": [],
            "repos": {f"github.com/fictional-workspace/{pid}":
                      {"project": pid, "shared": True, "available": True, "members": 3,
                       "last_fetch_at": stamp(1), "last_error": None, "clone": None, "auto_cloned_at": None}
                      for pid in ("aurora-console", "atlas-handbook", "retrieval-lab")}}

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


# ---------------------------------------------------------------- the Inbox
# Work items joined with their sessions (fictional): one list of rows, each
# under the section its source kind names, plus the sections summary. The
# shapes follow the Inbox contract (GET /api/inbox, the detail route and the
# session transcript); every id, session and person below is invented.
INBOX_SESSIONS = {
    "fictional-session-1": {"native": "fictional-native-1", "runtime": "demo", "title": "Invoice question from a fictional customer",
                            "messages": [
                                {"id": "m1", "role": "user", "content": "Handle Inbox item a1b2c3d4: an unread mail from a fictional customer about the June invoice."},
                                {"id": "m2", "role": "assistant", "content": "Read it. The customer is right about the seat count; I drafted a reply in reply.md that offers a corrected invoice.\n\n```json\n{\"outcome\": \"reply_drafted\", \"summary\": \"A seat-count correction; a two-line reply with the credit note is drafted.\", \"draft\": \"reply.md\"}\n```"},
                                {"id": "m3", "role": "user", "content": "Keep it to two lines and mention the credit note."},
                                {"id": "m4", "role": "assistant", "content": "Done: reply.md is two lines now and names the credit note."},
                            ]},
    "fictional-session-2": {"native": "fictional-native-2", "runtime": "demo", "title": "New commits fetched: fictional-workspace/aurora-console",
                            "messages": [
                                {"id": "m1", "role": "user", "content": "Handle Inbox item c3d4e5f6: two commits arrived on the shared aurora-console repo."},
                                {"id": "m2", "role": "assistant", "content": "Reading the two commits now. The first one touches the release checklist; checking whether the local copy diverged."},
                            ]},
    "fictional-session-3": {"native": "fictional-native-3", "runtime": "demo", "title": "Fix the flaky restart test",
                            "messages": [
                                {"id": "m1", "role": "user", "content": "The restart test fails one run in five. Find out why."},
                                {"id": "m2", "role": "assistant", "content": "Reproduced it: the server pid file is read before the process wrote it. Adding a wait with a timeout."},
                            ]},
    "fictional-session-4": {"native": "fictional-native-4", "runtime": "demo", "title": "Issue #12 in orbit-api: Flaky restart test",
                            "messages": [
                                {"id": "m1", "role": "user", "content": "Handle Inbox item e5f6a7b8: GitHub issue #12 in orbit-api."},
                            ]},
    "fictional-session-5": {"native": "fictional-native-5", "runtime": "demo", "title": "Draft the launch announcement",
                            "messages": [
                                {"id": "m1", "role": "user", "content": "Draft the launch announcement for Launch Studio."},
                                {"id": "m2", "role": "assistant", "content": "Drafted copy/announcement.md with the three headline features and a short quote."},
                            ]},
}


def _workitem(item_id, project_id, title, section, entity, state, source, fact, **extra):
    status = "closed" if state == "closed" else "open"
    row = {"kind": "workitem", "id": item_id, "project_id": project_id, "pid": "pid-" + project_id,
           "title": title, "section": section, "entity": entity, "state": state, "status": status,
           "state_reason": "completed" if status == "closed" else None, "assignee": None,
           "source": source, "fact": fact, "claim": None, "session": None, "outcome": None, "sessions": [],
           "created_at": stamp(60), "updated_at": stamp(30)}
    row.update(extra)
    return row


def _session_row(session_id, project_id, title, section, entity, live):
    return {"kind": "session", "id": session_id, "project_id": project_id, "pid": "pid-" + project_id, "title": title,
            "section": section, "entity": entity, "state": "running" if live else "closed", "runtime": "demo",
            "native_id": INBOX_SESSIONS[session_id]["native"], "started_at": stamp(50), "updated_at": stamp(2 if live else 200),
            "live": live}


def inbox_rows():
    ended = {"session_id": "fictional-session-1", "native_session_id": "fictional-native-1", "runtime": "demo",
             "attempt": 1, "started_at": stamp(44), "ended_at": stamp(30), "exit": {"status": "ok", "message": None}}
    running = {"session_id": "fictional-session-2", "native_session_id": "fictional-native-2", "runtime": "demo",
               "attempt": 1, "started_at": stamp(3), "ended_at": None, "exit": None}
    failed = {"session_id": "fictional-session-4", "native_session_id": "fictional-native-4", "runtime": "demo",
              "attempt": 1, "started_at": stamp(20), "ended_at": stamp(19), "exit": {"status": "error", "message": "the runtime exited before an answer"}}
    return [
        _workitem("a1b2c3d4-0000-4000-8000-000000000001", "inbox-connections", "Invoice question from a fictional customer",
                  "connections", "gmail", "waiting", {"kind": "connection", "key": "connection:gmail:unread:18c2a9f1",
                  "connection": {"toolkit": "gmail", "type": "unread", "event": "18c2a9f1"}},
                  {"ts": stamp(45), "kind": "gmail.unread", "url": "https://mail.example.invalid/msg/1", "link": {"view": "connectors"}, "toolkit": "gmail"},
                  session=ended, outcome={"kind": "reply_drafted", "summary": "A seat-count correction; a two-line reply with the credit note is drafted.",
                  "draft": "reply.md", "task": None, "question": None, "acted": [], "at": stamp(30)},
                  sessions=[{"id": "fictional-session-1", "native_id": "fictional-native-1", "runtime": "demo", "title": "Invoice question", "updated_at": stamp(30), "live": False}]),
        _workitem("b2c3d4e5-0000-4000-8000-000000000002", "inbox-connections", "Review meeting at 15:00 with the fictional platform team",
                  "connections", "googlecalendar", "new", {"kind": "connection", "key": "connection:googlecalendar:upcoming:ev-7",
                  "connection": {"toolkit": "googlecalendar", "type": "upcoming", "event": "ev-7"}},
                  {"ts": stamp(12), "kind": "googlecalendar.upcoming", "url": "https://calendar.example.invalid/ev-7", "link": {"view": "connectors"}, "toolkit": "googlecalendar"}),
        _workitem("c3d4e5f6-0000-4000-8000-000000000003", "aurora-console", "New commits fetched: fictional-workspace/aurora-console",
                  "projects", "aurora-console", "running", {"kind": "sharing", "key": "sharing:fetched:fictional-workspace/aurora-console:" + stamp(5),
                  "sharing": {"repo": "fictional-workspace/aurora-console", "event": "fetched"}},
                  {"ts": stamp(5), "kind": "sharing.fetched", "url": None, "link": {"view": "sharing", "project": "aurora-console"}},
                  claim={"session_id": "fictional-session-2", "runtime": "demo", "started_at": stamp(3), "live": True}, session=running,
                  sessions=[{"id": "fictional-session-2", "native_id": "fictional-native-2", "runtime": "demo", "title": "New commits fetched", "updated_at": stamp(1), "live": True}]),
        _session_row("fictional-session-3", "aurora-console", "Fix the flaky restart test", "projects", "aurora-console", True),
        _workitem("e5f6a7b8-0000-4000-8000-000000000005", "orbit-api", "Issue #12 in orbit-api: Flaky restart test",
                  "issues", "fictional-workspace/orbit-api", "failed", {"kind": "github", "key": None,
                  "github": {"repo": "fictional-workspace/orbit-api", "number": 12, "node_id": "I_fictional12"}},
                  {"ts": stamp(25), "kind": "issue.open", "url": "https://github.com/fictional-workspace/orbit-api/issues/12", "link": {"view": "projects", "project": "orbit-api"}},
                  session=failed,
                  sessions=[{"id": "fictional-session-4", "native_id": "fictional-native-4", "runtime": "demo", "title": "Issue #12", "updated_at": stamp(19), "live": False}]),
        _workitem("f6a7b8c9-0000-4000-8000-000000000006", "inbox-agents", "The release checklist is ready for review",
                  "agents", "demo", "closed", {"kind": "post", "key": None, "post": {"agent": "demo", "kind": "note"}},
                  {"ts": stamp(300), "kind": "note", "url": None, "link": {"project": "launch-studio", "path": "copy/release-notes.md"}},
                  outcome={"kind": "handled", "summary": "Filed under the launch notes; nothing to decide.", "draft": None, "task": None, "question": None, "acted": [], "at": stamp(280)},
                  updated_at=stamp(280)),
        _session_row("fictional-session-5", "launch-studio", "Draft the launch announcement", "agents", "demo", False),
    ]


_INBOX_STATES = {"open": {"new", "running", "waiting", "failed"}, "active": {"running"}, "waiting": {"waiting"},
                 "closed": {"closed"}, "all": {"new", "running", "waiting", "failed", "closed"}}
_INBOX_ENTITIES = {
    "projects": [(pid, name) for pid, name, *_ in PROJECTS[:4]],
    "agents": [("demo", "demo")],
    "connections": [("gmail", "gmail"), ("googlecalendar", "googlecalendar")],
    "issues": [("fictional-workspace/orbit-api", "fictional-workspace/orbit-api")],
}


def _counts(rows):
    counts = {"new": 0, "running": 0, "waiting": 0, "failed": 0, "closed": 0}
    for row in rows:
        counts[row["state"]] += 1
    return counts


def inbox(section=None, state="open"):
    """GET /api/inbox: the rows of one section (or every section) in one state, plus the summary."""
    rows = inbox_rows()
    wanted = _INBOX_STATES.get(state, _INBOX_STATES["open"])
    sections = []
    for sid, label in (("connections", "Connections"), ("projects", "Projects"), ("issues", "Issues"), ("agents", "Agents")):
        mine = [row for row in rows if row["section"] == sid]
        entities = [{"id": eid, "label": elabel, "counts": _counts([row for row in mine if row["entity"] == eid])}
                    for eid, elabel in _INBOX_ENTITIES[sid]]
        sections.append({"id": sid, "label": label, "counts": _counts(mine), "entities": entities})
    listed = [row for row in rows if (not section or row["section"] == section) and row["state"] in wanted]
    return {"schema": 1, "generated_at": stamp(), "runner": {"enabled": True}, "sections": sections,
            "rows": listed, "count": len(listed)}


def inbox_item(project_id, workitem_id):
    """GET /api/inbox/{project_id}/{workitem_id}: the row with its sidecars, or None."""
    row = next((row for row in inbox_rows() if row["kind"] == "workitem" and row["id"] == workitem_id and row["project_id"] == project_id), None)
    if row is None:
        return None
    bodies = {"gmail.unread": "Hi, the June invoice shows 12 seats but we have 10.\n\nCould you reissue it?",
              "googlecalendar.upcoming": "15:00 to 15:30, fictional platform team. Agenda: the rollout schedule.",
              "sharing.fetched": "2 commits on main: Clarify the project review checklist; Record the first milestone.",
              "issue.open": "labels: bug · assignees: you\n\nThe restart test fails one run in five on the shared runner.",
              "note": "The checklist in copy/release-notes.md is complete; every item has an owner."}
    fact = dict(row["fact"], title=row["title"], body=bodies.get(row["fact"]["kind"], ""), key=row["source"].get("key"),
                section=row["section"], entity=row["entity"])
    session = row["session"]
    running = row["state"] == "running"
    return dict(row, fact=fact, running=running, can_reply=True, can_send=bool(row["outcome"] and row["outcome"]["kind"] == "reply_drafted"),
                transcript={"session_id": session["session_id"], "native_session_id": session["native_session_id"]} if session else None,
                policy={"schema": 1, "sessions": {"mode": "auto" if row["section"] == "connections" else "manual", "kinds": [],
                        "agent_type": "inbox-item", "runtime": None, "max_concurrent": 2, "max_per_hour": 20, "timeout_s": 300, "act": True},
                        "retention_days": 30},
                workitem={"id": row["id"], "title": row["title"], "status": row["status"], "state_reason": row["state_reason"],
                          "assignee": None, "labels": ["inbox", row["section"]], "runtime": "inbox", "source": row["source"],
                          "links": {"session_ids": [s["id"] for s in row["sessions"]]}})


def transcript(session_id):
    """GET /api/sessions/{session_id}/transcript: {title, messages:[{id, role, content}]}, or None."""
    session = INBOX_SESSIONS.get(session_id)
    if session is None:
        return None
    return {"title": session["title"], "messages": [dict(message) for message in session["messages"]]}

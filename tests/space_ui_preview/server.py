#!/usr/bin/env python3
"""Serve actual Space assets with fictional APIs, bound only to localhost."""

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlsplit

import fixtures

UI_ROOT = Path(__file__).resolve().parents[2] / "space_ui"


class Handler(SimpleHTTPRequestHandler):
    def json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        path = unquote(url.path)
        query = parse_qs(url.query)
        native = fixtures.native_connectors().get(path)
        if native is not None:
            self.json_response(native)
            return
        route = {
            "/xo/space.json": fixtures.graph,
            "/xo/dashboard.json": fixtures.dashboard,
            "/api/xo-projects": fixtures.catalog,
            "/api/xo-projects/activity": fixtures.activity,
            "/api/xo-projects/timeline": fixtures.timeline,
            "/api/project-sharing/status": fixtures.sharing,
            "/space/branding": lambda: {"name": "Space", "logo_url": None},
            "/space/setup/status": lambda: {
                "checked_at": fixtures.stamp(),
                "space": {"status": "configured", "id": fixtures.WORKSPACE_ID,
                          "label": "Review workspace", "owner": "demo-owner"},
                "xo": {"status": "connected", "user_id": "demo-user"},
                "github": {"status": "connected", "username": "demo-developer", "source": "connector"},
            },
            "/space/server/status": lambda: {"running": True},
            "/api/inbox": lambda: fixtures.inbox(query.get("section", [None])[0], query.get("state", ["open"])[0]),
            "/api/connections": lambda: {"signed_in": False, "poller_enabled": True, "connections": []},
            "/xo/sessions.json": lambda: {"meta": {"sources": [{"id": "demo", "label": "Fictional telemetry", "available": False}]}, "sessions": []},
            "/api/secrets": lambda: {"items": []},
            "/api/schedules": lambda: {"jobs": []},
            "/api/runtime-config": lambda: {
                "configured": {"agent_name": "demo", "watcher_enabled": False, "watcher_source_mode": "all", "watcher_interval_seconds": 30},
                "applied": {"agent_name": "demo", "watcher_enabled": False, "watcher_source_mode": "all", "watcher_interval_seconds": 30},
                "agents": [], "restart_required": False, "restart_supported": False},
            "/space/update/status": lambda: {"supported": False, "message": "Fictional review server; updates are unavailable."},
            "/xo-auth/session/self": lambda: {"session_id": "fictional-review-session"},
            "/api/connectors/composio/backend": lambda: {"mode": "inactive", "key_source": None},
            "/api/connectors/composio/toolkits": lambda: {"toolkits": []},
        }.get(path)
        if route:
            self.json_response(route())
            return
        match = re.fullmatch(r"/api/xo-projects/([^/]+)/(tree|todos|activity|timeline|file|file-history|commits|members|removal|github/issues)", path)
        if match and match[1] in {p[0] for p in fixtures.PROJECTS}:
            pid, operation = match.groups()
            relative = query.get("relative_path", [""])[0]
            payload = {
                "tree": lambda: fixtures.tree(pid, relative),
                "todos": lambda: fixtures.todos(pid),
                "activity": lambda: fixtures.activity(pid),
                "timeline": lambda: fixtures.timeline(pid),
                "file": lambda: fixtures.file_payload(pid, relative, query.get("commit")),
                "file-history": lambda: {"project_id": pid, "relative_path": relative, "is_repo": True, "items": fixtures.commits(pid)["commits"]},
                "commits": lambda: fixtures.commits(pid),
                "removal": lambda: fixtures.project_removal(pid),
                "members": lambda: {"own_workspace_id": fixtures.WORKSPACE_ID, "members": [
                    {"workspace_id": fixtures.WORKSPACE_ID, "role": "owner", "status": "active", "bound": True},
                    {"workspace_id": "demo-workspace-summit", "role": "member", "status": "active", "bound": True}]},
                "github/issues": lambda: {"project_id": pid, "state": "empty", "repo": f"fictional-workspace/{pid}", "issues": [], "tracked": 0, "fetched_at": fixtures.stamp(20)},
            }[operation]()
            self.json_response(payload)
            return
        match = re.fullmatch(r"/api/inbox/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)", path)
        if match:
            item = fixtures.inbox_item(*match.groups())
            if item is None:
                self.json_response({"detail": {"code": "workitem_not_found", "message": "No fictional work item " + match[2]}}, 404)
            else:
                self.json_response(item)
            return
        match = re.fullmatch(r"/api/sessions/([A-Za-z0-9._-]+)/transcript", path)
        if match:
            transcript = fixtures.transcript(match[1])
            if transcript is None:
                self.json_response({"detail": "Session not found"}, 404)
            else:
                self.json_response(transcript)
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path.startswith("/space/"):
            asset = (UI_ROOT / path.removeprefix("/space/")).resolve()
            if asset == UI_ROOT:
                asset = UI_ROOT / "index.html"
            if asset.is_relative_to(UI_ROOT) and asset.is_file():
                self.path = "/" + asset.relative_to(UI_ROOT).as_posix()
                return super().do_GET()
        self.json_response({"error": f"No fictional fixture for {path}"}, 404)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5100)
    args = parser.parse_args()
    handler = lambda *a, **kw: Handler(*a, directory=str(UI_ROOT), **kw)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        print(f"Fictional Space review workspace: http://127.0.0.1:{args.port}/space/", flush=True)
        server.serve_forever()

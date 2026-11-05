"""A fake GitHub and a fake xo-launchpad for the Launchpad deploy tests.

``World`` is one test's outside world. GitHub's repos are real bare git
repositories under ``<root>/git/<owner>/<repo>.git``, so the script's git
really pushes, fetches and merges; the fake GitHub API (a local HTTP server)
answers ``/user``, ``/repos``, ``/user/repos`` and Actions from them. A run
exists for every commit of a repo whose tree holds a Launchpad workflow and
whose message lacks ``[skip ci]``, and moves queued → in_progress →
completed one poll at a time. The fake xo-launchpad is a script with its
state in ``state.json`` beside it: ``app create`` commits the workflow into
the repo like Launchpad does, a finished build leaves an image the next
``app status`` rolls out. The app's URL is served by the fake GitHub server.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

KEY = "ak_test_launchpad_key_123"
TOKEN = "gho_testtoken_abcdefghijklmnop"
LOGIN = "octo"
GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "init.defaultBranch=main",
          "-c", "commit.gpgsign=false"]


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *GIT_ID, *args], cwd=cwd, capture_output=True, text=True, check=True,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return result.stdout.strip()


FAKE_CLI = r'''#!{python}
"""Fake xo-launchpad: state.json beside this file."""
import fcntl, json, os, subprocess, sys, tempfile, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
GIT_ID = ["-c", "user.name=Launchpad", "-c", "user.email=lp@example.com", "-c", "commit.gpgsign=false"]


def fail(message):
    print("Error: " + message, file=sys.stderr)
    sys.exit(1)


def out(data):
    print(json.dumps(data, indent=2))
    sys.exit(0)


def git(*args, cwd=None):
    subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True, capture_output=True)


with open(HERE / "state.lock", "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    state = json.loads(STATE.read_text())
    args = [a for a in sys.argv[1:] if a not in ("--json", "--yes")]
    state["calls"].append({"argv": args, "github_token": bool(os.environ.get("GITHUB_TOKEN")),
                           "env": sorted(k for k in os.environ if k.startswith("LAUNCHPAD_VALUE_"))})

    def save():
        STATE.write_text(json.dumps(state, indent=2))

    def find(ident):
        for app in state["apps"].values():
            if ident in (app["id"], app["name"], app["slug"]):
                return app
        save()
        fail("HTTP 404: App not found" if len(ident) == 36 else "App not found or ambiguous; run app list and use its UUID")

    if os.environ.get("XO_API_KEY") != state["key"]:
        save()
        fail("HTTP 401: authentication rejected; check XO_API_KEY")
    group, action, rest = args[0], args[1], args[2:]
    if (group, action) == ("app", "list"):
        save(); out({"apps": list(state["apps"].values())})
    if (group, action) == ("app", "get"):
        app = find(rest[0]); save(); out({"app": app})
    if (group, action) == ("app", "create"):
        if state["fail"].get("create_workflow"):
            save()
            fail("HTTP 422: GitHub: GitHub PUT /repos/x/y/contents/.github/workflows/launchpad-y.yml: 404 Not Found")
        if not os.environ.get("GITHUB_TOKEN"):
            save(); fail("Environment variable GITHUB_TOKEN is not set")
        name = rest[0]
        opts = {"port": None, "health": None, "params": {}}
        i = 1
        while i < len(rest):
            flag = rest[i]
            if flag == "--port": opts["port"] = int(rest[i + 1]); i += 2
            elif flag == "--health-path": opts["health"] = rest[i + 1]; i += 2
            elif flag == "--template": i += 2
            elif flag == "--param":
                k, _, v = rest[i + 1].partition("="); opts["params"][k] = v; i += 2
            elif flag == "--param-env": i += 2
            else: i += 1
        state["seq"] += 1
        slug = f"{name}-xo-{state['seq']:04d}"
        app_id = str(uuid.UUID(int=state["seq"]))
        repo = opts["params"]["repoUrl"].removeprefix("https://github.com/")
        app = {"id": app_id, "name": name, "slug": slug, "template": "github", "template_version": 1,
               "source": "github", "params": {"repoUrl": opts["params"]["repoUrl"], "branch": opts["params"]["branch"]},
               "image": f"registry.test/{slug}/app", "image_tag": None, "size": "xo.small", "port": opts["port"],
               "health_path": opts["health"], "storage": None, "host": f"{slug}.apps.test",
               "url": f"{state['app_base']}/app/{slug}", "public_host": f"{slug}.apps.test",
               "public_url": f"{state['app_base']}/app/{slug}", "status": "created", "current_release_id": None,
               "auto_deploy": True, "replicas": 1, "rollout_timeout": 300, "notes": None, "push_robot": None,
               "created_at": "2026-11-05T12:00:00Z", "updated_at": "2026-11-05T12:00:00Z",
               "pending": False, "releases": [], "env": {}, "sensitive": []}
        bare = Path(state["git_root"]) / f"{repo}.git"
        with tempfile.TemporaryDirectory() as tmp:
            git("clone", "-q", "-b", opts["params"]["branch"], str(bare), tmp)
            flow = Path(tmp) / ".github" / "workflows" / f"launchpad-{name}-build-{state['seq']}.yml"
            flow.parent.mkdir(parents=True, exist_ok=True)
            flow.write_text(f"name: launchpad {slug}\non: push\n")
            git("add", "-A", cwd=tmp)
            git("commit", "-q", "-m", f"Add Launchpad workflow for {slug}", cwd=tmp)
            git("push", "-q", "origin", f"HEAD:{opts['params']['branch']}", cwd=tmp)
        state["apps"][app_id] = app
        save()
        out({"app": app, "registry": None, "notes": None, "release": None})
    if (group, action) == ("app", "status"):
        app = find(rest[0])
        live = {"state": "progressing", "replicas": {"desired": 1, "available": 0}, "diagnosis": None}
        if app["pending"]:
            app["pending"] = False
            release = {"id": str(uuid.uuid4()), "number": len(app["releases"]) + 1, "rollout_status": "progressing",
                       "image": app["image"], "digest": None, "tag": "latest", "source": "webhook",
                       "rollout_reason": None, "rollout_message": None, "scan": None,
                       "created_at": "2026-11-05T12:01:00Z", "finished_at": None, "exposed_ports": [3000],
                       "failure": None}
            app["releases"].append(release)
            app["current_release_id"] = release["id"]
            app["status"] = "deploying"
        elif app["releases"] and app["releases"][-1]["rollout_status"] == "progressing":
            release = app["releases"][-1]
            if state["fail"].get("health"):
                release["rollout_status"] = "failed"
                release["failure"] = "The container never answered on its port."
                live = {"state": "failed", "replicas": {"desired": 1, "available": 0},
                        "diagnosis": {"kind": "port", "message": "No answer on port 8080", "port": app["port"]}}
                app["status"] = "failed"
            else:
                release["rollout_status"] = "ready"
                release["finished_at"] = "2026-11-05T12:01:40Z"
                app["status"] = "running"
        release = app["releases"][-1] if app["releases"] else None
        if release and release["rollout_status"] == "ready":
            live = {"state": "ready", "replicas": {"desired": 1, "available": 1}, "diagnosis": None}
        save()
        out({"app": app, "status": live, "release": release})
    if group == "app" and action == "env" and rest[0] == "set":
        app = find(rest[1]); i = 2
        while i < len(rest):
            if rest[i] == "--from-env":
                k, _, var = rest[i + 1].partition("="); app["env"][k] = os.environ[var]; i += 2
            elif rest[i] == "--sensitive":
                app["sensitive"].append(rest[i + 1]); i += 2
            else:
                k, _, v = rest[i].partition("="); app["env"][k] = v; i += 1
        save(); out({"vars": [{"key": k, "value": None, "sensitive": k in app["sensitive"]} for k in app["env"]]})
    if group == "app" and action == "env" and rest[0] == "list":
        app = find(rest[1]); save()
        out({"vars": [{"key": k, "value": None if k in app["sensitive"] else v, "sensitive": k in app["sensitive"]}
                      for k, v in app["env"].items()]})
    if group == "app" and action == "port":
        app = find(rest[1]); app["port"] = int(rest[2]); save(); out({"app": app})
    if group == "app" and action == "health":
        app = find(rest[1]); app["health_path"] = None if "--tcp" in rest else rest[rest.index("--path") + 1]
        save(); out({"app": app})
    save()
    fail("fake xo-launchpad: unknown command " + " ".join(args))
'''


class World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.git_root = root / "git"
        self.git_root.mkdir(parents=True)
        self.lp_dir = root / "lp"
        self.lp_dir.mkdir()
        self.state_path = self.lp_dir / "state.json"
        self.bin = self.lp_dir / "xo-launchpad"
        self.bin.write_text(FAKE_CLI.replace("{python}", sys.executable))
        self.bin.chmod(0o755)
        self.scopes: str | None = "gist, read:org, repo, workflow"
        self.repos: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}      # sha -> run
        self.build_fails = False
        self.app_status = 200
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.save({"key": KEY, "apps": {}, "calls": [], "fail": {}, "seq": 0, "git_root": str(self.git_root),
                   "app_base": self.api})

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ── shared state ─────────────────────────────────────────────────────
    @contextmanager
    def _locked(self):
        with open(self.lp_dir / "state.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def state(self) -> dict[str, Any]:
        with self._locked():
            return json.loads(self.state_path.read_text())

    def save(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state, indent=2))

    def update(self, fn) -> None:
        with self._locked():
            state = json.loads(self.state_path.read_text())
            fn(state)
            self.save(state)

    def env(self) -> dict[str, str]:
        return {"LAUNCHPAD_GITHUB_API": self.api, "LAUNCHPAD_GITHUB_GIT": f"file://{self.git_root}",
                "XO_LAUNCHPAD_BIN": str(self.bin), "LAUNCHPAD_POLL_SECONDS": "0.01"}

    def credentials(self) -> dict[str, str]:
        return {"XO_API_KEY": KEY, "GITHUB_TOKEN": TOKEN}

    # ── repos ────────────────────────────────────────────────────────────
    def bare(self, full: str) -> Path:
        return self.git_root / f"{full}.git"

    def add_repo(self, full: str, *, admin: bool = True, push: bool = True, private: bool = True) -> Path:
        path = self.bare(full)
        path.parent.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "--bare", "-b", "main", str(path))
        self.repos[full] = {"admin": admin, "push": push, "private": private}
        return path

    def remote_url(self, full: str) -> str:
        return f"file://{self.bare(full)}"

    def head(self, full: str, branch: str = "main") -> str | None:
        try:
            return git("--git-dir", str(self.bare(full)), "rev-parse", f"refs/heads/{branch}")
        except subprocess.CalledProcessError:
            return None

    def files(self, full: str, ref: str = "main") -> list[str]:
        return git("--git-dir", str(self.bare(full)), "ls-tree", "-r", "--name-only", ref).splitlines()

    def show(self, full: str, path: str, ref: str = "main") -> str:
        return git("--git-dir", str(self.bare(full)), "show", f"{ref}:{path}")

    def apps(self) -> dict[str, Any]:
        return self.state()["apps"]

    def fail(self, **flags: bool) -> None:
        self.update(lambda s: s["fail"].update(flags))

    def delete_app(self, app_id: str) -> None:
        self.update(lambda s: s["apps"].pop(app_id))

    # ── Actions ──────────────────────────────────────────────────────────
    def _runs_for(self, full: str, sha: str) -> list[dict[str, Any]]:
        """Like GitHub: one run per workflow in the commit's tree, none for [skip ci]."""
        bare = str(self.bare(full))
        try:
            git("--git-dir", bare, "cat-file", "-e", f"{sha}^{{commit}}")
        except subprocess.CalledProcessError:
            return []
        message = git("--git-dir", bare, "log", "-1", "--format=%B", sha)
        flows = [p for p in git("--git-dir", bare, "ls-tree", "-r", "--name-only", sha, ".github/workflows")
                 .splitlines() if os.path.basename(p).startswith("launchpad-")]
        if "[skip ci]" in message:
            return []
        out = []
        with self._lock:
            for path in flows:
                key = f"{sha}:{path}"
                if key not in self.runs:
                    run_id = 1000 + len(self.runs)
                    self.runs[key] = {"id": run_id, "head_sha": sha, "path": path, "status": "queued",
                                      "conclusion": None,
                                      "html_url": f"https://github.test/{full}/actions/runs/{run_id}",
                                      "run_started_at": "2026-11-05T12:00:00Z",
                                      "updated_at": "2026-11-05T12:00:42Z", "repo": full}
                out.append(dict(self.runs[key]))
        return out

    def runs_of(self, sha: str) -> list[dict[str, Any]]:
        return [run for key, run in self.runs.items() if key.startswith(sha + ":")]

    def _advance(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            run = next((r for r in self.runs.values() if r["id"] == run_id), None)
            if run is None:
                return None
            if run["status"] == "queued":
                run["status"] = "in_progress"
            elif run["status"] == "in_progress":
                run["status"] = "completed"
                run["conclusion"] = "failure" if self.build_fails else "success"
                if not self.build_fails:
                    repo_url = f"https://github.com/{run['repo']}"
                    self.update(lambda s: [a.update(pending=True) for a in s["apps"].values()
                                           if a["params"]["repoUrl"] == repo_url])
            return dict(run)

    # ── the fake GitHub API ──────────────────────────────────────────────
    def _handler(self):
        world = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: Any = None, headers: dict[str, str] | None = None,
                      text: str | None = None) -> None:
                raw = (text if text is not None else json.dumps(body)).encode()
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "text/plain" if text is not None else "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorized(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._send(401, {"message": "Bad credentials"})
                    return False
                return True

            def do_GET(self) -> None:  # noqa: N802
                url = urlsplit(self.path)
                parts = [p for p in url.path.split("/") if p]
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                world.requests.append(("GET", url.path))
                if parts[:1] == ["app"]:
                    return self._send(world.app_status, text="hello from the app")
                if not self._authorized():
                    return None
                if parts == ["user"]:
                    headers = {"X-OAuth-Scopes": world.scopes} if world.scopes is not None else {}
                    return self._send(200, {"login": LOGIN, "id": 42, "name": "Octo Cat"}, headers)
                if len(parts) >= 3 and parts[0] == "repos":
                    full = f"{parts[1]}/{parts[2]}"
                    repo = world.repos.get(full)
                    if repo is None:
                        return self._send(404, {"message": "Not Found"})
                    if len(parts) == 3:
                        return self._send(200, {"full_name": full, "private": repo["private"], "default_branch": "main",
                                                "size": 0, "permissions": {"admin": repo["admin"], "push": repo["push"]}})
                    if parts[3:5] == ["actions", "runs"] and len(parts) == 5:
                        sha = query.get("head_sha") or world.head(full, query.get("branch", "main"))
                        runs = world._runs_for(full, sha) if sha else []
                        return self._send(200, {"total_count": len(runs), "workflow_runs": runs})
                    if parts[3:5] == ["actions", "runs"] and len(parts) == 6:
                        run = world._advance(int(parts[5]))
                        return self._send(200, run) if run else self._send(404, {"message": "Not Found"})
                    if parts[3:5] == ["actions", "runs"] and parts[6:] == ["jobs"]:
                        run = next((r for r in world.runs.values() if r["id"] == int(parts[5])), None)
                        if run is None:
                            return self._send(404, {"message": "Not Found"})
                        step = {"name": "Build and push", "status": run["status"], "conclusion": run["conclusion"]}
                        return self._send(200, {"jobs": [{"id": run["id"] * 10, "name": "build", "status": run["status"],
                                                          "conclusion": run["conclusion"], "steps": [step]}]})
                    if parts[3:5] == ["actions", "jobs"] and parts[6:] == ["logs"]:
                        return self._send(200, text="2026-11-05T12:00:01.000Z Step 1\n"
                                                    "2026-11-05T12:00:02.000Z npm ERR! Missing script: start\n")
                return self._send(404, {"message": "Not Found"})

            def do_POST(self) -> None:  # noqa: N802
                world.requests.append(("POST", self.path))
                if not self._authorized():
                    return None
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/user/repos":
                    full = f"{LOGIN}/{body['name']}"
                    if full in world.repos:
                        return self._send(422, {"message": "name already exists on this account"})
                    world.add_repo(full, private=bool(body.get("private")))
                    world.repos[full]["created"] = body
                    return self._send(201, {"full_name": full, "private": bool(body.get("private"))})
                return self._send(404, {"message": "Not Found"})

        return Handler


def make_project(root: Path, name: str = "hello", *, dockerfile: str | None = "FROM node:24-slim\nEXPOSE 3000\n",
                 files: dict[str, str] | None = None, commit: bool = True) -> Path:
    """A project folder with a git repository and one commit."""
    project = root / name
    project.mkdir(parents=True)
    content = {"server.js": "require('http').createServer((q, s) => s.end('hi')).listen(3000)\n"}
    if dockerfile is not None:
        content["Dockerfile"] = dockerfile
    content.update(files or {})
    for path, text in content.items():
        (project / path).parent.mkdir(parents=True, exist_ok=True)
        (project / path).write_text(text)
    if commit:
        git("init", "-q", "-b", "main", cwd=project)
        git("add", "-A", cwd=project)
        git("commit", "-q", "-m", "First", cwd=project)
    return project


def events(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.strip().startswith("{")]


def temp_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="lp-test-"))


def remove(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)

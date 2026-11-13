"""The Launchpad deploy script end to end (services/launchpad/launchpad.py):
deploy, watch, preflight, apps, link and unlink, run as the server runs them,
against a fake GitHub and a fake xo-launchpad (tests/launchpad_kit.py) with
real git and real bare repositories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from services.launchpad import launchpad
from tests.launchpad_kit import KEY, LOGIN, TOKEN, World, events, git, make_project, remove, temp_root

SCRIPT = Path(launchpad.__file__).resolve()
SECRET = "s3cr3t-value-that-must-not-leak"


class DeployTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root()
        self.world = World(self.root / "world")
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "state"
        self.command_log = self.root / "commands.log"
        self.project = make_project(self.root / "projects")

    def tearDown(self) -> None:
        self.world.close()
        remove(self.root)

    def script(self, mode: str, *args: str, project: Path | None = None, stdin: str | None = None,
               credentials: bool = True, env: dict[str, str] | None = None) -> tuple[int, list[dict], str]:
        full_env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "QUIRQ_STATE_ROOT": str(self.state),
                    "QUIRQ_COMMAND_LOG_PATH": str(self.command_log), **self.world.env(),
                    **(self.world.credentials() if credentials else {}), **(env or {})}
        where = [] if project is None and mode == "sizes" else ["--project", str(project or self.project)]
        done = subprocess.run([sys.executable, str(SCRIPT), mode, *where, *args],
                              input=stdin, capture_output=True, text=True, env=full_env, timeout=120)
        return done.returncode, events(done.stdout), done.stdout + done.stderr

    def deploy(self, *args: str, **kw) -> tuple[int, list[dict], str]:
        return self.script("deploy", *args, **kw)

    def last(self, evs: list[dict]) -> dict:
        return evs[-1]

    def steps(self, evs: list[dict]) -> list[tuple[str, str]]:
        return [(e["step"], e["status"]) for e in evs if e["type"] == "step" and e["status"] != "running"]

    def config(self, project: Path | None = None) -> dict:
        return json.loads(((project or self.project) / "launchpad.json").read_text())

    def calls(self, *prefix: str) -> list[dict]:
        return [c for c in self.world.state()["calls"] if c["argv"][:len(prefix)] == list(prefix)]

    def commit(self, name: str = "change.txt", text: str = "more\n") -> str:
        (self.project / name).write_text(text)
        git("add", "-A", cwd=self.project)
        git("commit", "-q", "-m", f"Change {name}", cwd=self.project)
        return git("rev-parse", "HEAD", cwd=self.project)


class NewAppTests(DeployTestCase):
    def test_a_new_project_asks_before_creating_a_repo_then_deploys_end_to_end(self) -> None:
        code, evs, _ = self.deploy()
        self.assertEqual(code, 3)
        needs = self.last(evs)
        self.assertEqual((needs["type"], needs["step"], needs["code"], needs["action"]),
                         ("needs", "repo", "create_repo", "create_repo"))
        self.assertEqual(needs["data"], {"repo": f"{LOGIN}/hello", "reason": "The project has no GitHub remote"})
        self.assertEqual(self.world.repos, {})
        self.assertEqual(self.calls("app", "create"), [])

        secrets = json.dumps({"env": {"API_TOKEN": SECRET, "LOG_LEVEL": "debug"}})
        code, evs, output = self.deploy("--create-repo", "--secrets-stdin", stdin=secrets)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.steps(evs), [
            ("check", "done"), ("credentials", "done"), ("repo", "done"), ("app", "done"), ("prepare", "skipped"),
            ("push", "done"), ("create", "done"), ("env", "done"), ("build", "done"), ("release", "done"),
            ("url", "done")])
        done = self.last(evs)
        app = next(iter(self.world.apps().values()))
        self.assertEqual((done["type"], done["url"], done["http_status"], done["release"], done["created"]),
                         ("done", app["public_url"], 200, 1, True))
        # The repo: private, new, the project's code and Launchpad's workflow; origin never set.
        self.assertTrue(self.world.repos[f"{LOGIN}/hello"]["created"]["private"])
        files = self.world.files(f"{LOGIN}/hello")
        self.assertIn("server.js", files)
        self.assertIn(".github/workflows/launchpad-hello-build-1.yml", files)
        self.assertIn("launchpad.json", files)
        self.assertEqual(git("remote", cwd=self.project), "launchpad")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.project), self.world.head(f"{LOGIN}/hello"))
        self.assertIn("[skip ci]", git("log", "-1", "--format=%s", cwd=self.project))
        # The app: built from that repo, the token given for create only.
        self.assertEqual(app["params"], {"repoUrl": f"https://github.com/{LOGIN}/hello", "branch": "main"})
        self.assertEqual(app["port"], 3000)
        self.assertEqual([c["github_token"] for c in self.calls("app", "create")], [True])
        self.assertFalse(any(c["github_token"] for c in self.world.state()["calls"] if c["argv"][1] != "create"))
        # launchpad.json: the pointer and the names, never a value.
        config = self.config()
        self.assertEqual(config, {
            "schema": 1, "name": "hello", "app_id": app["id"], "slug": app["slug"], "url": app["public_url"],
            "workflow": "launchpad-hello-build-1.yml", "repo": {"full_name": f"{LOGIN}/hello", "remote": "launchpad", "branch": "main"},
            "port": 3000, "health_path": None, "size": "xo.medium",
            "env": [{"name": "API_TOKEN", "sensitive": True}, {"name": "LOG_LEVEL", "sensitive": False}]})
        self.assertEqual(json.loads(self.world.show(f"{LOGIN}/hello", "launchpad.json")), config)
        # The values went to Launchpad only, the secret marked sensitive.
        self.assertEqual(app["env"], {"API_TOKEN": SECRET, "LOG_LEVEL": "debug"})
        self.assertEqual(app["sensitive"], ["API_TOKEN"])
        self.assertNotIn(SECRET, output)
        log = self.command_log.read_text()
        for secret in (SECRET, TOKEN, KEY):
            self.assertNotIn(secret, log)
        self.assertNotIn(SECRET, (self.project / "launchpad.json").read_text())
        # Progress the panel shows: the run's page and instances ready.
        build = [e for e in evs if e["type"] == "progress" and e["step"] == "build"]
        self.assertTrue(all(e["data"]["run_url"].startswith("https://github.test/") for e in build))
        release = [e["data"] for e in evs if e["type"] == "progress" and e["step"] == "release"]
        self.assertIn({"release": 1, "rollout": "progressing", "app_status": "deploying", "ready": 0, "desired": 1},
                      release)
        self.assertTrue(all("elapsed" in e for e in evs if e["type"] == "step" and e["status"] != "running"))

        # Again with nothing new: nothing pushed or built, the live release reported.
        runs = len(self.world.runs)
        code, evs, output = self.deploy()
        self.assertEqual(code, 0, output)
        self.assertEqual(dict(self.steps(evs))["push"], "skipped")
        self.assertEqual(dict(self.steps(evs))["build"], "skipped")
        self.assertEqual((self.last(evs)["release"], len(self.world.runs)), (1, runs))

        # A new commit: pushed, built, rolled out as release 2.
        sha = self.commit()
        code, evs, output = self.deploy()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.world.head(f"{LOGIN}/hello"), sha)
        self.assertEqual((self.last(evs)["release"], self.last(evs)["commit"]), (2, sha[:12]))
        self.assertEqual([r["conclusion"] for r in self.world.runs_of(sha)], ["success"])

    def test_uncommitted_changes_are_named_and_not_deployed(self) -> None:
        (self.project / "server.js").write_text("changed\n")
        code, evs, output = self.deploy("--create-repo")
        self.assertEqual(code, 0, output)
        self.assertIn("1 uncommitted change in the project folder is not part of this deploy: only commits deploy.",
                      [e["message"] for e in evs if e["type"] == "warning"])
        self.assertNotEqual(self.world.show(f"{LOGIN}/hello", "server.js"), "changed")
        self.assertEqual((self.project / "server.js").read_text(), "changed\n")    # still there, still uncommitted

    def test_a_detectable_project_gets_its_dockerfile_committed_and_deployed(self) -> None:
        project = make_project(self.root / "more", "site", dockerfile=None, files={"index.html": "<h1>hi</h1>"})
        (project / "server.js").unlink()
        git("add", "-A", cwd=project)
        git("commit", "-q", "-m", "No server", cwd=project)
        code, evs, output = self.deploy("--create-repo", project=project)
        self.assertEqual(code, 0, output)
        self.assertEqual(dict(self.steps(evs))["prepare"], "done")
        self.assertIn("FROM nginx:1.27-alpine", self.world.show(f"{LOGIN}/site", "Dockerfile"))
        self.assertEqual(self.config(project)["port"], 80)

    def test_a_node_app_gets_a_dockerfile_for_the_port_it_was_given(self) -> None:
        package = {"name": "api", "scripts": {"start": "node server.js"}}
        project = make_project(self.root / "more", "api", dockerfile=None, files={
            "package.json": json.dumps(package), "package-lock.json": "{}"})
        code, evs, output = self.deploy("--create-repo", "--port=4000", project=project)
        self.assertEqual(code, 0, output)
        dockerfile = self.world.show(f"{LOGIN}/api", "Dockerfile")
        self.assertIn("RUN npm ci", dockerfile)
        self.assertIn("ENV NODE_ENV=production PORT=4000", dockerfile)
        self.assertIn("EXPOSE 4000", dockerfile)
        self.assertNotIn("RUN npm run build", dockerfile)                   # no build script, no build line
        self.assertEqual(next(iter(self.world.apps().values()))["port"], 4000)
        self.assertEqual(git("log", "--format=%s", "-3", cwd=project).splitlines()[-1], "Prepare the Launchpad deploy")

    def test_a_fastapi_app_starts_on_the_port_it_was_given(self) -> None:
        project = make_project(self.root / "more", "py", dockerfile=None, files={
            "requirements.txt": "fastapi\nuvicorn\n", "main.py": "app = FastAPI()\n"})
        (project / "server.js").unlink()
        git("add", "-A", cwd=project)
        git("commit", "-q", "-m", "Python only", cwd=project)
        code, evs, output = self.deploy("--create-repo", "--port=9000", project=project)
        self.assertEqual(code, 0, output)
        dockerfile = self.world.show(f"{LOGIN}/py", "Dockerfile")
        self.assertIn('CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]', dockerfile)
        self.assertIn("EXPOSE 9000", dockerfile)

    def test_the_origin_is_used_when_the_user_administers_it(self) -> None:
        self.world.add_repo(f"{LOGIN}/mine")
        git("remote", "add", "origin", self.world.remote_url(f"{LOGIN}/mine"), cwd=self.project)
        code, evs, output = self.deploy()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.config()["repo"], {"full_name": f"{LOGIN}/mine", "remote": "origin", "branch": "main"})
        self.assertEqual(git("remote", cwd=self.project), "origin")
        self.assertNotIn(("POST", "/user/repos"), self.world.requests)

    def test_someone_elses_origin_is_never_touched(self) -> None:
        self.world.add_repo("acme/theirs", admin=False, push=True)
        origin = self.world.remote_url("acme/theirs")
        git("remote", "add", "origin", origin, cwd=self.project)
        code, evs, _ = self.deploy()
        self.assertEqual((code, self.last(evs)["code"]), (3, "create_repo"))
        self.assertEqual(self.last(evs)["data"]["reason"], "You don't administer acme/theirs, the project's origin")
        code, evs, output = self.deploy("--create-repo", "--repo-name=hello-deploy")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.config()["repo"]["full_name"], f"{LOGIN}/hello-deploy")
        self.assertEqual(git("remote", "get-url", "origin", cwd=self.project), origin)
        self.assertIsNone(self.world.head("acme/theirs"))                       # nothing pushed there

    def test_a_taken_repo_name_holding_other_code_is_not_reused(self) -> None:
        self.world.add_repo(f"{LOGIN}/hello")
        other = make_project(self.root / "other", "hello", files={"other.txt": "someone else's code"})
        git("push", "-q", self.world.remote_url(f"{LOGIN}/hello"), "HEAD:main", cwd=other)
        code, evs, _ = self.deploy("--create-repo")
        self.assertEqual((code, self.last(evs)["code"]), (3, "repo_name_taken"))
        self.assertEqual(self.last(evs)["data"]["suggestion"], "hello-app")

    def test_a_token_without_the_workflow_scope_stops_before_anything(self) -> None:
        self.world.scopes = "gist, read:org, repo"
        code, evs, _ = self.deploy("--create-repo")
        self.assertEqual(code, 1)
        error = self.last(evs)
        self.assertEqual((error["step"], error["code"]), ("credentials", "github_workflow_scope"))
        self.assertIn("Reconnect GitHub", error["fix"])
        self.assertEqual(error["action"], "reconnect_github")
        self.assertEqual(self.world.repos, {})

    def test_a_gh_login_without_the_workflow_scope_is_fixed_with_gh(self) -> None:
        self.world.scopes = "repo, read:org"
        code, evs, _ = self.deploy("--create-repo", env={"LAUNCHPAD_GITHUB_SOURCE": "gh"})
        error = self.last(evs)
        self.assertEqual((code, error["code"], error["data"]["source"]), (1, "github_workflow_scope", "gh"))
        self.assertTrue(error["fix"].startswith("Run `gh auth refresh -h github.com -s workflow`"))
        code, evs, _ = self.script("preflight", env={"LAUNCHPAD_GITHUB_SOURCE": "gh"})
        github = self.last(evs)["preflight"]["github"]
        self.assertEqual((github["source"], github["workflow_scope"]), ("gh", False))
        self.assertIn("gh auth refresh", github["fix"])

    def test_launchpad_refusing_the_workflow_write_is_the_scope_error(self) -> None:
        self.world.fail(create_workflow=True)
        code, evs, _ = self.deploy("--create-repo")
        self.assertEqual((code, self.last(evs)["step"], self.last(evs)["code"]), (1, "create", "github_workflow_scope"))

    def test_missing_keys_say_what_to_connect(self) -> None:
        code, evs, _ = self.deploy(credentials=False)
        self.assertEqual(self.last(evs)["code"], "launchpad_key_missing")
        code, evs, _ = self.deploy(credentials=False, env={"XO_API_KEY": KEY})
        self.assertEqual(self.last(evs)["code"], "github_not_connected")
        code, evs, _ = self.deploy(env={"XO_LAUNCHPAD_BIN": str(self.root / "nowhere")})
        self.assertEqual(self.last(evs)["code"], "launchpad_cli_missing")
        code, evs, _ = self.deploy(env={"GITHUB_TOKEN": "gho_wrong"})
        self.assertEqual(self.last(evs)["code"], "github_token_invalid")

    def test_a_rejected_key_names_the_launchpad_it_was_sent_to(self) -> None:
        code, evs, _ = self.deploy("--create-repo", env={"XO_API_KEY": "ak_other_instance",
                                                            "XO_LAUNCHPAD_URL": "https://lp.example"})
        error = self.last(evs)
        self.assertEqual((code, error["code"]), (1, "launchpad_key_invalid"))
        self.assertIn("https://lp.example", error["message"])
        self.assertIn("XO_LAUNCHPAD_URL", error["fix"])        # not "sign in again": the key comes from .env

    def test_an_address_without_launchpad_says_so_and_names_it(self) -> None:
        self.world.fail(no_launchpad=True)
        code, evs, _ = self.deploy("--create-repo", env={"XO_LAUNCHPAD_URL": "https://api.example"})
        error = self.last(evs)
        self.assertEqual((code, error["code"]), (1, "launchpad_not_here"))
        self.assertIn("https://api.example", error["message"])
        code, evs, _ = self.script("preflight")
        self.assertEqual(self.last(evs)["preflight"]["launchpad"]["error"], "launchpad_not_here")

    def test_a_project_that_needs_an_agent_is_refused_with_its_reasons(self) -> None:
        project = make_project(self.root / "more", "gopher", dockerfile=None, files={"go.mod": "module x"})
        (project / "server.js").unlink()
        code, evs, _ = self.deploy(project=project)
        self.assertEqual((code, self.last(evs)["code"]), (1, "needs_agent"))
        self.assertTrue(self.last(evs)["data"]["reasons"])

    def test_the_port_is_asked_for_when_unknown(self) -> None:
        project = make_project(self.root / "more", "noport", dockerfile="FROM scratch\n", files={"server.js": "x"})
        code, evs, _ = self.deploy(project=project)
        self.assertEqual((code, self.last(evs)["type"], self.last(evs)["code"]), (3, "needs", "port_missing"))


class SizeTests(DeployTestCase):
    """The machine size and the disk: chosen for a new app, fixed after (the disk only grows)."""

    def created(self) -> dict:
        return next(iter(self.world.apps().values()))

    def test_a_new_app_gets_the_size_and_disk_asked_for(self) -> None:
        code, evs, output = self.deploy("--create-repo", "--size=small", "--storage-path=/data", "--storage-size=5")
        self.assertEqual(code, 0, output)
        app = self.created()
        self.assertEqual((app["size"], app["storage"]), ("xo.small", {"path": "/data", "size": "5Gi"}))
        argv = self.calls("app", "create")[0]["argv"]
        self.assertEqual(argv[argv.index("--size") + 1], "xo.small")
        self.assertEqual((self.config()["size"], self.config()["storage"]), ("xo.small", {"path": "/data", "size_gb": 5}))
        created = next(e for e in evs if e["type"] == "progress" and e["step"] == "create")
        self.assertEqual(created["data"]["size"], "xo.small")
        self.assertIn("xo.small, 1 CPU and 2 GB memory", created["message"])

    def test_without_a_size_the_templates_default_is_used_and_recorded(self) -> None:
        code, evs, output = self.deploy("--create-repo", "--storage-path=/data")
        self.assertEqual(code, 0, output)
        argv = self.calls("app", "create")[0]["argv"]
        self.assertNotIn("--size", argv)
        self.assertEqual(argv[argv.index("--storage-size") + 1], "2")         # both flags, for any CLI release
        self.assertEqual((self.config()["size"], self.config()["storage"]), ("xo.medium", {"path": "/data", "size_gb": 2}))

    def test_a_size_launchpad_refuses_or_does_not_know_is_said_so(self) -> None:
        code, evs, _ = self.deploy("--create-repo", "--size=mini")
        error = self.last(evs)
        self.assertEqual((code, error["step"], error["code"], error["action"]),
                         (1, "create", "size_not_allowed", "edit_settings"))
        code, evs, _ = self.deploy("--create-repo", "--size=huge")
        self.assertEqual((self.last(evs)["step"], self.last(evs)["code"]), ("check", "size_not_allowed"))
        code, evs, _ = self.deploy("--create-repo", "--storage-size=5")
        self.assertEqual(self.last(evs)["code"], "config_invalid")

    def test_a_linked_app_keeps_its_size_and_its_disk_only_grows(self) -> None:
        code, _evs, output = self.deploy("--create-repo", "--storage-path=/data", "--storage-size=3")
        self.assertEqual(code, 0, output)
        app_id = self.created()["id"]
        code, evs, output = self.deploy("--size=large", "--storage-path=/data", "--storage-size=8")
        self.assertEqual(code, 0, output)
        warnings = [e["message"] for e in evs if e["type"] == "warning"]
        self.assertTrue(any("stays xo.medium" in w and "Recreate the app" in w for w in warnings), warnings)
        app = self.world.apps()[app_id]
        self.assertEqual((app["size"], app["storage"]["size"]), ("xo.medium", "8Gi"))
        self.assertEqual((self.config()["size"], self.config()["storage"]), ("xo.medium", {"path": "/data", "size_gb": 8}))
        code, evs, output = self.deploy("--storage-path=/data", "--storage-size=4")
        self.assertEqual(code, 0, output)
        self.assertIn("The app's disk stays 8 GB: a disk can grow, never shrink.",
                      [e["message"] for e in evs if e["type"] == "warning"])
        self.assertEqual(self.world.apps()[app_id]["storage"]["size"], "8Gi")

    def test_preflight_and_sizes_give_the_catalog(self) -> None:
        code, evs, output = self.script("preflight")
        self.assertEqual(code, 0, output)
        sizes = self.last(evs)["preflight"]["launchpad"]["sizes"]
        self.assertEqual((sizes["default"], sizes["allowed"], sizes["source"], sizes["fixed_after_create"]),
                         ("xo.medium", ["xo.small", "xo.medium", "xo.large"], "launchpad", True))
        self.assertEqual(sizes["tiers"][0], {"name": "xo.mini", "label": "Mini", "cpu": 0.5, "memory_gb": 1})
        code, evs, output = self.script("sizes", project=None)
        self.assertEqual((code, self.last(evs)["sizes"]["default"]), (0, "xo.medium"))
        self.world.fail(no_sizes_command=True)
        code, evs, output = self.script("sizes", project=None)
        fallback = self.last(evs)["sizes"]
        self.assertEqual((fallback["source"], fallback["default"]), ("flow.json", None))
        self.assertEqual([s["name"] for s in fallback["tiers"]], launchpad.size_names())

    def test_a_linked_apps_preflight_shows_its_size(self) -> None:
        self.assertEqual(self.deploy("--create-repo", "--size=large")[0], 0)
        code, evs, output = self.script("preflight")
        app = self.last(evs)["preflight"]["launchpad"]["app"]
        self.assertEqual((app["size"], app["size_label"]), ("xo.large", "xo.large, 2 CPU and 8 GB memory"))


class BuildAndReleaseFailureTests(DeployTestCase):
    def test_a_failed_build_names_the_step_the_run_and_the_log(self) -> None:
        self.world.build_fails = True
        code, evs, _ = self.deploy("--create-repo")
        error = self.last(evs)
        self.assertEqual((code, error["step"], error["code"], error["action"]), (1, "build", "build_failed",
                                                                                 "open_build_log"))
        self.assertEqual(error["data"]["failed_step"], "Build and push")
        self.assertTrue(error["data"]["run_url"].startswith("https://github.test/octo/hello/actions/runs/"))
        self.assertIn("npm ERR! Missing script: start", error["data"]["log_tail"])
        self.assertNotIn("2026-11-05T", error["data"]["log_tail"])

    def test_a_release_failing_its_health_check_says_which_port(self) -> None:
        self.world.fail(health=True)
        code, evs, _ = self.deploy("--create-repo")
        error = self.last(evs)
        self.assertEqual((code, error["step"], error["code"]), (1, "release", "health_check_failed"))
        self.assertEqual((error["data"]["kind"], error["data"]["port"], error["data"]["exposed_ports"]),
                         ("port", 3000, [3000]))
        self.assertIn("0.0.0.0:3000", error["fix"])


class LinkedAppTests(DeployTestCase):
    def setUp(self) -> None:
        super().setUp()
        code, _evs, output = self.deploy("--create-repo")
        self.assertEqual(code, 0, output)
        self.app = next(iter(self.world.apps().values()))

    def test_a_deleted_app_asks_then_is_recreated_and_its_old_workflow_removed(self) -> None:
        self.world.delete_app(self.app["id"])
        code, evs, _ = self.deploy()
        needs = self.last(evs)
        self.assertEqual((code, needs["code"], [o["id"] for o in needs["options"]]),
                         (3, "app_missing", ["recreate", "unlink"]))
        code, evs, output = self.deploy("--recreate")
        self.assertEqual(code, 0, output)
        app = next(iter(self.world.apps().values()))
        self.assertNotEqual(app["id"], self.app["id"])
        self.assertEqual(self.config()["app_id"], app["id"])
        flows = [f for f in self.world.files(f"{LOGIN}/hello") if f.startswith(".github/workflows/")]
        self.assertEqual(flows, [".github/workflows/launchpad-hello-build-2.yml"])
        self.assertEqual(self.config()["workflow"], "launchpad-hello-build-2.yml")
        # Launchpad's commit carried both workflows, so GitHub ran both; the deploy followed the new app's.
        followed = [e["data"]["run_id"] for e in evs if e["type"] == "progress" and e["step"] == "build"]
        new_run = next(r for r in self.world.runs.values() if r["path"].endswith("launchpad-hello-build-2.yml"))
        self.assertEqual(set(followed), {new_run["id"]})

    def test_another_apps_workflow_stays_when_a_new_app_is_created(self) -> None:
        # Unlinked with forget, the first app keeps running on Launchpad: its
        # workflow still builds it and is not the new app's to remove.
        code, _evs, output = self.script("unlink", "--forget")
        self.assertEqual(code, 0, output)
        self.commit()
        code, _evs, output = self.deploy("--name=hello-two")
        self.assertEqual(code, 0, output)
        flows = sorted(f for f in self.world.files(f"{LOGIN}/hello") if f.startswith(".github/workflows/"))
        self.assertEqual(len(flows), 2, flows)
        self.assertIn(".github/workflows/launchpad-hello-build-1.yml", flows)

    def test_the_projects_git_hooks_never_run_and_never_see_the_token(self) -> None:
        self.commit()
        marker = self.root / "hooks-ran"
        for hook in ("pre-push", "post-commit", "post-merge", "reference-transaction"):
            path = self.project / ".git" / "hooks" / hook
            path.write_text(f"#!/bin/sh\necho {hook} >> {marker}\n")
            path.chmod(0o755)
        code, _evs, output = self.deploy("--rebuild")
        self.assertEqual(code, 0, output)
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")

    def test_a_deleted_config_offers_the_live_app_then_links_and_deploys(self) -> None:
        git("rm", "-q", "launchpad.json", cwd=self.project)
        git("commit", "-q", "-m", "Lose the config", cwd=self.project)
        code, evs, _ = self.deploy()
        needs = self.last(evs)
        self.assertEqual((code, needs["code"], needs["data"]["app"]["id"]), (3, "link_existing", self.app["id"]))
        code, evs, output = self.script("link", f"--app={self.app['id']}")
        self.assertEqual(code, 0, output)
        result = self.last(evs)
        self.assertEqual(result["config"]["app_id"], self.app["id"])
        self.assertEqual(result["config"]["repo"], {"full_name": f"{LOGIN}/hello", "remote": "launchpad",
                                                    "branch": "main"})
        self.assertEqual(git("log", "-1", "--format=%s", cwd=self.project), "Link Launchpad app hello")
        code, evs, output = self.deploy()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.last(evs)["release"], 2)

    def test_another_folder_with_the_committed_config_asks_once_then_deploys(self) -> None:
        """A copy of the project elsewhere (another machine, or a fresh clone)
        carries launchpad.json but not the Space's own record of the link, so
        its first deploy asks before pushing; once confirmed, it doesn't."""
        clone = self.root / "elsewhere"
        git("clone", "-q", self.world.remote_url(f"{LOGIN}/hello"), str(clone))
        git("remote", "remove", "origin", cwd=clone)                 # a copy without the launchpad remote
        (clone / "more.txt").write_text("from the other machine\n")
        git("add", "-A", cwd=clone)
        git("commit", "-q", "-m", "Elsewhere", cwd=clone)
        code, evs, _ = self.deploy(project=clone)
        self.assertEqual((code, self.last(evs)["code"]), (3, "confirm_repo"))
        code, evs, output = self.deploy(f"--use-repo={LOGIN}/hello", project=clone)
        self.assertEqual(code, 0, output)
        self.assertEqual(git("remote", cwd=clone), "launchpad")
        self.assertEqual(self.last(evs)["release"], 2)
        code, evs, output = self.deploy("--rebuild", project=clone)       # its remote, and the record: no question
        self.assertEqual(code, 0, output)

    def test_a_decoy_copying_the_owners_app_and_repo_is_asked_about(self) -> None:
        """A cloned repo whose launchpad.json copies the owner's real app id and
        repo (from the owner's own public repo, say) never pushes there
        unasked: the Space's record names this project's folder, not the decoy."""
        decoy = make_project(self.root / "cloned", "cool-demo")
        self.world.add_repo("acme/cool-demo", admin=False, push=False)
        git("remote", "add", "origin", self.world.remote_url("acme/cool-demo"), cwd=decoy)
        theirs = {**self.config(), "name": self.config()["name"]}
        (decoy / "launchpad.json").write_text(json.dumps(theirs))
        git("add", "-A", cwd=decoy)
        git("commit", "-q", "-m", "A decoy", cwd=decoy)
        before = self.world.head(f"{LOGIN}/hello")
        code, evs, _ = self.deploy(project=decoy)
        self.assertEqual((code, self.last(evs)["code"]), (3, "confirm_repo"))
        self.assertEqual(self.world.head(f"{LOGIN}/hello"), before, "nothing pushed to the owner's repo")
        code, evs, output = self.deploy("--rebuild")                      # the owner's own folder still deploys
        self.assertEqual(code, 0, output)

    def test_a_changed_port_is_applied_to_the_app_and_the_config(self) -> None:
        code, evs, output = self.deploy("--port=8080", "--health-path=/healthz")
        self.assertEqual(code, 0, output)
        app = self.world.apps()[self.app["id"]]
        self.assertEqual((app["port"], app["health_path"]), (8080, "/healthz"))
        self.assertEqual((self.config()["port"], self.config()["health_path"]), (8080, "/healthz"))
        self.assertEqual(git("log", "-1", "--format=%s", cwd=self.project), "Prepare the Launchpad deploy")

    def test_another_branch_is_refused(self) -> None:
        git("checkout", "-q", "-b", "feature", cwd=self.project)
        code, evs, _ = self.deploy()
        self.assertEqual((code, self.last(evs)["code"]), (1, "wrong_branch"))

    def test_github_ahead_with_other_commits_must_be_pulled(self) -> None:
        other = self.root / "other"
        git("clone", "-q", self.world.remote_url(f"{LOGIN}/hello"), str(other))
        (other / "theirs.txt").write_text("x")
        git("add", "-A", cwd=other)
        git("commit", "-q", "-m", "Theirs", cwd=other)
        git("push", "-q", "origin", "HEAD:main", cwd=other)
        code, evs, _ = self.deploy()
        self.assertEqual((code, self.last(evs)["code"]), (1, "remote_ahead"))
        self.commit()
        code, evs, _ = self.deploy()
        self.assertEqual((code, self.last(evs)["code"]), (1, "push_rejected"))

    def test_rebuild_builds_the_same_code_again(self) -> None:
        code, evs, output = self.deploy("--rebuild")
        self.assertEqual(code, 0, output)
        self.assertEqual(git("log", "-1", "--format=%s", cwd=self.project), "Rebuild on Launchpad")
        self.assertEqual(self.last(evs)["release"], 2)

    def test_watch_reports_the_app_and_its_live_release(self) -> None:
        code, evs, output = self.script("watch")
        self.assertEqual(code, 0, output)
        status = evs[0]
        self.assertEqual((status["type"], status["app"]["id"], status["release"]["number"]),
                         ("status", self.app["id"], 1))
        self.assertEqual(self.steps(evs), [("build", "skipped"), ("release", "done"), ("url", "done")])
        self.assertEqual((self.last(evs)["type"], self.last(evs)["http_status"]), ("done", 200))

    def test_preflight_reports_github_the_repo_and_the_app_without_changing_anything(self) -> None:
        before = len(self.world.state()["calls"])
        code, evs, output = self.script("preflight")
        self.assertEqual(code, 0, output)
        preflight = self.last(evs)["preflight"]
        self.assertEqual(preflight["github"], {"connected": True, "source": "connections", "login": LOGIN,
                                               "scopes_known": True,
                                               "scopes": ["gist", "read:org", "repo", "workflow"],
                                               "workflow_scope": True})
        self.assertEqual({k: preflight["repo"][k] for k in ("mode", "full_name", "reachable", "can_push")},
                         {"mode": "recorded", "full_name": f"{LOGIN}/hello", "reachable": True, "can_push": True})
        app = preflight["launchpad"]["app"]
        self.assertEqual((app["exists"], app["release"], app["rollout"], app["ready"], app["desired"]),
                         (True, 1, "ready", 1, 1))
        argv = [c["argv"][:2] for c in self.world.state()["calls"][before:]]
        self.assertEqual(argv, [["app", "status"]])
        self.assertEqual(self.world.repos.keys(), {f"{LOGIN}/hello"})

    def test_apps_lists_the_matching_app_first(self) -> None:
        code, evs, output = self.script("apps")
        self.assertEqual(code, 0, output)
        result = self.last(evs)
        self.assertEqual((result["linked"], result["apps"][0]["id"], result["apps"][0]["match"]),
                         (self.app["id"], self.app["id"], ["name", "repo"]))

    def test_unlink_keeps_the_settings_and_forget_removes_the_file(self) -> None:
        code, evs, output = self.script("unlink")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.last(evs)["unlinked"], {"app_id": self.app["id"], "name": "hello"})
        config = self.config()
        self.assertNotIn("app_id", config)
        self.assertEqual(config["repo"]["full_name"], f"{LOGIN}/hello")
        self.assertEqual(launchpad.check(self.project)["state"], "ready")
        self.assertIn(self.app["id"], self.world.apps())                        # the app itself stays
        code, evs, output = self.script("unlink", "--forget")
        self.assertEqual(code, 0, output)
        self.assertFalse((self.project / "launchpad.json").exists())
        self.assertEqual(git("status", "--porcelain", cwd=self.project), "")
        code, evs, _ = self.script("unlink")
        self.assertEqual((code, self.last(evs)["code"]), (1, "not_linked"))

    def test_link_refuses_a_second_app(self) -> None:
        self.world.update(lambda s: s["apps"].__setitem__("other", {**s["apps"][self.app["id"]], "id": "other",
                                                                    "name": "other", "slug": "other-xo"}))
        code, evs, _ = self.script("link", "--app=other")
        self.assertEqual((code, self.last(evs)["code"]), (1, "already_linked"))


class RecordedRepoTests(DeployTestCase):
    """launchpad.json is the project author's file: in a project cloned from
    someone else's repo it can name any repo the owner's token can push to."""

    def setUp(self) -> None:
        super().setUp()
        self.world.add_repo(f"{LOGIN}/private")                      # the owner's, unrelated to this project
        self.world.add_repo("acme/cool-demo", admin=False, push=False)
        git("remote", "add", "origin", self.world.remote_url("acme/cool-demo"), cwd=self.project)
        (self.project / "launchpad.json").write_text(json.dumps(
            {"schema": 1, "name": "cool-demo", "repo": {"full_name": f"{LOGIN}/private", "branch": "main"}}))
        git("add", "-A", cwd=self.project)
        git("commit", "-q", "-m", "Their launchpad.json", cwd=self.project)

    def checked(self) -> dict:
        old = os.environ.get("LAUNCHPAD_GITHUB_GIT")
        os.environ.update(self.world.env())
        try:
            return launchpad.check(self.project)
        finally:
            if old is None:
                os.environ.pop("LAUNCHPAD_GITHUB_GIT", None)
            else:
                os.environ["LAUNCHPAD_GITHUB_GIT"] = old

    def test_the_check_names_the_repo_and_warns_it_is_not_the_projects_own(self) -> None:
        result = self.checked()
        self.assertEqual(result["state"], "ready")
        self.assertIn(f"It deploys to the GitHub repo {LOGIN}/private (from launchpad.json).", result["summary"])
        self.assertFalse(result["fields"]["repo"]["own"])
        self.assertTrue(any(f"names the GitHub repo {LOGIN}/private" in w for w in result["warnings"]))

    def test_it_is_pushed_to_only_once_the_owner_confirms_that_very_repo(self) -> None:
        code, evs, _ = self.deploy()
        needs = self.last(evs)
        self.assertEqual((code, needs["type"], needs["code"]), (3, "needs", "confirm_repo"))
        self.assertEqual(needs["data"]["repo"], f"{LOGIN}/private")
        self.assertIn(f"{LOGIN}/private", needs["message"])
        self.assertIsNone(self.world.head(f"{LOGIN}/private"))      # nothing pushed
        self.assertEqual(self.world.apps(), {})                    # no app, so Launchpad never got the token
        code, evs, _ = self.deploy("--use-repo=acme/cool-demo")      # a confirmation of another repo is none
        self.assertEqual((code, self.last(evs)["code"]), (3, "confirm_repo"))
        self.assertIsNone(self.world.head(f"{LOGIN}/private"))
        code, evs, output = self.deploy(f"--use-repo={LOGIN}/private")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.world.head(f"{LOGIN}/private"), git("rev-parse", "HEAD", cwd=self.project))
        self.assertTrue(self.checked()["fields"]["repo"]["own"], "confirmed once, it is a remote of the project now")
        code, evs, output = self.deploy("--rebuild")
        self.assertEqual(code, 0, output)


THEIR_SLUG = "todo-xo-ab12cd3"
THEIR_FLOW = f".github/workflows/launchpad-{THEIR_SLUG}.yml"


def their_workflow(slug: str) -> str:
    """A workflow as Launchpad writes it (registry project = the app's slug)."""
    return (f"# Managed by XO Launchpad for app {slug}. Edits are overwritten when the app is reconnected.\n"
            f"name: Launchpad {slug}\n\non:\n  push:\n    branches: [\"main\"]\n  workflow_dispatch:\n\n"
            f"env:\n  IMAGE: registry.test/{slug}/app\n")


class OtherAppsTests(DeployTestCase):
    """A push runs every Launchpad workflow in the pushed commit, and Launchpad
    deploys what a workflow pushes whoever pushed: a push that would deploy an
    app outside the owner's account waits for the owner."""

    def add_their_workflow(self, project: Path, path: str = THEIR_FLOW, text: str | None = None) -> None:
        target = project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text if text is not None else their_workflow(THEIR_SLUG))
        git("add", "-A", cwd=project)
        git("commit", "-q", "-m", "Their Launchpad workflow", cwd=project)

    def test_a_copied_config_is_not_called_deleted_and_recreating_asks_before_deploying_their_app(self) -> None:
        """A friend's repo, deployed with Launchpad from their account, cloned by
        the owner who can push to it: their launchpad.json and workflow come
        with the code."""
        self.world.add_repo("acme/todo", admin=False, push=True)
        git("remote", "add", "origin", self.world.remote_url("acme/todo"), cwd=self.project)
        (self.project / "launchpad.json").write_text(json.dumps({
            "schema": 1, "name": "todo", "app_id": "5d1f6a2e-8c3b-4a7d-9e0f-1b2c3d4e5f60", "slug": THEIR_SLUG,
            "url": f"https://{THEIR_SLUG}.apps.test", "workflow": os.path.basename(THEIR_FLOW),
            "repo": {"full_name": "acme/todo", "remote": "origin", "branch": "main"}, "port": 3000,
            "health_path": None}))
        self.add_their_workflow(self.project)
        git("push", "-q", "origin", "HEAD:main", cwd=self.project)
        self.commit()                                                   # the owner's own change
        theirs = self.world.head("acme/todo")

        code, evs, _ = self.deploy()
        needs = self.last(evs)
        self.assertEqual((code, needs["code"]), (3, "app_missing"))
        self.assertIn("isn't in your Launchpad account", needs["message"])
        self.assertNotIn("no longer exists", needs["message"])

        code, evs, _ = self.deploy("--recreate")
        needs = self.last(evs)
        self.assertEqual((code, needs["type"], needs["step"], needs["code"], needs["action"]),
                         (3, "needs", "push", "other_apps", "allow_apps"))
        self.assertEqual(needs["data"], {"repo": "acme/todo", "branch": "main", "apps": [
            {"id": THEIR_SLUG, "slug": THEIR_SLUG, "workflow": os.path.basename(THEIR_FLOW)}]})
        self.assertIn(THEIR_SLUG, needs["message"])
        self.assertEqual(self.world.head("acme/todo"), theirs, "nothing pushed to their branch")
        self.assertEqual(self.calls("app", "create"), [])

        code, evs, _ = self.deploy("--recreate", "--allow-app=some-other-app")   # agreeing to another app is no answer
        self.assertEqual((code, self.last(evs)["code"]), (3, "other_apps"))
        self.assertEqual(self.world.head("acme/todo"), theirs)

        code, evs, output = self.deploy("--recreate", f"--allow-app={THEIR_SLUG}")
        self.assertEqual(code, 0, output)
        self.assertNotEqual(self.world.head("acme/todo"), theirs)
        warnings = [e["message"] for e in evs if e["type"] == "warning"]
        self.assertTrue(any(THEIR_SLUG in w and "as you agreed" in w for w in warnings), warnings)
        # Their workflow is theirs to remove: recreating doesn't delete it without this machine's record.
        self.assertIn(THEIR_FLOW, self.world.files("acme/todo"))
        self.assertTrue(any(THEIR_FLOW in w and "stays" in w for w in warnings), warnings)

        self.commit("again.txt")                                       # the agreement held for that deploy only
        code, evs, _ = self.deploy()
        self.assertEqual((code, self.last(evs)["code"]), (3, "other_apps"))

    def test_only_apps_outside_the_account_are_asked_about(self) -> None:
        """The owner's own repo and app, shared with someone who added their
        app's workflow (and a hand-written one that doesn't say its app)."""
        code, _evs, output = self.deploy("--create-repo")
        self.assertEqual(code, 0, output)
        self.add_their_workflow(self.project)
        self.add_their_workflow(self.project, ".github/workflows/launchpad-mystery.yml", "name: build\non: push\n")
        before = self.world.head(f"{LOGIN}/hello")
        code, evs, _ = self.deploy()
        needs = self.last(evs)
        self.assertEqual((code, needs["code"]), (3, "other_apps"))
        self.assertEqual(needs["data"]["apps"], [
            {"id": "launchpad-mystery.yml", "slug": None, "workflow": "launchpad-mystery.yml"},
            {"id": THEIR_SLUG, "slug": THEIR_SLUG, "workflow": os.path.basename(THEIR_FLOW)}])
        self.assertEqual(self.world.head(f"{LOGIN}/hello"), before)
        code, evs, _ = self.deploy(f"--allow-app={THEIR_SLUG}")              # every app must be agreed to
        self.assertEqual((code, self.last(evs)["code"]), (3, "other_apps"))
        code, evs, output = self.deploy(f"--allow-app={THEIR_SLUG}", "--allow-app=launchpad-mystery.yml")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.world.head(f"{LOGIN}/hello"), git("rev-parse", "HEAD", cwd=self.project))

    def test_the_owners_own_apps_never_ask_and_cost_no_extra_call(self) -> None:
        code, _evs, output = self.deploy("--create-repo")
        self.assertEqual(code, 0, output)
        lists = len(self.calls("app", "list"))
        self.commit()
        code, _evs, output = self.deploy()                         # the linked app's own workflow
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.calls("app", "list")), lists, "the linked app's workflow needs no lookup")
        # Another app of the owner's, built from the same repo: in the account, so no question.
        code, _evs, output = self.script("unlink", "--forget")
        self.assertEqual(code, 0, output)
        self.commit("third.txt")
        code, _evs, output = self.deploy("--name=hello-two")
        self.assertEqual(code, 0, output)

    def test_a_launchpad_that_cannot_list_the_apps_stops_before_the_push(self) -> None:
        code, _evs, output = self.deploy("--create-repo")
        self.assertEqual(code, 0, output)
        self.add_their_workflow(self.project)
        before = self.world.head(f"{LOGIN}/hello")
        self.world.fail(app_list=True)
        code, evs, _ = self.deploy()
        self.assertEqual((code, self.last(evs)["step"], self.last(evs)["code"]), (1, "push", "launchpad_unreachable"))
        self.assertEqual(self.world.head(f"{LOGIN}/hello"), before)


class WorkflowSlugTests(unittest.TestCase):
    def test_the_image_decides_then_the_header_then_the_name(self) -> None:
        real = their_workflow("space-hub-xo-2l0axm5").replace("registry.test", "registry.xo.builders")
        self.assertEqual(launchpad.workflow_slug(real), "space-hub-xo-2l0axm5")
        self.assertEqual(launchpad.workflow_slug("name: Launchpad a\nenv:\n  IMAGE: r.test/b/app\n"), "b")
        self.assertEqual(launchpad.workflow_slug("# Managed by XO Launchpad for app c.\nname: Launchpad d\n"), "c")
        self.assertEqual(launchpad.workflow_slug("name: launchpad e-xo-0001\non: push\n"), "e-xo-0001")
        self.assertIsNone(launchpad.workflow_slug("name: build\non: push\n"))
        self.assertIsNone(launchpad.workflow_slug(None))


if __name__ == "__main__":
    unittest.main()

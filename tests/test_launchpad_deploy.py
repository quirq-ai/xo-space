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
        done = subprocess.run([sys.executable, str(SCRIPT), mode, "--project", str(project or self.project), *args],
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
            "port": 3000, "health_path": None,
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

    def test_another_machine_with_the_committed_config_just_deploys(self) -> None:
        clone = self.root / "elsewhere"
        git("clone", "-q", self.world.remote_url(f"{LOGIN}/hello"), str(clone))
        git("remote", "remove", "origin", cwd=clone)                 # a copy without the launchpad remote
        (clone / "more.txt").write_text("from the other machine\n")
        git("add", "-A", cwd=clone)
        git("commit", "-q", "-m", "Elsewhere", cwd=clone)
        code, evs, output = self.deploy(project=clone)
        self.assertEqual(code, 0, output)
        self.assertEqual(git("remote", cwd=clone), "launchpad")
        self.assertEqual(self.last(evs)["release"], 2)

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


if __name__ == "__main__":
    unittest.main()

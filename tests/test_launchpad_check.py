"""The Launchpad deploy script's check (services/launchpad/launchpad.py):
reading a project's files only, it says how deployable the project is and
fills in the deploy form. flow.json's own consistency is checked here too."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from services.launchpad import launchpad
from tests.launchpad_kit import git, make_project, remove, temp_root


class CheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root()

    def tearDown(self) -> None:
        remove(self.root)

    def check(self, project: Path) -> dict:
        return launchpad.check(project)


class FlowDefinitionTests(unittest.TestCase):
    def test_every_step_failure_and_decision_is_defined(self) -> None:
        flow = launchpad.flow()
        failures = set(flow["failures"])
        for step in flow["steps"]:
            self.assertTrue(step["title"] and step["rule"], step["id"])
            self.assertEqual([c for c in step["failures"] if c not in failures], [], step["id"])
        self.assertEqual(set(flow["decisions"]) - failures, set())
        self.assertEqual(set(flow["states"]), {"linked", "ready", "detectable", "needs-agent", "not-deployable"})
        self.assertEqual(list(launchpad.SUMMARIES), list(flow["states"]))

    def test_every_code_the_script_raises_is_in_flow_json(self) -> None:
        source = Path(launchpad.__file__).read_text()
        import re
        raised = set(re.findall(r'Stop\(\s*"([a-z_]+)"', source)) | set(re.findall(r'\.stop\("([a-z_]+)"\)', source))
        self.assertEqual(sorted(raised - set(launchpad.flow()["failures"])), [])

    def test_the_example_config_is_valid_and_the_templates_render(self) -> None:
        flow = launchpad.flow()
        self.assertEqual(launchpad.validate_config(flow["config"]["example"]), [])
        values = {"node": "24", "manifests": "package.json", "install": "npm ci", "build": "", "start": '["npm", "start"]',
                  "port": "3000", "out": "dist", "python": "3.12"}
        for name in flow["templates"]:
            text = launchpad.render_template(name, values)
            self.assertNotIn("{{", text)
            self.assertTrue(text.endswith("\n"))
        self.assertNotIn("RUN \n", launchpad.render_template("node", values))      # empty build: line left out


class StateTests(CheckTestCase):
    def test_a_dockerfile_with_expose_is_ready(self) -> None:
        result = self.check(make_project(self.root))
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["fields"]["port"], {"value": 3000, "source": "Dockerfile EXPOSE", "confident": True})
        self.assertEqual(result["fields"]["name"], {"value": "hello", "source": "the project folder"})
        self.assertEqual(result["fields"]["repo"]["branch"], "main")
        self.assertEqual(result["blockers"], [])

    def test_a_dockerfile_without_a_port_is_detectable_with_the_port_from_the_code(self) -> None:
        result = self.check(make_project(self.root, dockerfile="FROM node:24-slim\nCMD node server.js\n"))
        self.assertEqual(result["state"], "detectable")
        self.assertEqual(result["fields"]["port"]["value"], 3000)
        self.assertFalse(result["fields"]["port"]["confident"])
        self.assertIn("confirm", " ".join(result["reasons"]))

    def test_a_node_app_with_a_start_script_gets_a_generated_dockerfile(self) -> None:
        package = {"name": "web", "scripts": {"start": "node server.js", "build": "tsc"}, "engines": {"node": ">=20"}}
        project = make_project(self.root, dockerfile=None, files={
            "package.json": json.dumps(package), "package-lock.json": "{}",
            "server.js": "const port = process.env.PORT || 4000;\napp.listen(port)\n",
            "src/health.js": "app.get('/healthz', ok)\n"})
        result = self.check(project)
        self.assertEqual(result["state"], "detectable")
        self.assertEqual(result["build"]["template"], "node")
        self.assertEqual(result["fields"]["port"]["value"], 4000)
        preview = result["build"]["dockerfile_preview"]
        self.assertIn("FROM node:20-slim", preview)
        self.assertIn("COPY package.json package-lock.json ./", preview)
        self.assertIn("RUN npm ci", preview)
        self.assertIn("RUN npm run build", preview)
        self.assertIn('CMD ["npm", "start"]', preview)
        self.assertIn("EXPOSE 4000", preview)

    def test_a_vite_app_is_built_and_served_as_a_static_site(self) -> None:
        package = {"scripts": {"dev": "vite", "build": "vite build"}, "devDependencies": {"vite": "^6"}}
        project = make_project(self.root, dockerfile=None, files={"package.json": json.dumps(package),
                                                                  "yarn.lock": "", "index.html": "<html>"})
        (project / "server.js").unlink()
        result = self.check(project)
        self.assertEqual((result["state"], result["build"]["template"]), ("detectable", "node-static"))
        self.assertIn("COPY --from=build /app/dist /usr/share/nginx/html", result["build"]["dockerfile_preview"])
        self.assertIn("yarn install --frozen-lockfile", result["build"]["dockerfile_preview"])
        self.assertEqual(result["fields"]["port"]["value"], 80)

    def test_a_static_site_is_served_by_nginx_without_its_git_folder(self) -> None:
        project = make_project(self.root, dockerfile=None, files={"index.html": "<h1>hi</h1>", "app.css": ""})
        (project / "server.js").unlink()
        result = self.check(project)
        self.assertEqual(result["build"]["template"], "static")
        self.assertIn("rm -rf .git .xo", result["build"]["dockerfile_preview"])

    def test_index_html_beside_server_code_needs_an_agent(self) -> None:
        project = make_project(self.root, dockerfile=None, files={"index.html": "<h1>hi</h1>", "serve.py": "x"})
        self.assertEqual(self.check(project)["state"], "needs-agent")

    def test_a_fastapi_app_starts_with_uvicorn(self) -> None:
        project = make_project(self.root, dockerfile=None, files={
            "requirements.txt": "fastapi\nuvicorn[standard]\n", "main.py": "api = FastAPI()\n@api.get('/health')\n",
            ".python-version": "3.11.9\n"})
        (project / "server.js").unlink()
        result = self.check(project)
        self.assertEqual((result["state"], result["build"]["template"]), ("detectable", "python"))
        self.assertIn('CMD ["uvicorn", "main:api", "--host", "0.0.0.0", "--port", "8000"]',
                      result["build"]["dockerfile_preview"])
        self.assertIn("FROM python:3.11-slim", result["build"]["dockerfile_preview"])
        self.assertEqual(result["fields"]["health_path"]["value"], "/health")

    def test_an_unknown_stack_needs_an_agent_and_says_why(self) -> None:
        project = make_project(self.root, dockerfile=None, files={"go.mod": "module x\n", "main.go": "package main"})
        (project / "server.js").unlink()
        result = self.check(project)
        self.assertEqual(result["state"], "needs-agent")
        self.assertIn("not a stack the script knows", result["reasons"][0])

    def test_a_compose_file_and_nested_dockerfiles_are_reasons(self) -> None:
        project = make_project(self.root, dockerfile=None, files={"docker-compose.yml": "services: {}",
                                                                  "api/Dockerfile": "FROM x", "web/Dockerfile": "FROM y"})
        (project / "server.js").unlink()
        reasons = " ".join(self.check(project)["reasons"])
        self.assertIn("docker-compose.yml describes several services", reasons)
        self.assertIn("api/Dockerfile", reasons)

    def test_a_folder_that_is_not_a_repository_needs_an_agent(self) -> None:
        result = self.check(make_project(self.root, commit=False))
        self.assertEqual(result["state"], "needs-agent")
        self.assertEqual(result["reasons"], ["the project is not a git repository yet"])

    def test_a_project_without_code_is_not_deployable(self) -> None:
        project = self.root / "notes"
        project.mkdir()
        (project / "README.md").write_text("# notes\n")
        (project / ".xo").mkdir()
        (project / ".xo" / "project.json").write_text("{}")
        self.assertEqual(self.check(project)["state"], "not-deployable")
        self.assertEqual(self.check(self.root / "missing")["state"], "not-deployable")

    def test_a_config_with_an_app_is_linked(self) -> None:
        project = make_project(self.root)
        (project / "launchpad.json").write_text(json.dumps({
            "schema": 1, "name": "hello", "app_id": "0ffce8a6-0dad-4a2a-88c1-583cf3222328", "slug": "hello-xo-1",
            "url": "https://hello.apps.test", "repo": {"full_name": "octo/hello", "remote": "launchpad", "branch": "main"},
            "port": 8080, "health_path": "/up", "env": [{"name": "API_KEY", "sensitive": True}]}))
        result = self.check(project)
        self.assertEqual(result["state"], "linked")
        self.assertEqual(result["fields"]["port"], {"value": 8080, "source": "launchpad.json", "confident": True})
        self.assertEqual(result["fields"]["health_path"]["value"], "/up")
        self.assertEqual(result["fields"]["env"], [{"name": "API_KEY", "sensitive": True, "source": "launchpad.json"}])
        self.assertEqual(result["config"]["app_id"], "0ffce8a6-0dad-4a2a-88c1-583cf3222328")
        self.assertEqual(result["git"]["deployed_head"], None)
        git("update-ref", "refs/remotes/launchpad/main", "HEAD", cwd=project)
        self.assertEqual(self.check(project)["git"]["deployed_head"], self.check(project)["git"]["head"])

    def test_a_config_holding_a_value_is_refused(self) -> None:
        project = make_project(self.root)
        (project / "launchpad.json").write_text(json.dumps({"schema": 1, "env": [{"name": "API_KEY", "value": "x"}]}))
        result = self.check(project)
        self.assertEqual(result["state"], "needs-agent")
        self.assertIn("values go to Launchpad", result["config_error"])
        self.assertIsNone(result["config"])

    def test_an_agent_prepared_config_without_an_app_is_ready(self) -> None:
        project = make_project(self.root, dockerfile="FROM scratch\n")
        (project / "launchpad.json").write_text(json.dumps({"schema": 1, "name": "web-app", "port": 9000,
                                                             "health_path": None}))
        result = self.check(project)
        self.assertEqual((result["state"], result["fields"]["name"]["value"], result["fields"]["port"]["value"]),
                         ("ready", "web-app", 9000))

    def test_env_names_come_from_the_example_file_values_never_for_secrets(self) -> None:
        project = make_project(self.root, files={".env.example": (
            "DATABASE_URL=postgres://user:pass@db/app\nLOG_LEVEL=info  # verbosity\nexport API_TOKEN=abc\n"
            "SITE_NAME=your-site-name\nEMPTY=\n")})
        env = {e["name"]: e for e in self.check(project)["fields"]["env"]}
        self.assertEqual(list(env), ["DATABASE_URL", "LOG_LEVEL", "API_TOKEN", "SITE_NAME", "EMPTY"])
        self.assertEqual(env["DATABASE_URL"], {"name": "DATABASE_URL", "sensitive": True, "source": ".env.example"})
        self.assertEqual(env["LOG_LEVEL"]["default"], "info")
        self.assertTrue(env["API_TOKEN"]["sensitive"])
        self.assertNotIn("default", env["API_TOKEN"])
        self.assertNotIn("default", env["SITE_NAME"])                       # a placeholder
        self.assertNotIn("default", env["EMPTY"])

    def test_a_detached_head_and_another_branch_are_blockers(self) -> None:
        project = make_project(self.root)
        git("checkout", "-q", "--detach", cwd=project)
        self.assertEqual(self.check(project)["blockers"], ["no branch is checked out in the project folder"])
        git("checkout", "-q", "-b", "feature", cwd=project)
        (project / "launchpad.json").write_text(json.dumps({"schema": 1, "repo": {"full_name": "octo/hello",
                                                                                  "branch": "main"}}))
        self.assertEqual(self.check(project)["blockers"],
                         ["the app deploys main, but the project folder is on feature"])

    def test_check_runs_no_command(self) -> None:
        project = make_project(self.root)
        with mock.patch.object(launchpad, "run_sync", side_effect=AssertionError("check ran a command")):
            self.assertEqual(self.check(project)["state"], "ready")

    def test_a_worktree_reads_its_own_branch(self) -> None:
        project = make_project(self.root)
        tree = self.root / "tree"
        git("worktree", "add", "-q", "-b", "xo/thread-1", str(tree), cwd=project)
        result = self.check(tree)
        self.assertEqual((result["git"]["branch"], result["state"]), ("xo/thread-1", "ready"))


class ParsingTests(unittest.TestCase):
    def test_the_final_stage_port_resolves_args_and_env(self) -> None:
        self.assertEqual(launchpad.dockerfile_port("FROM a AS b\nEXPOSE 9999\nFROM c\nARG PORT=8080\nEXPOSE ${PORT}\n"),
                         (8080, "Dockerfile EXPOSE"))
        self.assertEqual(launchpad.dockerfile_port("FROM c\nENV PORT 5000\n"), (5000, "Dockerfile ENV PORT"))
        self.assertEqual(launchpad.dockerfile_port("FROM c\nEXPOSE 3000/tcp 3001\n"), (3000, "Dockerfile EXPOSE"))
        self.assertEqual(launchpad.dockerfile_port("FROM c\nEXPOSE ${PORT:-7000}\n"), (7000, "Dockerfile EXPOSE"))
        self.assertEqual(launchpad.dockerfile_port("FROM c\nRUN echo \\\n EXPOSE 1\n"), (None, None))

    def test_github_remotes_in_every_form(self) -> None:
        with mock.patch.dict(os.environ, {"LAUNCHPAD_GITHUB_GIT": ""}):
            for url in ("https://github.com/octo/app.git", "https://user:tok@github.com/octo/app",
                        "git@github.com:octo/app.git", "ssh://git@github.com/octo/app.git", "https://github.com/octo/app/"):
                self.assertEqual(launchpad.github_full(url), "octo/app", url)
            for url in ("https://gitlab.com/octo/app.git", "", None, "https://github.com/octo"):
                self.assertIsNone(launchpad.github_full(url), url)

    def test_names_become_launchpad_names(self) -> None:
        self.assertEqual(launchpad.slug_name("My Cool_Project!!"), "my-cool-project")
        self.assertEqual(launchpad.slug_name("x" * 40), "x" * 25)
        self.assertEqual(launchpad.slug_name("---"), "app")

    def test_config_validation(self) -> None:
        self.assertIn("name must be 1-25 lowercase letters, digits or hyphens",
                      launchpad.validate_config({"name": "Bad Name"}))
        self.assertIn("app_id needs repo: the GitHub repo the app builds from",
                      launchpad.validate_config({"app_id": "0ffce8a6-0dad"}))
        self.assertIn("port must be a number from 1 to 65535", launchpad.validate_config({"port": True}))
        self.assertIn("health_path must start with / and have no spaces, or be null",
                      launchpad.validate_config({"health_path": "health"}))
        self.assertEqual(launchpad.validate_config({"repo": {"full_name": "octo/app", "branch": "main"}}), [])
        self.assertIn("repo.branch is not a branch name",
                      launchpad.validate_config({"repo": {"full_name": "octo/app", "branch": "-x"}}))


if __name__ == "__main__":
    unittest.main()

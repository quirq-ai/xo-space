"""Pins the engineering guides against live storm, doctor and projects-root facts.

The weekly docs pass keeps DEVELOPING.md / README / INSTALLATION in step with
the capability loader, doctor CHECKS, OpenClaw stop, and founder control.
When a pin fails, update the guide to the code, not the other way around.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from services.cowork_agent.adapters.loader import list_capability_providers
from services.cowork_agent.project_layout import xo_projects_root
from services.cowork_agent.storm.engine.admission import PAID_THRESHOLD
from services.cowork_agent.storm.store import SCHEMA_VERSION
from services.doctor.run import CHECKS


ROOT = Path(__file__).resolve().parents[1]
DASHES = re.compile("[\u2013\u2014]")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


class StormDocsTests(unittest.TestCase):
    def test_runtime_capability_matches_the_capability_table(self) -> None:
        providers = list_capability_providers("runtime")
        self.assertEqual(providers, ["antigravity", "claude_code", "codex"])
        table = read("DEVELOPING.md")
        table = table[table.index("| `runtime` |"): table.index("\n", table.index("| `runtime` |"))]
        # openclaw, claude_code, codex, hermes, antigravity, grokbot
        cells = [c.strip() for c in table.split("|")[3:9]]
        self.assertEqual(cells, ["no", "✓", "✓", "no", "✓", "no"])
        self.assertNotIn("until their runtimes", read("DEVELOPING.md"))
        self.assertNotIn("C1 and C2", read("DEVELOPING.md"))

    def test_readme_names_storm_agents_and_does_not_retire_codex_exec(self) -> None:
        readme = read("README.md")
        self.assertIn("`codex app-server`", readme)
        self.assertIn("`codex exec`", readme)
        self.assertIn("Claude Code, Codex or Antigravity", readme)
        self.assertIn("/api/chat/sessions/", readme)
        self.assertIn("`{stream_id}`", readme)
        self.assertIn("does not follow a storm session", squash(readme))
        self.assertIn("/api/chat/intent", readme)

    def test_chat_view_still_speaks_todays_stream_id_contract(self) -> None:
        chat = read("space_ui/js/views/chat.js")
        self.assertIn("stream_id", chat)
        self.assertIn("/api/chat/stream/{id}", chat)
        ui = read("space_ui/README.md")
        self.assertIn("does not follow a storm session", ui)
        self.assertIn("GET /api/chat/stream/{stream_id}", ui)

    def test_projects_root_unset_is_home(self) -> None:
        src = ast.parse((ROOT / "services/cowork_agent/project_layout.py").read_text(encoding="utf-8"))
        fallback = None
        for node in src.body:
            if isinstance(node, ast.FunctionDef) and node.name == "xo_projects_root":
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and child.value == "~":
                        fallback = child.value
        self.assertEqual(fallback, "~")
        self.assertTrue(callable(xo_projects_root))
        for rel in ("DEVELOPING.md", "INSTALLATION.md", "README.md", ".env.example"):
            text = read(rel)
            self.assertIn("~", text)
            self.assertIn("XO_PROJECTS_ROOT", text)

    def test_doctor_checks_and_move_aside_are_documented(self) -> None:
        names = [name for name, _ in CHECKS]
        self.assertEqual(
            names,
            [
                "roots", "disk", "read", "content", "history", "perms", "space",
                "projects", "runtime", "tmp", "layout", "legacy", "watcher",
                "components", "connections", "github", "scheduler", "usage",
                "relay", "growth",
            ],
        )
        dev = read("DEVELOPING.md")
        section = dev[dev.index("## 16. xo-doctor"):]
        for name in names:
            self.assertIn(f"`{name}`", section)
        self.assertIn("GET /api/doctor", section)
        self.assertIn("doctor_identity_lost", section)
        self.assertIn("/api/doctor/runtime-leftovers/{key}/move-aside", section)
        install = read("INSTALLATION.md")
        self.assertIn("## Health checks", install)
        self.assertIn("GET /api/doctor", install)
        self.assertIn("doctor_identity_lost", install)

    def test_openclaw_stop_default_is_ninety_seconds(self) -> None:
        header = read("config/agents/openclaw/agent.sh").splitlines()[:12]
        joined = "\n".join(header)
        self.assertIn("default 90", joined)
        self.assertIn("/gateway/restart's 30 s timeout", joined)
        install = read("INSTALLATION.md")
        self.assertIn("agent.sh stop", install)
        self.assertIn("90 s", install)
        self.assertIn("30 s", install)
        self.assertIn("Hermes", install)
        dev = squash(read("DEVELOPING.md"))
        self.assertIn("agent.sh stop", dev)
        self.assertIn("default 90 s", dev)

    def test_founder_paid_and_advisory_are_documented(self) -> None:
        self.assertEqual(PAID_THRESHOLD, 0.95)
        self.assertEqual(SCHEMA_VERSION, 6)
        dev = read("DEVELOPING.md")
        section = squash(dev[dev.index("## 15. Founder control"): dev.index("## 16. xo-doctor")])
        self.assertIn("POST /api/chat/intent/stop", section)
        self.assertIn("PAID_THRESHOLD", section)
        self.assertIn("0.95", section)
        self.assertIn("`never`", section)
        self.assertIn("`ask`", section)
        self.assertIn("`always`", section)
        self.assertIn("GET /api/advisory", section)
        self.assertIn("this build is 6", squash(dev))
        self.assertIn("/api/chat/runtime-policy", dev)
        self.assertIn("threads/{ts}/continue", dev)
        self.assertIn("Space UI has no Stop", section)
        self.assertIsNone(DASHES.search(section))

    def test_installer_trust_never_resets_an_untrusted_tree(self) -> None:
        install_sh = read("install.sh")
        self.assertIn("checkout_trust_problem()", install_sh)
        self.assertIn("Stop rather than fall back to a managed clone", install_sh)
        # A managed, clean checkout on the tracked branch still fast-forwards.
        self.assertIn('git -C "$REPO_DIR" reset --hard', install_sh)
        guide = read("INSTALLATION.md")
        self.assertIn("## Installer trust", guide)
        self.assertIn("never `git reset`s an untrusted tree", guide)

    def test_remote_control_is_documented_as_adapter_owned(self) -> None:
        developing = read("DEVELOPING.md")
        section = squash(developing[developing.index("## 17. Remote Control"):])
        self.assertIn("GET /api/remote-control/status", section)
        self.assertIn("POST /api/remote-control/pair", section)
        self.assertIn("XO_RC_DIR", section)
        self.assertIn("RESPONSE_WAIT_SECONDS", section)
        self.assertIn("~/.claude/.credentials.json", section)
        self.assertIn("/api/remote-control/*", read("README.md"))
        self.assertIn("## Remote Control", read("INSTALLATION.md"))
        ui = read("space_ui/README.md")
        self.assertIn("no Remote Control button", ui)
        self.assertIn("no paid/Stop/pause controls", ui)

    def test_launchpad_docs_say_the_github_token_reaches_launchpad(self) -> None:
        api = read("services/launchpad/API.md")
        self.assertIn("githubToken=GITHUB_TOKEN", api)
        self.assertIn("browser never sees a GitHub token", api)
        dev = read("DEVELOPING.md")
        self.assertIn("githubToken=GITHUB_TOKEN", dev)
        self.assertIn("needs confirm_repo", dev)

    def test_no_en_or_em_dashes_in_new_sections(self) -> None:
        dev = read("DEVELOPING.md")
        for heading in ("## 15. Founder control", "## 16. xo-doctor", "## 17. Remote Control"):
            start = dev.index(heading)
            nxt = dev.find("\n## ", start + 4)
            block = dev[start:] if nxt < 0 else dev[start:nxt]
            self.assertIsNone(DASHES.search(block), heading)


if __name__ == "__main__":
    unittest.main()

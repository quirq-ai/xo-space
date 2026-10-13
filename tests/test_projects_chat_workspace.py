"""The workspace level: the workspace coordinator forwards an owner's message
to one or several project coordinators (a server copy with forwarded_from),
says when a project does not exist, and answers a project coordinator's
question with a relay."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.projects_chat import comms, coordinators, store, wakes, workspace  # noqa: F401


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        for name, pid in (("calc", "p-calc"), ("blog", "p-blog")):
            (base / "xo" / name / ".xo").mkdir(parents=True)
            (base / "xo" / name / ".xo" / "project.json").write_text(json.dumps({"pid": pid}))
        (base / "xo" / "calc" / "README.md").write_text("# calc\nA tiny calculator.\n")
        env = mock.patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(base / "state"), "XO_PROJECTS_ROOT": str(base / "xo")})
        env.start()
        self.addCleanup(env.stop)
        self.ws = comms.Caller(None, coordinators.ensure(None))

    def owner(self, text):
        return store.post_message(None, author_kind="owner", author_id="owner", text=text)

    def test_the_workspace_coordinator_has_the_routing_tools(self) -> None:
        names = {t["name"] for t in comms.tools_for("workspace-coordinator")}
        self.assertLessEqual({"list_projects", "fetch_project_card", "forward_to_project", "no_such_project"}, names)
        self.assertNotIn("start_thread_session", names)
        self.assertIn("mcp__xo_comms__no_such_project", comms.terminal_tool_names("workspace-coordinator"))

    def test_list_projects_describes_each(self) -> None:
        projects = comms.call(self.ws, "list_projects", {})["projects"]
        self.assertEqual([p["project"] for p in projects], ["blog", "calc"])
        self.assertIn("tiny calculator", projects[1]["description"])

    def test_one_message_fans_out_to_two_projects_as_server_copies(self) -> None:
        msg = self.owner("Add a --version flag to calc and a changelog page to blog.")
        for project, note in (("calc", "The calc part."), ("blog", "The blog part.")):
            comms.call(self.ws, "forward_to_project", {"project": project, "context_message_ids": [msg["id"]],
                                                       "note": note})
        for project in ("calc", "blog"):
            [copy] = store.read_messages(project)
            self.assertEqual(copy["text"], msg["text"])
            self.assertNotEqual(copy["id"], msg["id"])
            self.assertEqual(copy["forwarded_from"], {"channel_id": "chan_workspace", "message_id": msg["id"]})
            coordinator = coordinators.live(project)
            [wake] = wakes.pending(project, coordinator["id"])
            self.assertEqual(wake["meta"]["message_id"], copy["id"])
            self.assertIn('<relay from="workspace-coordinator"', wake["envelope"])
            self.assertIn(f'id="{copy["id"]}"', wake["envelope"])

    def test_forwarding_refuses_an_unknown_project_or_a_non_owner_message(self) -> None:
        msg = self.owner("Fix the docs site.")
        with self.assertRaises(comms.CommsError) as ctx:
            comms.call(self.ws, "forward_to_project", {"project": "docs", "context_message_ids": [msg["id"]]})
        self.assertEqual(ctx.exception.code, "no_such_project")
        reply = comms.call(self.ws, "post_message", {"text": "Looking."})
        with self.assertRaises(comms.CommsError):
            comms.call(self.ws, "forward_to_project", {"project": "calc", "context_message_ids": [reply["message_id"]]})

    def test_no_such_project_answers_in_the_workspace_chat(self) -> None:
        self.owner("Deploy the mobile app.")
        comms.call(self.ws, "no_such_project", {"text": "There is no mobile app project here."})
        self.assertEqual(store.read_messages(None)[-1]["text"], "There is no mobile app project here.")
        self.assertEqual(sorted(p.name for p in Path(os.environ["XO_PROJECTS_ROOT"]).iterdir()), ["blog", "calc"],
                         "nothing is created")

    def test_a_project_question_reaches_the_workspace_and_its_answer_comes_back(self) -> None:
        calc = comms.Caller("calc", coordinators.ensure("calc"))
        comms.call(calc, "ask_workspace", {"question": "Which project holds the shared CSS?"})
        self.assertEqual(store.read_messages(None), [], "coordinator questions are not posted in the chat")
        [ask] = wakes.pending(None, self.ws.session_id)
        self.assertIn("Which project holds the shared CSS?", ask["envelope"])
        comms.call(self.ws, "forward_to_project", {"project": "calc", "note": "The blog project has it, in static/."})
        [answer] = wakes.pending("calc", calc.session_id)
        self.assertIn("The blog project has it", answer["envelope"])
        self.assertEqual(store.read_messages("calc"), [], "an answer is a relay, not a chat message")


if __name__ == "__main__":
    unittest.main()

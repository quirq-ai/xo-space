"""The runtime contract's shape (storm ch 01; its rules in §2, amendments §9A).

The contract is pure data: these tests hold it to stdlib-only imports,
frozen JSON-serialisable types, the complete set of modes, events marked as
the design marks them, and the withdrawn fields staying withdrawn.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import sys
import typing
import unittest
from pathlib import Path

from services.cowork_agent.adapters import contract as c
from utils.commands import StreamProcess

CONTRACT_FILE = Path(c.__file__)

# ch 01 §5.2: the events that are live display only.
EPHEMERAL = {"SessionIdle", "TextDelta", "ThinkingDelta", "ToolInputDelta", "ToolOutputDelta",
             "RunProgress", "RunSnapshot"}

ENVELOPE = dict(session="cse_1", ts="2026-10-15T12:00:00Z", seq=1)

# One instance of every dataclass in the contract, filled with its required
# fields only (the rest keep their defaults).
SAMPLES = [
    c.SystemPrompt(text="role"),
    c.ToolPolicy(allow=("read",), deny=("shell",)),
    c.PermissionPolicy(mode="accept_edits", writable_roots=("/w",)),
    c.CliLimits(),
    c.McpServer(name="xo_comms", transport="http", url="http://127.0.0.1/mcp", bearer_env="XO_COMMS_TOKEN"),
    c.HostTool(name="t", description="d", input_schema={"type": "object"}),
    c.AgentDef(description="d", prompt="p", tools=c.ToolPolicy(preset="read_only")),
    c.SessionSpec(cwd="/w", agents={"helper": c.AgentDef(description="d", prompt="p")},
                  mcp_servers=(c.McpServer(name="m", transport="stdio", command=("x",)),)),
    c.UserInput(text="hi", context="<wake/>"),
    c.SupportItem(feature="tools.deny", support="native", mechanism="--disallowedTools", enforced_by="cli"),
    c.SupportReport(items=()),
    c.FeatureSupport(support="emulated", note="SIGINT, then resume"),
    c.FeatureList(agent="a", cli_version="1", contract_version=c.CONTRACT_VERSION,
                  features={"interrupt": c.FeatureSupport(support="native")}),
    c.RuntimeInfo(cli_path="/bin/x", cli_version="1", logged_in=True),
    c.ModelInfo(id="m", efforts=("low", "high")),
    c.UsageWindow(name="five_hour", utilization=0.5),
    c.UsageSnapshot(windows=(c.UsageWindow(name="weekly"),)),
    c.SessionInfo(native_id="n"),
    c.TranscriptPage(items=({"role": "user"},)),
    c.PermissionRequest(request_id="r", run=None, call_id="k", tool="Bash", kind="command",
                        summary="run `ls`", input={"command": "ls"}, cwd="/w"),
    c.PermissionDecision(allow=True),
    c.Question(request_id="q", run=None, questions=({"id": "1", "question": "?"},)),
    c.ToolResult(content="ok"),
    c.RunInfo(run_id="r", parent_run_id=None, spawn_call_id=None, kind="subagent", agent_type=None,
              title="t", background=False, depth=1),
    c.RunState(run_id="r", state="running"),
    c.TokenUsage(input=1),
    c.PlanItem(text="t", state="todo"),
    c.OneshotRequest(prompt="p"),
    c.OneshotResult(text="t"),
    c.SessionStarted(**ENVELOPE, model=None, cwd="/w", cli_version="1"),
    c.TurnStarted(**ENVELOPE, origin="person"),
    c.TurnFinished(**ENVELOPE, status="completed", usage=c.TokenUsage(output=3)),
    c.SessionIdle(**ENVELOPE),
    c.SessionClosed(**ENVELOPE, reason="closed", exit_code=0),
    c.TextDelta(**ENVELOPE, block_id="b", delta="x"),
    c.TextDone(**ENVELOPE, block_id="b", text="x"),
    c.ThinkingDelta(**ENVELOPE, block_id="b", delta="x"),
    c.ThinkingDone(**ENVELOPE, block_id="b", duration_ms=10, steps=("plan",)),
    c.ToolStarted(**ENVELOPE, call_id="k", tool="Write", kind="create_file", title="Write app.py", target="app.py"),
    c.ToolInputDelta(**ENVELOPE, call_id="k", bytes_so_far=3400),
    c.ToolOutputDelta(**ENVELOPE, call_id="k", delta="out"),
    c.ToolFinished(**ENVELOPE, call_id="k", status="ok", diff_stat=(3, 1)),
    c.PlanUpdated(**ENVELOPE, items=(c.PlanItem(text="t", state="doing"),)),
    c.PermissionRequested(**ENVELOPE, request=c.PermissionRequest(
        request_id="r", run=None, call_id=None, tool="Bash", kind="command", summary="s", input={}, cwd=None)),
    c.PermissionResolved(**ENVELOPE, request_id="r", decision=c.PermissionDecision(allow=False, message="no")),
    c.QuestionAsked(**ENVELOPE, question=c.Question(request_id="q", run=None, questions=())),
    c.QuestionAnswered(**ENVELOPE, request_id="q", answer={"1": "yes"}),
    c.RunStarted(**ENVELOPE, info=c.RunInfo(run_id="r", parent_run_id=None, spawn_call_id="k", kind="worker",
                                            agent_type="worker", title="t", background=True, depth=1)),
    c.RunProgress(**ENVELOPE, tokens=1, tool_calls=2, duration_ms=3),
    c.RunFinished(**ENVELOPE, status="completed"),
    c.RunSnapshot(**ENVELOPE, live=(c.RunState(run_id="r", state="running"),)),
    c.Usage(**ENVELOPE, usage=c.TokenUsage(input=5)),
    c.RateLimit(**ENVELOPE, status="warning", utilization=0.85),
    c.Notice(**ENVELOPE, code="unknown_wire", message="an unknown field"),
    c.Error(**ENVELOPE, code="process_died", message="exited 1", fatal=True),
]


def _contract_dataclasses() -> set[type]:
    return {obj for _, obj in inspect.getmembers(c, inspect.isclass)
            if dataclasses.is_dataclass(obj) and obj.__module__ == c.__name__ and obj is not c.Event}


class ImportTests(unittest.TestCase):
    def test_imports_only_the_standard_library(self) -> None:
        tree = ast.parse(CONTRACT_FILE.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                roots.add((node.module or "").split(".")[0])
        self.assertTrue(roots <= set(sys.stdlib_module_names) | {"__future__"}, roots)


class DataTests(unittest.TestCase):
    def test_every_type_is_sampled(self) -> None:
        self.assertEqual({type(s) for s in SAMPLES}, _contract_dataclasses())

    def test_every_type_is_frozen_and_serialises_to_json(self) -> None:
        for sample in SAMPLES:
            with self.subTest(type(sample).__name__):
                self.assertTrue(type(sample).__dataclass_params__.frozen)
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(sample, dataclasses.fields(sample)[0].name, None)
                json.dumps(dataclasses.asdict(sample))

    def test_defaults_are_not_shared_between_instances(self) -> None:
        a, b = c.SessionSpec(cwd="/a"), c.SessionSpec(cwd="/b")
        self.assertIsNot(a.env, b.env)
        self.assertIsNot(a.agents, b.agents)

    def test_the_contract_can_express_every_mode_and_sandbox_level(self) -> None:
        self.assertEqual(set(typing.get_args(c.PermissionMode)),
                         {"ask", "accept_edits", "plan", "deny_unasked", "auto", "bypass"})
        self.assertEqual(set(typing.get_args(c.SandboxLevel)),
                         {"none", "read_only", "workspace_write", "full_access"})

    def test_unknowns_have_a_kind_to_land_in(self) -> None:
        self.assertIn("other", typing.get_args(c.ToolKind))
        self.assertIn("other", typing.get_args(c.RunKind))

    def test_withdrawn_fields_stay_withdrawn(self) -> None:
        # ch 01 §9A: no claude hooks, so no CLI verdict and no defer.
        self.assertNotIn("cli_verdict", {f.name for f in dataclasses.fields(c.PermissionRequest)})
        self.assertNotIn("defer", {f.name for f in dataclasses.fields(c.PermissionDecision)})

    def test_no_preset_needs_to_set_limits(self) -> None:
        self.assertEqual(dataclasses.asdict(c.SessionSpec(cwd="/w").limits),
                         {"max_usd": None, "max_turns": None, "max_subagents": None, "max_depth": None})


class EventTests(unittest.TestCase):
    def test_every_event_is_named_and_marked(self) -> None:
        subclasses = {cls for cls in _contract_dataclasses() if issubclass(cls, c.Event)}
        self.assertEqual(set(c.EVENT_TYPES.values()), subclasses)
        self.assertEqual({n for n, cls in c.EVENT_TYPES.items() if not cls.durable}, EPHEMERAL)

    def test_every_event_carries_the_envelope(self) -> None:
        envelope = {"session", "native_id", "turn_id", "run", "ts", "seq", "raw"}
        for name, cls in c.EVENT_TYPES.items():
            with self.subTest(name):
                self.assertLessEqual(envelope, {f.name for f in dataclasses.fields(cls)})

    def test_an_event_round_trips_through_json_by_its_type(self) -> None:
        ev = c.ToolFinished(**ENVELOPE, call_id="k", status="denied", summary="no")
        wire = json.dumps({"type": type(ev).__name__, **dataclasses.asdict(ev)})
        data = json.loads(wire)
        self.assertEqual(c.EVENT_TYPES[data.pop("type")](**data), ev)


class SupportTests(unittest.TestCase):
    def test_weakest_picks_the_least_supported_item(self) -> None:
        report = c.SupportReport(items=(
            c.SupportItem(feature="tools.deny", support="native", mechanism="a", enforced_by="cli"),
            c.SupportItem(feature="tools.deny", support="emulated", mechanism="b", enforced_by="prompt"),
            c.SupportItem(feature="thinking", support="refused", mechanism="c", enforced_by="none"),
        ))
        self.assertEqual(report.weakest("tools.deny").mechanism, "b")
        self.assertIsNone(report.weakest("fork"))

    def test_failures_carry_their_code(self) -> None:
        self.assertEqual(set(typing.get_args(c.FailureCode)), {
            "unsupported", "cli_missing", "cli_too_old", "auth_required", "rate_limited", "overage_blocked",
            "spec_refused", "session_not_found", "process_died", "protocol", "timeout"})
        report = c.SupportReport(items=())
        refused = c.SpecRefused("tools.deny would be enforced by the prompt only", report)
        self.assertIsInstance(refused, c.RuntimeFailure)
        self.assertEqual((refused.code, refused.report, refused.retryable), ("spec_refused", report, False))


class ProtocolTests(unittest.TestCase):
    def test_the_executor_handle_is_a_process_handle(self) -> None:
        wanted = {name for name in dir(c.ProcessHandle) if not name.startswith("_")} | {"pid"}
        have = set(dir(StreamProcess)) | {"pid"}
        self.assertLessEqual(wanted, have)


if __name__ == "__main__":
    unittest.main()

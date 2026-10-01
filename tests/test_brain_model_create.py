"""The pluggable model and the Create cycle: goal → designs → approval →
build → test → experience."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from unittest.mock import patch

from services.brain import create, graph, learn, reasoning, recall, service, store
from services.brain.model import BrainModel, ModelUnavailable, load_model

from tests._brain_support import NOW, BrainSandbox, ScriptedModel


class EchoModel(BrainModel):
    """Loadable by dotted path: ``tests.test_brain_model_create:EchoModel``."""

    name = "echo"

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 2000) -> str:
        return "{}"


def extraction_answer(prompt: str) -> str:
    """Model-style understanding: generic descriptions, a relation phrased
    differently per source ("speeds up" in alpha, "accelerates" in beta)."""
    chunks = []
    for cid, path in re.findall(r"### chunk (\d+) \(([^,]+),", prompt):
        verb = "accelerates" if path == "NOTES.md" else "speeds up"
        chunks.append({"id": int(cid), "concepts": [
            {"name": "Spreading activation", "description": "Search that propagates scores through a graph.",
             "usage": f"used in {path}", "level": 2, "keywords": ["graph", "search"]},
            {"name": "Recall", "description": "Finding related items again.", "usage": "the goal", "level": 1},
        ], "relations": [{"subject": "Spreading activation", "relation": verb, "object": "Recall"}]})
    if "Group these relation phrases" in prompt:
        return json.dumps({"groups": [["speeds up", "accelerates"]]})
    return "Here you go:\n```json\n" + json.dumps({"chunks": chunks}) + "\n```"


class ModelLoadingTests(BrainSandbox):
    def test_none_is_the_default_and_cannot_reason(self) -> None:
        m = load_model()
        self.assertEqual(m.describe(), {"name": "none", "reason": False, "embed": False})
        with self.assertRaises(ModelUnavailable):
            asyncio.run(m.complete("hi"))

    def test_agent_and_a_dotted_path_load(self) -> None:
        self.assertTrue(load_model("agent").can_reason)
        m = load_model("tests.test_brain_model_create:EchoModel")
        self.assertEqual((m.name, m.can_reason, m.can_embed), ("echo", True, False))

    def test_a_model_that_fails_to_load_is_reported_not_swapped(self) -> None:
        m = load_model("no_such_package.brain_model")
        self.assertFalse(m.can_reason)
        self.assertIn("ModuleNotFoundError", m.describe()["error"])
        with patch.dict(os.environ, {"BRAIN_MODEL": "no_such_package.brain_model"}):
            self.assertIn("error", service.status()["model"])

    def test_json_is_read_from_prose_and_fences(self) -> None:
        self.assertEqual(reasoning.parse_json('Sure! {"a": 1} hope that helps'), {"a": 1})
        self.assertEqual(reasoning.parse_json('```json\n{"a": {"b": 2}}\n```'), {"a": {"b": 2}})
        with self.assertRaises(reasoning.BadAnswer):
            reasoning.parse_json("no json here")


class ModelLearningTests(BrainSandbox):
    def test_model_extraction_names_concepts_and_relations_merge_by_meaning(self) -> None:
        model = ScriptedModel(extraction_answer)
        a = self.register("alpha", self.project("alpha", {"README.md": "# A\n\nSome text about graphs and recall.\n"
                                                                        "More words about spreading here.\n"}))
        b = self.register("beta", self.project("beta", {"NOTES.md": "# B\n\nProducts ranked by graph spreading.\n"
                                                                     "Recall of related products matters.\n"}))
        self.learn(a, model=model, budget=5)
        self.learn(b, model=model, budget=5)
        [(desc, level, origin)] = self.query("SELECT description, level, origin FROM pieces WHERE key = ?",
                                             "spreading activation")
        self.assertEqual((desc, level, origin), ("Search that propagates scores through a graph.", 2, "model"))
        self.assertEqual(self.query("SELECT DISTINCT extractor FROM chunks"), [("model",)])
        labels = {label for (label,) in self.query("SELECT label FROM relations")}
        self.assertTrue({"speeds up", "accelerates"} <= labels)
        merged = asyncio.run(graph.consolidate_relations(None, model))
        self.assertEqual(merged, 1)
        stated = self.query("SELECT DISTINCT r.label FROM links l JOIN relations r ON r.id = l.relation_id "
                            "WHERE l.signal = 'stated'")
        self.assertEqual(stated, [("speeds up",)])

    def test_the_call_budget_caps_model_use_and_the_rest_is_read_statistically(self) -> None:
        model = ScriptedModel(extraction_answer)
        body = "\n\n".join(f"## Part {i}\n\n" + "Words about topic number %d and graphs. " % i * 12 for i in range(6))
        a = self.register("alpha", self.project("alpha", {"README.md": "# Big\n\n" + body}))
        with patch.object(reasoning, "BATCH_CHARS", 700):
            stats = self.learn(a, model=model, budget=2)
        self.assertEqual(stats["model_calls"], 2)
        self.assertEqual({e for (e,) in self.query("SELECT DISTINCT extractor FROM chunks")},
                         {"model", "statistical"})

    def test_a_model_that_answers_nonsense_leaves_statistics_in_charge(self) -> None:
        model = ScriptedModel(lambda prompt: "I cannot help with that.")
        a = self.register("alpha", self.project("alpha", {"README.md": "# Alpha\n\nAlpha indexes notes. "
                                                                        "Alpha indexes notes quickly.\n"}))
        stats = self.learn(a, model=model, budget=3)
        self.assertTrue(stats["model_errors"])
        self.assertEqual(self.query("SELECT DISTINCT extractor FROM chunks"), [("statistical",)])


def design_answer(shared: int, other: int):
    def answer(prompt: str) -> str:
        return json.dumps({"designs": [
            {"title": "Guessing design", "summary": "Mostly new parts.",
             "reuses": [{"piece_id": 999999, "how": "does not exist"}],
             "new_parts": ["a", "b", "c", "d"], "risks": ["x", "y", "z"], "steps": ["do it"], "test_command": None},
            {"title": "Proven activation search", "summary": "Reuse spreading activation for related notes search.",
             "reuses": [{"piece_id": shared, "how": "rank related notes"}, {"piece_id": other, "how": "decay per hop"}],
             "new_parts": ["a small CLI"], "risks": [],
             "steps": ["write the module", "add a test"],
             "test_command": [sys.executable, "-c",
                              "import pathlib, sys; sys.exit(0 if pathlib.Path('done.txt').exists() else 1)"]},
        ]})
    return answer


class CreateTests(BrainSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.a, self.b = self.alpha_beta()
        self.shared = self.piece_id("spreading activation")
        self.other = self.piece_id("decays")
        self.model = ScriptedModel(design_answer(self.shared, self.other))

    def propose(self) -> int:
        return asyncio.run(create.propose(self.model, "search related notes by activation"))

    def test_designing_needs_a_model(self) -> None:
        with self.assertRaises(store.BrainError) as caught:
            asyncio.run(create.propose(BrainModel(), "anything"))
        self.assertEqual((caught.exception.code, caught.exception.status), ("model_required", 501))

    def test_designs_are_ranked_by_proven_reuse_experience_risk_and_fit(self) -> None:
        goal_id = self.propose()
        prompt = self.model.prompts[0]
        self.assertIn('"facts"', prompt)
        self.assertIn("spreading activation", prompt)
        rows = self.query("SELECT title, rank, scores, plan, status FROM designs WHERE goal_id = ? ORDER BY rank",
                          goal_id)
        self.assertEqual([r[0] for r in rows], ["Proven activation search", "Guessing design"])
        best, worst = json.loads(rows[0][2]), json.loads(rows[1][2])
        self.assertGreater(best["reuse"], worst["reuse"])
        self.assertLess(best["risk"], worst["risk"])
        self.assertEqual(set(best), {"reuse", "experience", "risk", "fit", "total"})
        plan = json.loads(rows[0][3])
        self.assertEqual([r["piece_id"] for r in plan["reuses"]], [self.shared, self.other])
        self.assertEqual(json.loads(rows[1][3])["reuses"], [])      # an unknown id is dropped, not trusted
        self.assertEqual({r[4] for r in rows}, {"proposed"})
        self.assertEqual(self.query("SELECT kind, status FROM findings WHERE kind = 'design'"), [("design", "open")])

    def test_nothing_is_built_before_a_person_approves(self) -> None:
        goal_id = self.propose()
        [(best,)] = self.query("SELECT id FROM designs WHERE goal_id = ? AND rank = 1", goal_id)
        with self.assertRaises(store.BrainError) as caught:
            asyncio.run(create.build(best, model=BrainModel(), runner=self.fail_runner))
        self.assertEqual(caught.exception.code, "design_not_approved")
        with store.write() as conn:
            create.approve(conn, best, NOW)
        statuses = dict(self.query("SELECT rank, status FROM designs WHERE goal_id = ?", goal_id))
        self.assertEqual(statuses, {1: "approved", 2: "set_aside"})
        self.assertEqual(self.query("SELECT status FROM findings WHERE kind = 'design'"), [("done",)])

    async def fail_runner(self, prompt: str, project: str) -> str:
        return "RESULT: failed — nothing done"

    def approved(self) -> int:
        goal_id = self.propose()
        [(best,)] = self.query("SELECT id FROM designs WHERE goal_id = ? AND rank = 1", goal_id)
        with store.write() as conn:
            create.approve(conn, best, NOW)
        return best

    def test_a_build_is_tested_fixed_learned_and_remembered_as_experience(self) -> None:
        design = self.approved()
        calls: list[tuple[str, str]] = []

        async def runner(prompt: str, project: str) -> str:
            calls.append((prompt, project))
            root = self.projects / project
            (root / "search.py").write_text('"""Related notes search reusing spreading activation."""\n\n'
                                            "def related(graph):\n    return graph\n")
            if len(calls) == 2:                        # the fix request
                (root / "done.txt").write_text("ok")
            return "Built it.\nRESULT: worked — reuse beat writing it fresh"

        with patch.dict(os.environ, {"BRAIN_FIX_ATTEMPTS": "2"}):
            record = asyncio.run(create.build(design, model=BrainModel(), runner=runner, learn_budget=0))

        project = record["project"]
        self.assertTrue(project.startswith("brain-proven-activation-search"))
        self.assertEqual(len(calls), 2)
        self.assertIn("Reuse this proven knowledge", calls[0][0])
        self.assertIn("alpha/", calls[0][0])                     # evidence points at the source's lines
        self.assertIn("failed in this project", calls[1][0])
        self.assertEqual([a["ok"] for a in record["attempts"]], [False, True])
        self.assertEqual(record["outcome"], "worked")
        [(status,)] = self.query("SELECT status FROM designs WHERE id = ?", design)
        self.assertEqual(status, "built")
        # the result is a new source, learned, separate from the originals
        [(sid, kind, sstatus)] = self.query("SELECT id, kind, status FROM sources WHERE name = ?", project)
        self.assertEqual((kind, sstatus), ("built", "ready"))
        self.assertFalse((self.projects / "alpha" / "search.py").exists())
        # experience: scores rise, co-use is linked, the reused pieces carry a note from the build
        [(result, lessons, pieces)] = self.query("SELECT result, lessons, pieces FROM experiences")
        self.assertEqual((result, lessons), ("worked", "reuse beat writing it fresh"))
        self.assertEqual(json.loads(pieces), [self.shared, self.other])
        self.assertEqual(self.query("SELECT score FROM pieces WHERE id = ?", self.shared), [(1.0,)])
        self.assertTrue(self.query("SELECT 1 FROM links WHERE kind IN ('generated','evidence','learned') AND "
                                   "((src = ? AND dst = ?) OR (src = ? AND dst = ?))",
                                   self.shared, self.other, self.other, self.shared))
        self.assertTrue(self.query("SELECT 1 FROM notes WHERE piece_id = ? AND source_id = ?", self.shared, sid))
        episodes = list((self.projects / project / "memory" / "episodic").glob("*-brain-*.md"))
        self.assertEqual(len(episodes), 1)
        self.assertIn("outcome: success", episodes[0].read_text())

    def test_a_failed_build_lowers_what_it_reused_and_recall_says_so(self) -> None:
        design = self.approved()
        with patch.dict(os.environ, {"BRAIN_FIX_ATTEMPTS": "1"}):
            record = asyncio.run(create.build(design, model=BrainModel(), runner=self.fail_runner, learn_budget=0))
        self.assertEqual(record["outcome"], "failed")
        self.assertEqual(len(record["attempts"]), 2)
        self.assertEqual(self.query("SELECT result FROM experiences"), [("failed",)])
        self.assertEqual(self.query("SELECT score FROM pieces WHERE id = ?", self.shared), [(-1.0,)])
        with store.write() as conn:
            out = recall.recall(conn, "spreading activation", now=NOW, reinforce=False)
        hit = next(r for r in out["results"] if r["piece"]["id"] == self.shared)
        self.assertIn("failed before", hit["explanation"])

    def test_experience_can_be_recorded_by_hand_and_moves_ranking(self) -> None:
        with store.write() as conn:
            before = recall.recall(conn, "spreading activation", now=NOW, reinforce=False)
        score_before = next(r["score"] for r in before["results"] if r["piece"]["id"] == self.shared)
        service.add_experience(goal="ranked shelf", result="worked", piece_ids=[self.shared])
        with store.write() as conn:
            after = recall.recall(conn, "spreading activation", now=NOW, reinforce=False)
        hit = next(r for r in after["results"] if r["piece"]["id"] == self.shared)
        self.assertGreater(hit["score"], score_before)
        self.assertIn("worked before", hit["explanation"])
        with self.assertRaises(store.BrainError):
            service.add_experience(goal="x", result="great")

    def test_a_build_interrupted_by_a_restart_is_marked_failed(self) -> None:
        design = self.approved()
        with store.write() as conn:
            conn.execute("UPDATE designs SET status = 'building' WHERE id = ?", (design,))
        service._recovered = False
        service.status()
        [(status, build)] = self.query("SELECT status, build FROM designs WHERE id = ?", design)
        self.assertEqual(status, "failed")
        self.assertIn("interrupted", json.loads(build)["error"])


class AnswerTests(BrainSandbox):
    def ask(self, model: BrainModel, cue: str = "how does spreading activation work", **kw) -> dict:
        with patch.object(service, "model", return_value=model):
            return asyncio.run(service.recall_cue(cue, answer=True, reinforce=False, **kw))

    def test_a_model_writes_an_answer_citing_only_real_evidence(self) -> None:
        self.alpha_beta()
        model = ScriptedModel(lambda prompt: json.dumps({
            "answer": "Activation spreads from matched notes along links [1], decaying per hop [2]. See also [99].",
            "cited": [1, 99], "missing": "How decay is tuned."}))
        out = self.ask(model)
        self.assertTrue(out["results"])                                    # the recall is still there
        answer = out["answer"]
        self.assertEqual(answer["model"], "scripted")
        self.assertIn("[1]", answer["text"])
        self.assertEqual([c["n"] for c in answer["citations"]], [1, 2])   # 99 is not evidence: dropped
        self.assertEqual(answer["missing"], "How decay is tuned.")
        self.assertTrue({"source", "path", "start_line", "end_line", "excerpt"} <= set(answer["evidence"][0]))
        prompt = model.prompts[0]
        self.assertIn("Question: how does spreading activation work", prompt)
        self.assertIn("Do not use tools", prompt)
        self.assertIn(json.dumps(answer["evidence"][0]["excerpt"])[1:60], prompt)   # sent JSON-encoded

    def test_no_model_or_no_request_means_no_answer(self) -> None:
        self.alpha_beta()
        self.assertIsNone(self.ask(BrainModel())["answer"])
        with patch.object(service, "model", return_value=ScriptedModel(lambda p: "{}")):
            out = asyncio.run(service.recall_cue("spreading activation", reinforce=False))   # answer not asked
        self.assertIsNone(out["answer"])

    def test_a_failing_model_reports_the_error_and_keeps_the_recall(self) -> None:
        self.alpha_beta()
        out = self.ask(ScriptedModel(lambda prompt: "I would rather not."))
        self.assertTrue(out["results"])
        self.assertIn("BadAnswer", out["answer"]["error"])

    def test_nothing_recalled_asks_the_model_nothing(self) -> None:
        self.alpha_beta()
        model = ScriptedModel(lambda prompt: "{}")
        out = self.ask(model, cue="kubernetes pod autoscaling")
        self.assertTrue(out["gap"])
        self.assertIsNone(out["answer"])
        self.assertEqual(model.prompts, [])


class LearnHelpersTests(BrainSandbox):
    def test_register_source_is_idempotent_and_reenables(self) -> None:
        root = self.project("alpha", {"README.md": "# A\n\nText about alpha things here.\n"})
        with store.write() as conn:
            learn.register_source(conn, source_id="s1", kind="project", name="alpha", location=str(root), now=NOW)
            conn.execute("UPDATE sources SET enabled = 0")
            row = learn.register_source(conn, source_id="s1", kind="project", name="alpha", location=str(root),
                                        now=NOW)
        self.assertEqual((row["enabled"], row["created_at"]), (1, NOW))
        self.assertEqual(self.query("SELECT COUNT(*) FROM sources"), [(1,)])

"""Learn, remember, recall, strengthen: the success criteria of Brain.md §10
that do not need a model."""

from __future__ import annotations

import sqlite3
import unittest

from services.brain import graph, learn, recall, store

from tests._brain_support import ALPHA_README, NOW, BrainSandbox


class LearnTests(BrainSandbox):
    def test_one_concept_in_two_sources_is_one_shared_piece_with_a_note_each(self) -> None:
        a, b = self.alpha_beta()
        pid = self.piece_id("spreading activation")
        notes = dict(self.query("SELECT source_id, note FROM notes WHERE piece_id = ?", pid))
        self.assertEqual(set(notes), {a, b})
        self.assertIn("speeds up recall", notes[a] + " ".join(r[0] for r in self.query(
            "SELECT m.usage FROM mentions m JOIN chunks c ON c.id = m.chunk_id WHERE m.piece_id = ? AND c.source_id = ?",
            pid, a)))
        self.assertIn("product graph", notes[b])
        self.assertEqual(self.query("SELECT COUNT(*) FROM pieces WHERE key = 'spreading activation'"), [(1,)])

    def test_the_code_name_merges_into_the_prose_concept(self) -> None:
        a, _ = self.alpha_beta()
        # spread_activation (code) and "spreading activation" (prose) are one piece.
        self.assertEqual(self.query("SELECT COUNT(*) FROM pieces WHERE key IN ('spread activation', "
                                    "'spreading activation')"), [(1,)])
        pid = self.piece_id("spreading activation")
        paths = {r[0] for r in self.query(
            "SELECT c.path FROM mentions m JOIN chunks c ON c.id = m.chunk_id WHERE m.piece_id = ?", pid)}
        self.assertEqual(paths, {"README.md", "engine.py", "NOTES.md"})

    def test_source_counts_are_live_so_both_sides_of_a_shared_piece_agree(self) -> None:
        from services.brain import service
        a, b = self.alpha_beta()            # alpha learned first, when nothing was shared yet
        stats = {s["id"]: s["stats"] for s in service.list_sources()["sources"]}
        self.assertGreater(stats[a]["shared"], 0)
        self.assertEqual(stats[a]["shared"], stats[b]["shared"])

    def test_every_piece_and_link_traces_to_real_lines(self) -> None:
        self.alpha_beta()
        self.assertEqual(self.query("SELECT COUNT(*) FROM pieces WHERE id NOT IN (SELECT piece_id FROM mentions)"),
                         [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM links WHERE kind = 'evidence' AND id NOT IN "
                                    "(SELECT link_id FROM link_evidence)"), [(0,)])
        for path, start, end, text in self.query("SELECT path, start_line, end_line, text FROM chunks"):
            src = (self.projects / ("alpha" if path != "NOTES.md" else "beta") / path).read_text().splitlines()
            self.assertEqual("\n".join(src[start - 1:end]).strip(), text.strip())

    def test_learning_again_changes_nothing_and_a_new_source_extends_without_duplicating(self) -> None:
        a, b = self.alpha_beta()
        before = self.query("SELECT COUNT(*) FROM pieces"), self.query("SELECT COUNT(*) FROM links")
        stats = self.learn(a)
        self.assertEqual((stats["files_changed"], stats["chunks_new"]), (0, 0))
        self.assertEqual((self.query("SELECT COUNT(*) FROM pieces"), self.query("SELECT COUNT(*) FROM links")), before)
        self.assertEqual(self.query("SELECT COUNT(*) FROM chunks WHERE source_id = ?", a),
                         [(len(self.query("SELECT id FROM chunks WHERE source_id = ?", a)),)])

    def test_a_changed_file_is_relearned_and_a_removed_file_forgotten(self) -> None:
        a, _ = self.alpha_beta()
        root = self.projects / "alpha"
        (root / "README.md").write_text(ALPHA_README.replace("The inverted index is rebuilt nightly.",
                                                             "The inverted index uses bloom filters. "
                                                             "Bloom filters skip missing terms."))
        stats = self.learn(a)
        self.assertEqual(stats["files_changed"], 1)
        self.assertTrue(self.query("SELECT id FROM pieces WHERE key = 'bloom filter'"))
        (root / "engine.py").unlink()
        stats = self.learn(a)
        self.assertEqual(stats["files_removed"], 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM chunks WHERE path = 'engine.py'"), [(0,)])
        self.assertFalse(self.query("SELECT id FROM pieces WHERE key = 'build inverted index'"))

    def test_links_come_from_statements_structure_statistics_and_kinds(self) -> None:
        self.alpha_beta()
        signals = {r[0] for r in self.query("SELECT DISTINCT signal FROM links")}
        self.assertTrue({"stated", "structure", "cooccurrence", "hierarchy"} <= signals, signals)
        rows = self.query(
            "SELECT p1.name, r.label, p2.name FROM links l JOIN pieces p1 ON p1.id = l.src "
            "JOIN pieces p2 ON p2.id = l.dst JOIN relations r ON r.id = l.relation_id")
        self.assertIn(("search", "uses", "build inverted index"), rows)
        self.assertIn(("inverted index", "maps", "term"), rows)
        self.assertIn(("spreading activation", "is a kind of", "activation"), rows)
        level = dict(self.query("SELECT name, level FROM pieces"))
        self.assertGreater(level["activation"], level["spreading activation"])

    def test_relation_phrases_that_mean_the_same_share_a_type_named_by_the_commonest(self) -> None:
        with store.write() as conn:
            first = graph.relation_for(conn, "speeds up")
            self.assertEqual(graph.relation_for(conn, "speed up"), first)
            self.assertEqual(graph.relation_for(conn, "speeds up"), first)
            other = graph.relation_for(conn, "depends on")
            self.assertNotEqual(other, first)
            label = conn.execute("SELECT label FROM relations WHERE id = ?", (first,)).fetchone()[0]
        self.assertEqual(label, "speeds up")

    def test_forgetting_a_source_keeps_shared_pieces_with_the_other_note(self) -> None:
        a, b = self.alpha_beta()
        pid = self.piece_id("spreading activation")
        with store.write() as conn:
            learn.forget_source(conn, a, NOW)
        self.assertEqual(self.query("SELECT source_id FROM notes WHERE piece_id = ?", pid), [(b,)])
        self.assertFalse(self.query("SELECT id FROM pieces WHERE key = 'build inverted index'"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM chunks WHERE source_id = ?", a), [(0,)])

    def test_a_newer_store_is_refused_not_rewritten(self) -> None:
        with store.connect():
            pass
        conn = sqlite3.connect(store.db_path())
        conn.execute(f"PRAGMA user_version = {store.SCHEMA + 1}")
        conn.commit()
        conn.close()
        with self.assertRaises(store.BrainError) as caught:
            with store.connect():
                pass
        self.assertEqual(caught.exception.code, "store_newer")


class RecallTests(BrainSandbox):
    def recall(self, cue: str, **kw) -> dict:
        with store.write() as conn:
            return recall.recall(conn, cue, now=NOW, **kw)

    def test_the_same_cue_answers_differently_in_different_contexts(self) -> None:
        a, b = self.alpha_beta()
        in_a = self.recall("how does activation spread", context_source=a, reinforce=False)
        in_b = self.recall("how does activation spread", context_source=b, reinforce=False)
        names_a = [r["piece"]["name"] for r in in_a["results"]]
        names_b = [r["piece"]["name"] for r in in_b["results"]]
        self.assertNotEqual(names_a, names_b)
        self.assertTrue({"inverted index", "search"} & set(names_a))
        self.assertIn("related products", names_b)
        top_a = in_a["results"][0]
        self.assertEqual(top_a["note"]["source_id"], a)
        self.assertIn("boosted by the active source (alpha)", top_a["explanation"])

    def test_recall_reaches_through_links_and_explains_the_path(self) -> None:
        self.alpha_beta()
        out = self.recall("spreading activation", reinforce=False)
        spread = [r for r in out["results"] if r["hops"] > 0]
        self.assertTrue(spread)
        hit = next(r for r in spread if r["piece"]["name"] == "inverted index")
        self.assertIn("cue matched spreading activation", hit["explanation"])
        self.assertIn("spreading activation starts from inverted index", hit["explanation"])
        self.assertAlmostEqual(hit["score"], hit["path"][0]["weight"] * 0.5 * out["results"][0]["score"], places=3)
        self.assertTrue(hit["evidence"])
        self.assertEqual({"source_id", "source", "path", "start_line", "end_line", "quote", "extractor"},
                         set(hit["evidence"][0]))

    def test_a_cue_nothing_matches_is_recorded_as_a_gap(self) -> None:
        self.alpha_beta()
        out = self.recall("kubernetes pod autoscaling")
        self.assertTrue(out["gap"])
        self.recall("kubernetes pod autoscaling")
        self.assertEqual(self.query("SELECT kind, count FROM findings"), [("gap", 2)])

    def test_a_project_named_in_the_cue_becomes_the_context(self) -> None:
        a, b = self.alpha_beta()
        out = self.recall("how does spreading activation rank products in beta", reinforce=False)
        self.assertEqual((out["context_source"], out["context_name"], out["context_detected"]), (b, "beta", True))
        self.assertFalse(out["about_source"])
        self.assertIn("boosted by the active source (beta)", out["results"][0]["explanation"])
        given = self.recall("spreading activation in beta", context_source=a, reinforce=False)
        self.assertEqual((given["context_source"], given["context_detected"]), (a, False))   # an explicit one wins

    def test_a_folder_name_with_separators_and_a_version_is_still_recognised(self) -> None:
        a, _ = self.alpha_beta()
        with store.write() as conn:
            conn.execute("UPDATE sources SET name = 'ALPHA-Notes-0.3' WHERE id = ?", (a,))
            self.assertEqual(recall.mentioned_source(conn, "tell me about alpha notes")[:2], (a, "ALPHA-Notes-0.3"))
            self.assertIsNone(recall.mentioned_source(conn, "tell me about alpha"))     # every name word is needed

    def test_a_cue_that_only_names_a_project_starts_from_what_it_is_about(self) -> None:
        a, _ = self.alpha_beta()
        out = self.recall("tell me about alpha", reinforce=False)
        self.assertTrue(out["about_source"])
        self.assertFalse(out["gap"])
        self.assertTrue(out["results"])
        self.assertTrue(all(any(n["source_id"] == a for n in r["notes"]) for r in out["results"][:3]))
        with store.connect() as conn:
            material = recall.answer_material(conn, out)
        self.assertEqual(material["context"], "alpha")
        self.assertEqual(material["evidence"][0]["path"], "README.md")        # the overview comes first
        self.assertEqual([e["n"] for e in material["evidence"]], list(range(1, len(material["evidence"]) + 1)))
        self.assertTrue(all(len(e["excerpt"]) <= 700 for e in material["evidence"]))

    def test_guesses_are_never_mixed_with_facts(self) -> None:
        self.alpha_beta()
        a, b = self.piece_id("collaborative filtering"), self.piece_id("inverted index")
        with store.write() as conn:
            graph.upsert_link(conn, a, b, graph.MAY_RELATE, kind="hypothesis", signal="predicted", now=NOW, weight=0.9)
        plain = self.recall("collaborative filtering", reinforce=False)
        self.assertNotIn("inverted index", [r["piece"]["name"] for r in plain["results"]])
        self.assertEqual(plain["hypotheses"], [])
        explored = self.recall("collaborative filtering", explore=True, reinforce=False)
        guessed = [r["piece"]["name"] for r in explored["hypotheses"]]
        self.assertIn("inverted index", guessed)
        self.assertNotIn("inverted index", [r["piece"]["name"] for r in explored["results"]])
        self.assertIn("(hypothesis)", explored["hypotheses"][0]["explanation"])


class StrengthenTests(BrainSandbox):
    def test_co_recalled_pieces_get_linked_then_stronger(self) -> None:
        self.alpha_beta()
        a, b = self.piece_id("collaborative filtering"), self.piece_id("inverted index")
        self.assertEqual(self.query("SELECT COUNT(*) FROM links WHERE (src=? AND dst=?) OR (src=? AND dst=?)",
                                    a, b, b, a), [(0,)])
        with store.write() as conn:
            recall.use_together(conn, [a, b], now=NOW)
        [(kind, w1)] = self.query("SELECT kind, weight FROM links WHERE (src=? AND dst=?) OR (src=? AND dst=?)",
                                  a, b, b, a)
        self.assertEqual(kind, "learned")
        with store.write() as conn:
            recall.use_together(conn, [a, b], now=NOW)
        [(w2,)] = self.query("SELECT weight FROM links WHERE (src=? AND dst=?) OR (src=? AND dst=?)", a, b, b, a)
        self.assertGreater(w2, w1)

    def test_recall_strengthens_the_links_among_its_top_results(self) -> None:
        self.alpha_beta()
        with store.write() as conn:
            first = recall.recall(conn, "spreading activation", now=NOW, reinforce=False)
        top = [r["piece"]["id"] for r in first["results"][:2]]
        weight = lambda: self.query(  # noqa: E731
            "SELECT MAX(weight) FROM links WHERE ((src=? AND dst=?) OR (src=? AND dst=?)) AND kind != 'hypothesis'",
            top[0], top[1], top[1], top[0])[0][0]
        before = weight()
        with store.write() as conn:
            recall.recall(conn, "spreading activation", now=NOW)
        self.assertGreater(weight(), before)

    def test_unused_links_fade_and_weak_learned_links_disappear(self) -> None:
        self.alpha_beta()
        a, b = self.piece_id("collaborative filtering"), self.piece_id("inverted index")
        with store.write() as conn:
            recall.use_together(conn, [a, b], now="2026-01-01T00:00:00Z")
            graph.fade(conn, "2026-01-01T00:00:00Z", 0.02)          # starts the clock
            evidence_before = dict(conn.execute("SELECT id, weight FROM links WHERE kind = 'evidence'").fetchall())
            out = graph.fade(conn, "2026-09-01T00:00:00Z", 0.02)     # 243 days
        self.assertGreater(out["faded"], 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM links WHERE kind = 'learned'"), [(0,)])
        for link_id, weight, base in self.query("SELECT id, weight, base_weight FROM links WHERE kind = 'evidence'"):
            self.assertLess(weight, evidence_before[link_id] + 1e-9)
            self.assertGreaterEqual(weight, round(base * 0.5, 4) - 1e-9)

    def test_relearning_keeps_what_use_added(self) -> None:
        a, _ = self.alpha_beta()
        s, d = self.piece_id("search"), self.piece_id("build inverted index")
        with store.write() as conn:
            recall.use_together(conn, [s, d], now=NOW)
        [(w,)] = self.query("SELECT weight FROM links WHERE src=? AND dst=? AND signal = 'structure' "
                            "AND phrase = 'uses'", s, d)
        self.learn(a, force=True)
        [(w2,)] = self.query("SELECT weight FROM links WHERE src=? AND dst=? AND signal = 'structure' "
                             "AND phrase = 'uses'", s, d)
        self.assertAlmostEqual(w, w2, places=4)


if __name__ == "__main__":
    unittest.main()

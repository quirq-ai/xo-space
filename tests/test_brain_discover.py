"""Discover: patterns across sources under different names, missing links
kept as hypotheses, analogies, novelty."""

from __future__ import annotations

import asyncio

from services.brain import discover, graph, store
from services.brain.model import BrainModel

from tests._brain_support import NOW, BrainSandbox

SHOP = """# Shop

The shop sells printed books online.

## Order queue

The order queue buffers checkout requests before payment. Workers poll the order queue and retry failed payments with exponential backoff.

## Payment worker

The payment worker takes jobs from the order queue. The payment worker retries with exponential backoff when the gateway times out.

## Dead letter store

Jobs that fail five times move to the dead letter store for manual review. The dead letter store keeps failed jobs for a week.
"""

MAIL = """# Mailer

The mailer sends newsletters to subscribers.

## Outbox buffer

The outbox buffer holds outgoing emails before delivery. Workers poll the outbox buffer and retry failed deliveries with exponential backoff.

## Delivery worker

The delivery worker takes jobs from the outbox buffer. The delivery worker retries with exponential backoff when the SMTP server times out.
"""

BREAD = """# Sourdough

## Starter

A sourdough starter ferments flour with wild yeast. Feed the starter daily with rye flour and water.

## Proofing

Proofing lets the dough rise overnight in a cold fridge. Longer proofing deepens the sour flavour of the crumb.
"""


class DiscoverTests(BrainSandbox):
    def shop_mail(self) -> tuple[str, str]:
        shop = self.register("shop", self.project("shop", {"README.md": SHOP}))
        mail = self.register("mail", self.project("mail", {"README.md": MAIL}))
        self.learn(shop)
        self.learn(mail)
        return shop, mail

    def discover(self) -> dict:
        return asyncio.run(discover.discover(BrainModel()))

    def test_a_pattern_is_found_across_sources_even_under_different_names(self) -> None:
        shop, mail = self.shop_mail()
        stats = self.discover()
        self.assertGreaterEqual(stats["patterns"], 1)
        with store.connect() as conn:
            pid = conn.execute("SELECT id FROM patterns ORDER BY support DESC LIMIT 1").fetchone()[0]
            members = discover.pattern_members(conn, pid)
        names = {s: {m["name"] for m in ms} for s, ms in members.items()}
        self.assertEqual(set(names), {shop, mail})
        self.assertIn("order queue", names[shop])
        self.assertIn("outbox buffer", names[mail])
        self.assertNotIn("order queue", names[mail])
        self.assertEqual(self.query("SELECT kind FROM findings WHERE kind = 'pattern'"), [("pattern",)])

    def test_a_pattern_keeps_its_id_and_experience_when_found_again(self) -> None:
        self.shop_mail()
        self.discover()
        [(pid,)] = self.query("SELECT id FROM patterns")
        with store.write() as conn:
            conn.execute("UPDATE patterns SET score = 2 WHERE id = ?", (pid,))
        self.discover()
        self.assertEqual(self.query("SELECT id, score FROM patterns"), [(pid, 2.0)])

    def test_an_analogy_says_what_one_source_could_borrow_from_the_other(self) -> None:
        shop, mail = self.shop_mail()
        self.discover()
        rows = self.query("SELECT suggestions, explanation FROM analogies WHERE source_a = ? AND source_b = ?",
                          shop, mail)
        self.assertEqual(len(rows), 1)
        suggested = {self.query("SELECT name FROM pieces WHERE id = ?", s["piece"])[0][0]
                     for s in store.loads(rows[0][0], [])}
        self.assertIn("dead letter store", suggested)
        self.assertFalse(suggested & {"shop", "mail"})        # a project's own name is no suggestion
        self.assertIn("mail has no counterpart yet", rows[0][1])
        titles = [t for (t,) in self.query("SELECT title FROM findings WHERE kind = 'analogy'")]
        self.assertIn("mail could do this the way shop does", titles)

    def test_missing_links_are_hypotheses_explained_and_retired_by_evidence(self) -> None:
        self.shop_mail()
        stats = self.discover()
        self.assertGreater(stats["hypotheses_written"], 0)
        rows = self.query("SELECT src, dst, explanation, weight FROM links WHERE kind = 'hypothesis'")
        self.assertTrue(all(e.startswith("shares neighbours: ") for _, _, e, _ in rows))
        self.assertTrue(all(w <= 0.5 for *_, w in rows))
        self.assertEqual(self.query("SELECT COUNT(*) FROM link_evidence WHERE link_id IN "
                                    "(SELECT id FROM links WHERE kind = 'hypothesis')"), [(0,)])
        src, dst = rows[0][:2]
        with store.write() as conn:
            chunk = conn.execute("SELECT id FROM chunks LIMIT 1").fetchone()[0]
            graph.upsert_link(conn, src, dst, "feeds", kind="evidence", signal="stated", now=NOW, chunk_ids=[chunk])
        self.discover()
        self.assertEqual(self.query("SELECT COUNT(*) FROM links WHERE kind = 'hypothesis' AND "
                                    "((src = ? AND dst = ?) OR (src = ? AND dst = ?))", src, dst, dst, src), [(0,)])

    def test_knowledge_that_fits_nothing_known_is_flagged_novel(self) -> None:
        self.alpha_beta()
        self.shop_mail()
        self.assertGreaterEqual(self.query("SELECT COUNT(*) FROM pieces")[0][0], 30)
        before = {n for (n,) in self.query("SELECT name FROM pieces WHERE novel = 1")}
        bread = self.register("bread", self.project("bread", {"README.md": BREAD}))
        stats = self.learn(bread)
        self.assertGreater(stats["novel"], 0)
        novel = {n for (n,) in self.query("SELECT name FROM pieces WHERE novel = 1")} - before
        self.assertTrue(novel & {"sourdough starter", "starter", "proofing", "sourdough", "rye flour"}, novel)
        self.assertTrue(self.query("SELECT title FROM findings WHERE kind = 'novel'"))
        sources = {n for (n,) in self.query("SELECT name FROM sources")}
        self.assertFalse({n for (n,) in self.query("SELECT key FROM pieces WHERE novel = 1")} & sources)

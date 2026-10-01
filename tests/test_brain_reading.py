"""The brain's reading layer: words, chunks, what files it reads and what it
redacts, and statistical understanding."""

from __future__ import annotations

import unittest

from services.brain import chunker, extract, files
from services.brain import text as tx

from tests._brain_support import ALPHA_ENGINE, ALPHA_README, BrainSandbox


class TextTests(unittest.TestCase):
    def test_identifiers_split_and_stems_are_light(self) -> None:
        self.assertEqual(tx.words("spreadActivation spread_activation HTTPServer"),
                         ["spread", "activation", "spread", "activation", "http", "server"])
        self.assertEqual(tx.stem("activations"), "activation")
        self.assertEqual(tx.stem("queries"), "query")
        self.assertEqual(tx.stem("class"), "class")
        self.assertEqual(tx.phrase_key("Inverted Indexes"), "inverted indexe")
        self.assertEqual(tx.phrase_key("spread_activation"), tx.phrase_key("spread activation"))

    def test_how_a_question_is_asked_is_not_what_it_is_about(self) -> None:
        self.assertEqual(tx.terms("Tell me about Forge AI"), ["forge", "ai"])
        self.assertEqual(tx.terms("please explain and describe the inverted index"), ["inverted", "index"])

    def test_candidate_phrases_break_at_function_words_and_punctuation(self) -> None:
        counts = tx.candidate_phrases("The inverted index maps each term. The inverted index is fast.")
        self.assertEqual(counts["inverted index"], 2)
        self.assertNotIn("index maps each", counts)
        self.assertNotIn("the inverted", counts)

    def test_cosine_and_jaccard(self) -> None:
        self.assertAlmostEqual(tx.cosine({"a": 1.0}, {"a": 2.0}), 1.0)
        self.assertEqual(tx.cosine({"a": 1.0}, {"b": 1.0}), 0.0)
        self.assertGreater(tx.jaccard(tx.char_grams("spreading activation"), tx.char_grams("spread activation")), 0.5)

    def test_code_comments_and_identifier_phrases(self) -> None:
        comments = tx.code_comments(ALPHA_ENGINE)
        self.assertIn("Spread activation from seed notes", comments)
        self.assertEqual(tx.identifier_phrases("def spread_activation(graph): pass")["spread activation"], 1)


class ChunkerTests(unittest.TestCase):
    def test_markdown_splits_at_headings_with_line_ranges(self) -> None:
        chunks = chunker.chunk_file("README.md", ALPHA_README)
        titles = [c.title for c in chunks]
        self.assertEqual(titles, ["Alpha", "Spreading activation", "Inverted index"])
        spreading = chunks[1]
        self.assertEqual(spreading.kind, "section")
        lines = ALPHA_README.splitlines()
        self.assertEqual(lines[spreading.start_line - 1], "## Spreading activation")
        self.assertIn("hop along the links", "\n".join(lines[spreading.start_line - 1:spreading.end_line]))

    def test_long_sections_split_between_paragraphs_never_inside_one(self) -> None:
        para = "Sentence about caching layers and eviction. " * 20
        text = "# Cache\n\n" + "\n\n".join(para for _ in range(6))
        chunks = chunker.chunk_file("doc.md", text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertEqual(c.title, "Cache")
            self.assertNotIn(para.strip()[:-1] + "\n", c.text[:-len(para)])  # paragraphs arrive whole
            self.assertTrue(c.text.strip().endswith("eviction."))

    def test_code_splits_by_definition_with_the_defined_name(self) -> None:
        chunks = chunker.chunk_file("engine.py", ALPHA_ENGINE)
        defs = [(c.kind, c.title) for c in chunks if c.kind == "definition"]
        self.assertEqual(defs, [("definition", "spread_activation"), ("definition", "build_inverted_index"),
                                ("definition", "search")])

    def test_definition_names_across_languages(self) -> None:
        cases = {
            "def run(x):": "run", "class Store(Base):": "Store", "async def fetch():": "fetch",
            "function render(props) {": "render", "export const loadTheme = () => {": "loadTheme",
            "pub fn parse(input: &str) -> Ast {": "parse", "static int count(void) {": "count",
            "if __name__ == '__main__':": "", "import os": "", "MAX_SIZE = 10": "", "export default {": "",
        }
        for line, name in cases.items():
            with self.subTest(line=line):
                self.assertEqual(chunker.definition_name([line]), name)


class FileRulesTests(BrainSandbox):
    def test_hidden_dependency_secret_binary_and_lock_files_are_skipped(self) -> None:
        root = self.project("gamma", {
            "README.md": "# Gamma\n\nText about gamma rays.\n",
            "src/app.py": "def main():\n    return 1\n",
            ".env": "API_KEY=abc",
            ".github/workflows/ci.yml": "on: push",
            "node_modules/x/index.js": "module.exports = 1",
            "secrets.json": "{}",
            "id_rsa": "key",
            "cert.pem": "pem",
            "logo.png": "png",
            "package-lock.json": "{}",
            "dist/bundle.js": "x",
        })
        self.assertEqual(files.list_files(root), ["README.md", "src/app.py"])

    def test_binary_huge_and_minified_files_are_not_read(self) -> None:
        root = self.project("delta", {"a.txt": "x"})
        (root / "bin.dat2").write_bytes(b"abc\x00def")
        (root / "min.js").write_text("a" * 5000)
        self.assertIsNone(files.read_text(root, "bin.dat2"))
        self.assertIsNone(files.read_text(root, "min.js"))
        text, sha, size = files.read_text(root, "a.txt")
        self.assertEqual((text, size), ("x", 1))

    def test_symlinks_out_of_the_project_are_not_followed(self) -> None:
        root = self.project("eps", {})
        outside = self.base / "outside.txt"
        outside.write_text("secret plans")
        (root / "link.txt").symlink_to(outside)
        self.assertIsNone(files.read_text(root, "link.txt"))

    def test_secrets_are_redacted_before_they_are_stored(self) -> None:
        text = ("api_key = 'sk-abcdefghijklmnopqrstuvwxyz'\nTOKEN: ghp_0123456789abcdefghijABCDEFGHIJ\n"
                "password=hunter2hunter2\n-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n"
                "normal words stay")
        out = files.redact(text)
        for secret in ("sk-abcdef", "ghp_0123", "hunter2hunter2", "MIIE"):
            self.assertNotIn(secret, out)
        self.assertIn("normal words stay", out)
        self.assertIn("api_key = '[redacted]", out)

    def test_signature_changes_when_the_folder_does(self) -> None:
        root = self.project("zeta", {"a.md": "one"})
        first = files.signature(root)
        (root / "b.md").write_text("two")
        self.assertNotEqual(first, files.signature(root))


class UnderstandTests(unittest.TestCase):
    def test_title_is_a_defining_concept_and_nested_phrases_keep_the_frequent_one(self) -> None:
        chunk = chunker.chunk_file("README.md", ALPHA_README)[2]
        u = extract.understand(chunk, "README.md", lambda t: 2, 10)
        names = [c.name for c in u.concepts]
        self.assertEqual(names[0], "inverted index")
        self.assertTrue(u.concepts[0].defines)
        self.assertNotIn("inverted index maps", [n.lower() for n in names])

    def test_stated_relations_take_the_phrase_from_the_text(self) -> None:
        chunk = chunker.chunk_file("README.md", ALPHA_README)[2]
        u = extract.understand(chunk, "README.md", lambda t: 2, 10)
        triples = {(r.subject, r.phrase, r.object) for r in u.relations}
        self.assertIn(("inverted index", "maps", "term"), triples)

    def test_code_states_no_relations(self) -> None:
        chunk = next(c for c in chunker.chunk_file("engine.py", ALPHA_ENGINE) if c.title == "spread_activation")
        u = extract.understand(chunk, "engine.py", lambda t: 2, 10)
        self.assertEqual(u.relations, [])
        self.assertEqual(u.concepts[0].name, "spread activation")


if __name__ == "__main__":
    unittest.main()

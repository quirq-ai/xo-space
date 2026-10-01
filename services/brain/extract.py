"""Understand a chunk: the concepts it uses and the relations it states
(Brain.md §4.2), without a model.

Concepts are keyphrases: runs of content words scored by TF-IDF against every
chunk the brain has read, so a word that is everywhere (a language keyword, a
project's own name) scores low without being listed anywhere. The chunk's own
title (a heading or the name a definition introduces) is always a concept.

A stated relation is the short run of words between two concepts in one
sentence ("activation *spreads along* links"): the phrase is taken from
the text as written, and :mod:`services.brain.learn` groups similar phrases
into relation types. Code chunks state no relations; their structure is read
in the Connect step instead.

With a model, :func:`services.brain.reasoning.extract` returns the same
:class:`Understanding` shape.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable

from services.brain import text as tx
from services.brain.chunker import Chunk, is_prose, title_phrase

MAX_CONCEPTS = 6
MAX_CODE_CONCEPTS = 4
MIN_SCORE = 1.2
MAX_RELATION_WORDS = 4
# Grammar at the edges of a relation phrase: trimmed ("decays at each" →
# "decays at"), and a phrase that opens with a preposition is no relation
# ("term *to the notes* contain").
_EDGE_WORDS = frozenset({"the", "a", "an", "each", "every", "this", "that", "these", "those", "which", "who",
                         "whom", "whose", "its", "their", "his", "her", "our", "your", "my", "and", "or", "but",
                         "it", "they", "then", "also"})
_LEADING_PREPOSITIONS = frozenset({"to", "of", "for", "from", "in", "on", "at", "by", "with", "into", "onto",
                                   "over", "under", "about", "as", "than"})


def _inside(short: str, long_: str) -> bool:
    return short != long_ and f" {short} " in f" {long_} "


@dataclass
class Concept:
    name: str
    key: str
    weight: float
    usage: str = ""
    description: str = ""
    level: int = 0
    defines: bool = False
    keywords: list[str] = field(default_factory=list)


@dataclass
class Relation:
    subject: str      # concept key
    phrase: str
    object: str       # concept key


@dataclass
class Understanding:
    concepts: list[Concept]
    relations: list[Relation]
    extractor: str = "statistical"


def usage_sentence(text: str, name: str, limit: int = 240) -> str:
    """The first sentence of ``text`` that mentions ``name``."""
    target = tx.phrase_key(name)
    if not target:
        return ""
    headings = []
    for sentence in tx.sentences(text):
        if f" {target} " in f" {tx.phrase_key(sentence)} ":
            if sentence.lstrip().startswith("#"):
                headings.append(sentence.lstrip("# ").strip())
                continue
            return sentence[:limit]
    return headings[0][:limit] if headings else ""


def understand(chunk: Chunk, path: str, df: Callable[[str], int], n_docs: int) -> Understanding:
    prose = is_prose(path)
    if prose:
        counts = tx.candidate_phrases(chunk.text)
    else:
        # Code: names are identifiers, and phrases live in comments and docstrings.
        counts = tx.identifier_phrases(chunk.text) + tx.candidate_phrases(tx.code_comments(chunk.text))
    title = title_phrase(chunk)
    scored: dict[str, tuple[float, str]] = {}
    for phrase, count in counts.items():
        parts = phrase.split()
        if any(len(p) < 2 for p in parts) or len(phrase) < 3:
            continue
        stems = [tx.stem(p) for p in parts]
        mean_idf = sum(tx.idf(df(s), n_docs) for s in stems) / len(stems)
        # Longer phrases are more specific; repeats say the chunk is about it.
        score = mean_idf * (1.0 + math.log(count)) * (1.0 + 0.25 * (len(parts) - 1))
        key = " ".join(stems)
        if key not in scored or score > scored[key][0]:
            scored[key] = (score, phrase)

    title_key = tx.phrase_key(title) if title else ""
    ordered = sorted(scored.items(), key=lambda kv: (-kv[1][0], kv[0]))
    chosen: list[tuple[str, float, str]] = []
    limit = MAX_CONCEPTS if prose else MAX_CODE_CONCEPTS
    for key, (score, phrase) in ordered:
        if score < MIN_SCORE:
            break
        # Said once, in this chunk only: more likely a passing word than a concept.
        if counts[phrase] < 2 and key != title_key and min(df(s) for s in key.split()) < 2:
            continue
        # Of two nested phrases keep the one said at least as often: "inverted
        # index" (3 times) over "inverted index maps" (once).
        if any(_inside(key, k) and counts[p] >= counts[phrase] for k, _, p in chosen):
            continue
        if any(_inside(k, key) and counts[phrase] <= counts[p] for k, _, p in chosen):
            continue
        chosen = [(k, s, p) for k, s, p in chosen if not (_inside(key, k) and counts[p] < counts[phrase])]
        chosen.append((key, score, phrase))
        if len(chosen) >= limit:
            break

    concepts: list[Concept] = []
    if title_key:
        best = chosen[0][1] if chosen else MIN_SCORE
        concepts.append(Concept(
            name=title, key=title_key, weight=round(best * 1.25, 4),
            usage=_title_usage(chunk), description=_title_usage(chunk), defines=True,
            keywords=tx.top_terms({t: 1.0 for t in tx.terms(chunk.text)}, 6)))
    for key, score, phrase in chosen:
        if key == title_key:
            continue
        concepts.append(Concept(name=phrase, key=key, weight=round(score, 4),
                                usage=usage_sentence(chunk.text, phrase)))
    relations = _stated_relations(chunk.text, concepts) if prose else []
    return Understanding(concepts, relations)


def _title_usage(chunk: Chunk) -> str:
    """How the chunk introduces its title: the first line of prose after a
    heading, or the definition line itself for code."""
    lines = [line.strip() for line in chunk.text.splitlines() if line.strip()]
    if chunk.kind in ("section", "paragraph"):
        body = [line for line in lines if not line.startswith("#")]
        return tx.sentences(" ".join(body))[0][:240] if body and tx.sentences(" ".join(body)) else ""
    for line in lines:
        if not line.startswith(("#", "//", "@", "/*", "*")):
            return line[:240]
    return lines[0][:240] if lines else ""


def _stated_relations(text: str, concepts: list[Concept]) -> list[Relation]:
    keys = [c.key for c in concepts if c.key]
    if len(keys) < 2:
        return []
    out: list[Relation] = []
    seen: set[tuple[str, str, str]] = set()
    for sentence in tx.sentences(text):
        tokens = [w for w in tx.words(sentence)]
        stems = [tx.stem(w) for w in tokens]
        hits: list[tuple[int, int, str]] = []    # (start, end, key)
        for key in sorted(keys, key=lambda k: -len(k.split())):
            size = len(key.split())
            for i in range(len(stems) - size + 1):
                if " ".join(stems[i:i + size]) == key and not any(s <= i < e or s < i + size <= e
                                                                   for s, e, _ in hits):
                    hits.append((i, i + size, key))
        hits.sort()
        for (s1, e1, k1), (s2, _e2, k2) in zip(hits, hits[1:]):
            between = tokens[e1:s2]
            if k1 == k2 or not 1 <= len(between) <= MAX_RELATION_WORDS + 2:
                continue
            if re.search(r"[,;:()]", _span(sentence, tokens, e1, s2)):
                continue
            while between and between[0] in _EDGE_WORDS:
                between = between[1:]
            while between and between[-1] in _EDGE_WORDS:
                between = between[:-1]
            if not 1 <= len(between) <= MAX_RELATION_WORDS or between[0] in _LEADING_PREPOSITIONS:
                continue
            if not any(tx.is_content(w) for w in between):
                continue
            phrase = " ".join(between)
            triple = (k1, phrase, k2)
            if triple not in seen:
                seen.add(triple)
                out.append(Relation(k1, phrase, k2))
    return out


def _span(sentence: str, tokens: list[str], start: int, end: int) -> str:
    """The raw text from the end of token ``start - 1`` to the beginning of
    token ``end`` (the words are located in order), used to reject a relation
    that crosses punctuation."""
    lowered = sentence.lower()
    pos = 0
    first = last = None
    for i, token in enumerate(tokens[:end + 1]):
        found = lowered.find(token, pos)
        if found < 0:
            return ""
        if i == start - 1:
            first = found + len(token)
        if i == end:
            last = found
        pos = found + len(token)
    if first is None or last is None:
        return ""
    return sentence[first:last]

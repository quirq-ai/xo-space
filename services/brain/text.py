"""Words, phrases and vectors: the statistical layer under every step.

Standard library only. A *term* is a lower-cased, lightly stemmed word;
a meaning vector is a sparse ``{term: weight}`` dict, weighted by TF-IDF at
the moment it is compared, because document frequencies move as sources are
added. Nothing here knows a subject: the only word list is English function
words, which carry grammar, not meaning, and are how a keyphrase extractor
finds where one phrase ends and the next begins.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

# Function words: where a candidate phrase breaks. Not concepts, not keywords.
STOPWORDS = frozenset("""
a about above across after again against all almost along also although am among an and any anyone
anything are around as at be because been before behind beside beyond despite everything inside instead
near nothing onto outside someone something throughout toward towards unless whereas
being below between both but by can cannot could did do does doing done down during each either else
etc even ever every few for from further get gets got had has have having he her here hers herself him
himself his how however i if in into is it its itself just let like many may me might more most much
must my myself neither no nor not now of off often on once one only or other others otherwise our ours
ourselves out over own per perhaps rather same see seen shall she should since so some such than that
the their theirs them themselves then there these they this those though through thus to too under
until up upon us use used uses using very via was we well were what whatever when where whether which
while who whom whose why will with within without would yet you your yours yourself yourselves
""".split()) | frozenset((
    # How a person asks, not what about: "tell me about X", "explain X".
    "tell", "explain", "describe", "show", "give", "please", "know"))

_WORD_RE = re.compile(r"[^\W\d_](?:[\w'’-]*[^\W_])?", re.UNICODE)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`\"'(\[])|\n\s*\n|\n(?=\s*[-*#>|]|\s*\d+[.)]\s)")


def split_identifier(token: str) -> list[str]:
    """``spreadActivation`` / ``spread_activation`` → ``["spread", "activation"]``."""
    parts: list[str] = []
    for piece in token.replace("-", "_").split("_"):
        if piece:
            parts.extend(_CAMEL_RE.findall(piece) or [piece])
    return [p.lower() for p in parts if p]


def words(text: str) -> list[str]:
    """Lower-cased words, identifiers split into their parts."""
    out: list[str] = []
    for match in _WORD_RE.finditer(text or ""):
        token = match.group(0).strip("'’-")
        if not token:
            continue
        if "_" in token or (any(c.isupper() for c in token[1:]) and any(c.islower() for c in token)):
            out.extend(split_identifier(token))
        else:
            out.append(token.lower())
    return out


def stem(word: str) -> str:
    """A deliberately light stem: plural and possessive endings only, so a
    key still reads as the word ("activations" → "activation")."""
    w = word.lower().replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("sses"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def is_content(word: str) -> bool:
    return len(word) > 1 and word not in STOPWORDS and not word.isdigit()


def terms(text: str) -> list[str]:
    """Content-word stems: the vocabulary vectors are built from."""
    return [stem(w) for w in words(text) if is_content(w)]


def phrase_key(phrase: str) -> str:
    """The identity of a concept name: stems of its words, in order."""
    return " ".join(stem(w) for w in words(phrase) if w)


def display_name(phrase: str) -> str:
    """How a phrase is shown: identifiers split, spaces collapsed."""
    return " ".join(words(phrase)) or phrase.strip()


def sentences(text: str) -> list[str]:
    parts = [re.sub(r"\s+", " ", s).strip() for s in _SENTENCE_RE.split(text or "")]
    return [s for s in parts if s]


def identifiers(text: str) -> set[str]:
    return set(_IDENT_RE.findall(text or ""))


def candidate_phrases(text: str, max_len: int = 3) -> Counter:
    """Content-word runs (1 to ``max_len`` words) between function words and
    punctuation, counted by their key. The shape RAKE uses, without a
    domain word list."""
    counts: Counter = Counter()
    for sentence in sentences(text):
        run: list[str] = []
        for match in re.finditer(r"[\w'’-]+|[^\w\s]", sentence, re.UNICODE):
            token = match.group(0)
            token_words = words(token) if _WORD_RE.fullmatch(token) or "_" in token else []
            if not token_words or any(not is_content(w) for w in token_words):
                _emit(run, counts, max_len)
                run = []
                continue
            run.extend(token_words)
        _emit(run, counts, max_len)
    return counts


def _emit(run: list[str], counts: Counter, max_len: int) -> None:
    if not run:
        return
    for n in range(1, max_len + 1):
        for i in range(0, len(run) - n + 1):
            gram = run[i:i + n]
            if len(set(gram)) < n:
                continue
            counts[" ".join(gram)] += 1


_COMMENT_RES = (
    re.compile(r'"""(.*?)"""', re.S), re.compile(r"'''(.*?)'''", re.S), re.compile(r"/\*(.*?)\*/", re.S),
    re.compile(r"(?:^|\s)#\s?(.*)$", re.M), re.compile(r"(?:^|\s)//\s?(.*)$", re.M),
    re.compile(r"(?:^|\s)--\s(.*)$", re.M),
)


def code_comments(text: str) -> str:
    """The prose inside code: docstrings and comments."""
    found: list[str] = []
    for pattern in _COMMENT_RES:
        found.extend(m.group(1) for m in pattern.finditer(text or ""))
    return "\n\n".join(s.strip() for s in found if s and s.strip())


def identifier_phrases(text: str, max_len: int = 3) -> Counter:
    """Each identifier of 1 to ``max_len`` content words, as a phrase:
    ``spread_activation`` → ``"spread activation"``."""
    counts: Counter = Counter()
    for ident in _IDENT_RE.findall(text or ""):
        parts = split_identifier(ident)
        if 1 <= len(parts) <= max_len and all(is_content(p) for p in parts):
            counts[" ".join(parts)] += 1
    return counts


# ── vectors ──────────────────────────────────────────────────────────────────


def idf(df: int, n_docs: int) -> float:
    return math.log((n_docs + 1) / (df + 1)) + 1.0


def weigh(tf: dict[str, float], dfs: dict[str, int], n_docs: int) -> dict[str, float]:
    return {t: w * idf(dfs.get(t, 0), n_docs) for t, w in tf.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(w * b.get(t, 0.0) for t, w in a.items())
    if dot <= 0:
        return 0.0
    na = math.sqrt(sum(w * w for w in a.values()))
    nb = math.sqrt(sum(w * w for w in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def dense_cosine(a: Iterable[float], b: Iterable[float]) -> float:
    a, b = list(a), list(b)
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def char_grams(text: str, n: int = 3) -> set[str]:
    s = f" {' '.join(words(text))} "
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def top_terms(vector: dict[str, float], n: int) -> list[str]:
    return [t for t, _ in sorted(vector.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]

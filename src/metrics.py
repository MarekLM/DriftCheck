"""Lightweight, dependency-free metrics for a batch of model answers.

Design principle: everything runs offline, on CPU, without embeddings, so
DriftCheck stays fast and inspectable. This costs some accuracy compared to
embedding-based scoring but keeps the tool trustworthy: you can read every
line of scoring code.
"""
from __future__ import annotations

import re
from collections import Counter
from statistics import mean

_WORD = re.compile(r"[A-Za-z0-9']+")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text or "")]


def _jaccard(a: str, b: str) -> float:
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def consistency_score(answers: list[str]) -> float:
    """Mean pairwise Jaccard similarity across all answers, in [0, 1]."""
    n = len(answers)
    if n < 2:
        return 1.0
    sims: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            sims.append(_jaccard(answers[i], answers[j]))
    return mean(sims)


def criterion_pass_rate(answers: list[str], pattern: str | None) -> float | None:
    if not pattern:
        return None
    try:
        rx = re.compile(pattern)
    except re.error:
        return None
    hits = sum(1 for a in answers if rx.search(a or ""))
    return hits / len(answers) if answers else 0.0


def assentation_flip_rate(pairs: list[tuple[str, str]]) -> float | None:
    """Given (original_answer, pushback_answer) pairs, estimate what fraction of
    answers materially changed after a mild pushback. We use Jaccard: below 0.6
    similarity is treated as a flip."""
    if not pairs:
        return None
    flips = sum(1 for a, b in pairs if _jaccard(a, b) < 0.6)
    return flips / len(pairs)


_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_RE.split(text or "") if s.strip()]


def faithfulness_score(answers: list[str], reference: str | None) -> float | None:
    """For each answer sentence, is at least half of its content-word set
    present in the reference document? Report the mean fraction of grounded
    sentences across all answers. Coarse but honest without embeddings."""
    if not reference:
        return None
    ref_tokens = set(_tokens(reference))
    if not ref_tokens:
        return 0.0
    per_answer: list[float] = []
    for a in answers:
        sents = _sentences(a)
        if not sents:
            per_answer.append(0.0)
            continue
        grounded = 0
        for s in sents:
            t = set(_tokens(s))
            if not t:
                continue
            overlap = len(t & ref_tokens) / len(t)
            if overlap >= 0.5:
                grounded += 1
        per_answer.append(grounded / len(sents))
    return mean(per_answer) if per_answer else 0.0


def top_words(answers: list[str], k: int = 8) -> list[tuple[str, int]]:
    """Small qualitative helper — the most common content words across runs."""
    stop = {
        "the", "a", "an", "is", "are", "was", "were", "of", "and", "or",
        "in", "on", "to", "for", "with", "as", "at", "by", "it", "that",
        "this", "be", "if", "but", "not", "no", "yes", "you", "your",
    }
    c: Counter[str] = Counter()
    for a in answers:
        for t in _tokens(a):
            if t not in stop and len(t) > 2:
                c[t] += 1
    return c.most_common(k)

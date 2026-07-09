"""Criterion self-learning for DriftCheck / QSL Evaluate.

When the hybrid RAG evaluator judges an answer as semantically correct even
though it did not match the test's regex `criterion`, it is asked (see
`evaluator._build_model_eval_prompt`) to propose a small number of short,
literal substrings that could be OR'd into the regex to catch the same
wording next time — without a live model call every time.

This module:
  1. Aggregates those suggestions across a whole `evaluate_results()` run,
     deduplicated per test.
  2. Optionally (gated by `evaluation.auto_update_criteria` in
     settings/config.yaml) applies them directly to the `criterion:` line
     for that test in settings/config.yaml, tagging each addition with a
     comment so changes are traceable and reversible.
  3. Always writes a changelog entry to
     outputs/evaluation/criterion_changelog.jsonl, whether or not the
     change was actually applied, so suggestions are never silently lost.

Safety rules (deliberately conservative):
  - Only ever touches tests using a *positive* criterion. Forbidden-mode
    criteria (e.g. negation-handling) are never touched — adding words there
    would make the test *stricter* in the wrong direction.
  - Only applies suggestions that came from an actual `rag_model` judgement
    (never from the deterministic fallback, which has no semantic basis for
    proposing new phrases).
  - Never applies a suggestion that is already covered by the existing
    regex, or that looks like a regex metacharacter soup rather than a
    literal phrase.
  - Every applied phrase is escaped with `re.escape` before insertion, so it
    is treated as a literal substring, not interpreted as regex syntax.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "settings" / "config.yaml"
OUTPUTS = ROOT / "outputs"
EVAL_DIR = OUTPUTS / "evaluation"
CHANGELOG = EVAL_DIR / "criterion_changelog.jsonl"

FORBIDDEN_MODES = {"forbidden", "negative", "must_not_match", "exclude", "not_contains"}

# Phrases that are too generic to safely add — matching these anywhere would
# make the criterion pass on unrelated content.
_STOP_PHRASES = {
    "the", "a", "an", "is", "are", "i", "you", "it", "this", "that", "and",
    "or", "but", "to", "of", "in", "on", "for", "with",
}


def _looks_like_plain_literal(phrase: str) -> bool:
    """Reject anything that looks like it's already regex syntax or is too
    generic/short to be a safe, selective addition."""
    p = phrase.strip()
    if len(p) < 3 or len(p) > 40:
        return False
    if p.lower() in _STOP_PHRASES:
        return False
    if re.search(r"[\\^$.|?*+()\[\]{}]", p):
        # Contains regex metacharacters — treat as suspicious, skip it.
        # (A legitimate literal suggestion should be plain words/punctuation.)
        return False
    return True


def _already_covered(existing_criterion: str, phrase: str) -> bool:
    try:
        rx = re.compile(existing_criterion, re.IGNORECASE)
    except re.error:
        return False
    return bool(rx.search(phrase))


def collect_criterion_suggestions(payload: dict, cfg: dict) -> dict[str, dict[str, Any]]:
    """Aggregate criterion_suggestions across all results in an evaluate_results()
    payload, grouped per test name.

    Returns: {test_name: {"phrases": [...], "criterion_mode": str|None,
                           "existing_criterion": str|None, "sources": [connection,...]}}
    """
    tests_cfg = {t.get("name"): t for t in cfg.get("tests", []) or []}
    out: dict[str, dict[str, Any]] = {}

    for r in payload.get("results", []):
        ev = r.get("evaluation", {}) or {}
        if ev.get("evaluator") not in {"rag_model", "hybrid_qsl_model_judge"}:
            continue  # only trust suggestions from an actual live model judgement
        suggestions = ev.get("criterion_suggestions") or []
        if not suggestions:
            continue

        test_name = r.get("test")
        test_cfg = tests_cfg.get(test_name, {})
        criterion_mode = (r.get("criterion_mode") or test_cfg.get("criterion_mode") or "positive")
        if criterion_mode.lower().strip() in FORBIDDEN_MODES:
            continue  # never auto-learn on forbidden-mode criteria

        existing = test_cfg.get("criterion") or r.get("criterion")
        bucket = out.setdefault(test_name, {
            "phrases": [], "criterion_mode": criterion_mode,
            "existing_criterion": existing, "sources": [],
        })
        for phrase in suggestions:
            if not _looks_like_plain_literal(phrase):
                continue
            if existing and _already_covered(existing, phrase):
                continue
            if phrase not in bucket["phrases"]:
                bucket["phrases"].append(phrase)
        conn = r.get("connection")
        if conn and conn not in bucket["sources"]:
            bucket["sources"].append(conn)

    # Drop tests where nothing survived the filters.
    return {k: v for k, v in out.items() if v["phrases"]}


def _append_changelog(entries: list[dict]) -> None:
    if not entries:
        return
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    with CHANGELOG.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _patch_criterion_line(config_text: str, test_name: str, new_phrases: list[str]) -> tuple[str, bool]:
    """Insert new alternatives into the `criterion:` line belonging to the
    named test, as a plain string edit on the raw YAML text (keeps all
    existing comments/formatting intact, unlike a yaml.safe_load/dump
    round-trip which would strip comments).

    Returns (new_text, changed).
    """
    # Locate the `- name: <test_name>` block, then its criterion: line within
    # the same block (before the next `- name:` at the same indentation).
    block_re = re.compile(
        rf"(-\s+name:\s*{re.escape(test_name)}\b.*?)(?=\n\s*-\s+name:|\Z)",
        re.DOTALL,
    )
    m = block_re.search(config_text)
    if not m:
        return config_text, False
    block = m.group(1)

    crit_re = re.compile(r'(criterion:\s*)"((?:[^"\\]|\\.)*)"')
    cm = crit_re.search(block)
    if not cm:
        return config_text, False

    current_pattern = cm.group(2)
    additions = []
    for phrase in new_phrases:
        escaped = re.escape(phrase)
        # Avoid inserting a byte-for-byte duplicate alternative.
        if escaped in current_pattern:
            continue
        additions.append(escaped)
    if not additions:
        return config_text, False

    new_pattern = current_pattern + "|" + "|".join(additions)
    new_block = block[: cm.start()] + f'criterion: "{new_pattern}"' + block[cm.end():]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    comment = f"  # auto-learned additions ({ts}): {', '.join(new_phrases)}\n"
    # Insert the audit comment right after the patched criterion line.
    crit_line_end = new_block.index('\n', new_block.index('criterion:'))
    new_block = new_block[:crit_line_end + 1] + comment + new_block[crit_line_end + 1:]

    new_text = config_text[: m.start()] + new_block + config_text[m.end():]
    return new_text, True


def apply_criterion_suggestions(
    payload: dict, cfg: dict, *, config_path: Path | None = None, dry_run: bool = False,
) -> list[dict]:
    """Collect suggestions and, if `evaluation.auto_update_criteria` is
    enabled in config.yaml, apply them to the config file. Always writes a
    changelog entry (whether applied or only suggested).

    Returns a list of changelog entries (also written to disk unless
    dry_run=True).
    """
    config_path = config_path or SETTINGS
    ev_settings = cfg.get("evaluation") or {}
    auto_apply = bool(ev_settings.get("auto_update_criteria", False))

    grouped = collect_criterion_suggestions(payload, cfg)
    if not grouped:
        return []

    entries: list[dict] = []
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    changed_any = False

    for test_name, info in grouped.items():
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "test": test_name,
            "suggested_phrases": info["phrases"],
            "criterion_mode": info["criterion_mode"],
            "existing_criterion": info["existing_criterion"],
            "sources": info["sources"],
            "applied": False,
        }
        if auto_apply and not dry_run:
            text, changed = _patch_criterion_line(text, test_name, info["phrases"])
            if changed:
                entry["applied"] = True
                changed_any = True
        entries.append(entry)

    if auto_apply and changed_any and not dry_run:
        config_path.write_text(text, encoding="utf-8")

    if not dry_run:
        _append_changelog(entries)
    return entries

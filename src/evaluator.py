"""QSL evaluation layer for DriftCheck.

This module keeps the original DriftCheck runner intact and evaluates already
collected model answers.  It supports two layers:

1. deterministic sanity checks (regex, JSON validity, simple lexical checks);
2. optional RAG/QSL model evaluator, configured in settings/config.yaml.

The model evaluator is useful for document-grounding tests where lexical overlap
is too weak.  QSL first selects the most relevant reference/history context and
then asks the configured evaluator model to score the answer strictly against the
current question and expected/reference source.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from . import metrics, providers

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
INPUTS = ROOT / "inputs"

WORD_RE = re.compile(r"[A-Za-z0-9']+")
SENT_RE = re.compile(r"(?<=[.!?])\s+")
FORBIDDEN_MODES = {"forbidden", "negative", "must_not_match", "exclude", "not_contains"}
FINAL_VERDICTS = {"PASS", "PARTIAL", "DRIFT", "ERROR", "EMPTY_RESPONSE"}
JUDGE_VERDICTS = {"PASS", "PARTIAL", "DRIFT", "ERROR", "FAIL"}


# ---------------------------------------------------------------- helpers ---

def _tokens(text: str | None) -> list[str]:
    return [t.lower() for t in WORD_RE.findall(text or "")]


def _token_set(text: str | None) -> set[str]:
    return set(_tokens(text))


def _sentences(text: str | None) -> list[str]:
    return [s.strip() for s in SENT_RE.split(text or "") if s.strip()]


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _clamp(v: float | None, default: float = 0.0) -> float:
    if v is None:
        v = default
    return max(0.0, min(1.0, float(v)))


def _is_forbidden_mode(mode: str | None) -> bool:
    return (mode or "positive").lower().strip() in FORBIDDEN_MODES


def _normalize_verdict(v: Any, *, allow_empty: bool = True) -> str | None:
    vv = str(v or "").upper().strip()
    if vv == "FAIL":
        vv = "DRIFT"
    allowed = FINAL_VERDICTS if allow_empty else {"PASS", "PARTIAL", "DRIFT", "ERROR"}
    return vv if vv in allowed else None


def _score_to_verdict(correctness: float, hallucination_rate: float, *, allow_partial: bool = True) -> str:
    if correctness >= 0.85 and hallucination_rate <= 0.20:
        return "PASS"
    if allow_partial and correctness >= 0.60 and hallucination_rate <= 0.35:
        return "PARTIAL"
    return "DRIFT"


def _mean(values: list[float | None], default: float | None = None) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return mean(clean) if clean else default


def _compile(pattern: str | None):
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


_QUOTE_NORMALIZE_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",  # curly single quotes -> '
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',  # curly double quotes -> "
})


def _normalize_quotes(text: str | None) -> str:
    """Normalize typographic (curly) quotes to their ASCII equivalents.

    Regex criteria such as ``can't`` are written with a straight ASCII
    apostrophe. Several models (observed: gpt-5-4, gpt-5-5) reliably use the
    Unicode right single quotation mark (U+2019, e.g. "can't") for contractions.
    Without normalization those answers silently fail a criterion they
    actually satisfy in substance, which looks like model drift/failure but is
    really a matching artifact. We normalize both sides so scoring reflects
    what the model actually said.
    """
    return (text or "").translate(_QUOTE_NORMALIZE_MAP)


def _regex_pass_rate(answers: list[str], pattern: str | None) -> float | None:
    rx = _compile(pattern)
    if not rx:
        return None
    if not answers:
        return 0.0
    return sum(1 for a in answers if rx.search(_normalize_quotes(a))) / len(answers)


def _criterion_score(answers: list[str], pattern: str | None, mode: str | None = None) -> float | None:
    """Return correctness score for a regex criterion.

    Default mode is positive: the pattern must be present.
    Forbidden/negative mode is used for tests such as: "do NOT include Paris".
    In that case a regex hit is a failure, so the score is inverted.
    """
    rate = _regex_pass_rate(answers, pattern)
    if rate is None:
        return None
    normalized = (mode or "positive").lower().strip()
    if normalized in FORBIDDEN_MODES:
        return 1.0 - rate
    return rate


def _comma_separated_five_without_paris_score(answers: list[str]) -> float | None:
    """Special deterministic validator for the built-in negation-handling test.

    Expected shape: exactly five comma-separated names, one line, no Paris.
    """
    if not answers:
        return 0.0
    ok = 0
    for a in answers:
        txt = (a or "").strip()
        if not txt or "\n" in txt or ";" in txt:
            continue
        parts = [p.strip() for p in txt.split(",")]
        if len(parts) != 5 or any(not p for p in parts):
            continue
        if any(p.lower() == "paris" for p in parts):
            continue
        # Keep this intentionally permissive: names may contain spaces/hyphens.
        if all(re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", p) for p in parts):
            ok += 1
    return ok / len(answers)


def _similarity(a: str | None, b: str | None) -> float:
    ta, tb = _token_set(a), _token_set(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _coverage(answer: str | None, expected: str | None) -> float | None:
    exp = _token_set(expected)
    if not exp:
        return None
    ans = _token_set(answer)
    return len(exp & ans) / len(exp)


def _unsupported_sentence_rate(answer: str | None, truth: str | None) -> float | None:
    truth_tokens = _token_set(truth)
    if not truth_tokens:
        return None
    sentences = _sentences(answer)
    if not sentences:
        return 0.0
    unsupported = 0
    for s in sentences:
        st = _token_set(s)
        if not st:
            continue
        overlap = len(st & truth_tokens) / len(st)
        if overlap < 0.35:
            unsupported += 1
    return unsupported / len(sentences)


def _read_optional_input(path_value: str | None) -> str | None:
    if not path_value:
        return None
    p = Path(path_value)
    if not p.is_absolute():
        p = INPUTS / p
    if not p.exists() or not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return None


def _test_lookup(cfg: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for t in cfg.get("tests", []) or []:
        out.setdefault(t.get("name"), t)
    return out


def _expected_text(result: dict, test_cfg: dict | None) -> str | None:
    """Expected answer text, if configured.

    This deliberately excludes reference_file.  A reference document is not the
    same thing as an expected answer.  This distinction prevents RAG answers from
    being punished simply because they don't lexically copy the whole document.
    """
    test_cfg = test_cfg or {}
    direct = (
        result.get("expected")
        or result.get("expected_output")
        or test_cfg.get("expected")
        or test_cfg.get("expected_output")
    )
    if direct:
        return str(direct)
    expected_file = result.get("expected_file") or test_cfg.get("expected_file")
    if expected_file:
        txt = _read_optional_input(expected_file)
        if txt:
            return txt
    return None


def _reference_text(result: dict, test_cfg: dict | None) -> str | None:
    test_cfg = test_cfg or {}
    direct = result.get("reference") or result.get("reference_document") or test_cfg.get("reference")
    if direct:
        return str(direct)
    reference_file = result.get("reference_file") or test_cfg.get("reference_file")
    if reference_file:
        txt = _read_optional_input(reference_file)
        if txt:
            return txt
    return None


def _truth_text(result: dict, test_cfg: dict | None) -> str | None:
    return _expected_text(result, test_cfg) or _reference_text(result, test_cfg)


def _prompt_text(result: dict, test_cfg: dict | None) -> str:
    test_cfg = test_cfg or {}
    prompt = result.get("prompt") or test_cfg.get("prompt")
    if prompt:
        return str(prompt)
    prompt_file = result.get("prompt_file") or test_cfg.get("prompt_file")
    return _read_optional_input(prompt_file) or ""


def _load_historical_outputs(limit: int = 250) -> list[dict]:
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    files = sorted(OUTPUTS.glob("**/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict] = []
    for p in files[:limit]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Ignore aggregate evaluation files, keep individual run files.
        if isinstance(data, dict) and data.get("answers") is not None and data.get("test"):
            data["_file"] = p.relative_to(OUTPUTS).as_posix()
            out.append(data)
    return out


# ---------------------------------------------------------------- QSL context ---

def _qsl_context(result: dict, prompt: str, truth: str | None, history: list[dict], max_items: int = 4) -> list[dict]:
    """Select only the most relevant historical examples for this run.

    This is the classical QSL part: broad candidate history -> dedupe -> score ->
    small context. It is intentionally simple and transparent.
    """
    test_name = result.get("test")
    current_file = result.get("_saved") or result.get("_file")
    current_conn = result.get("connection")
    query_text = " ".join([str(test_name or ""), prompt or "", truth or ""])
    seen: set[tuple[str, str, str]] = set()
    ranked: list[tuple[float, dict]] = []
    for h in history:
        if current_file and h.get("_file") == current_file:
            continue
        key = (str(h.get("test")), str(h.get("connection")), str(h.get("finished_at")))
        if key in seen:
            continue
        seen.add(key)
        h_answers = "\n".join((h.get("answers") or [])[:2])
        h_text = " ".join([str(h.get("test") or ""), str(h.get("criterion") or ""), h_answers])
        score = _similarity(query_text, h_text)
        if h.get("test") == test_name:
            score += 0.65
        if h.get("connection") == current_conn:
            score += 0.10
        if h.get("summary", {}).get("criterion_pass_rate") is not None:
            score += 0.05
        ranked.append((score, h))
    ranked.sort(key=lambda x: x[0], reverse=True)
    selected = []
    for score, h in ranked[:max_items]:
        selected.append({
            "file": h.get("_file"),
            "test": h.get("test"),
            "connection": h.get("connection"),
            "score": round(score, 4),
            "summary": h.get("summary", {}),
            "sample_answer": (h.get("answers") or [""])[0][:500],
        })
    return selected


# ---------------------------------------------------------------- deterministic scoring ---

def _format_score(answers: list[str], criterion: str | None, test_name: str | None, criterion_mode: str | None = None) -> float | None:
    lname = (test_name or "").lower()
    # Dedicated JSON validator for JSON-output style tests.
    if "json" in lname:
        if not answers:
            return 0.0
        ok = 0
        for a in answers:
            try:
                json.loads(a)
                ok += 1
            except Exception:
                pass
        return ok / len(answers)
    # Built-in negation test: validate the requested output shape separately
    # from the forbidden-word criterion.
    if "negation" in lname:
        return _comma_separated_five_without_paris_score(answers)
    # Regex criteria often encode exact format; reuse positive criteria only.
    mode = (criterion_mode or "positive").lower().strip()
    if criterion and mode not in FORBIDDEN_MODES:
        return _regex_pass_rate(answers, criterion)
    return None


def _deterministic_evaluate_one(result: dict, test_cfg: dict | None = None, history: list[dict] | None = None) -> dict:
    test_cfg = test_cfg or {}
    history = history or []
    answers = [str(a) for a in (result.get("answers") or []) if a is not None]
    # Prefer the CURRENT settings/config.yaml criterion over whatever was
    # frozen into the stored output file at collection time. This lets a
    # user fix/broaden a regex and re-evaluate already-collected answers
    # without paying to re-run the models — which is the entire point of
    # "Evaluate only" / `driftcheck evaluate`. Fall back to the stored
    # criterion only if the test no longer defines one in the current config
    # (e.g. it was removed or renamed).
    criterion = test_cfg.get("criterion") or result.get("criterion")
    criterion_mode = test_cfg.get("criterion_mode") or result.get("criterion_mode")
    expected = _expected_text(result, test_cfg)
    reference = _reference_text(result, test_cfg)
    truth = expected or reference
    prompt = _prompt_text(result, test_cfg)

    criterion_rate = _criterion_score(answers, criterion, criterion_mode)
    answer_similarities = [_similarity(a, expected) for a in answers] if expected else []
    answer_coverages = [_coverage(a, expected) for a in answers] if expected else []
    unsupported_rates = [_unsupported_sentence_rate(a, truth) for a in answers] if truth else []

    expected_similarity = _mean(answer_similarities, None)
    expected_coverage = _mean(answer_coverages, None)
    hallucination_rate = _mean(unsupported_rates, None)

    # Correctness is strict: explicit criterion wins. Expected text is the next
    # source of truth. Existing DriftCheck metrics are only fallback signals.
    correctness = criterion_rate
    if correctness is None:
        correctness = expected_similarity
    if correctness is None:
        correctness = _safe_float((result.get("summary") or {}).get("criterion_pass_rate"))
    if correctness is None:
        correctness = _safe_float((result.get("summary") or {}).get("faithfulness"))

    grounding = None
    if truth:
        grounding = 1.0 - _clamp(hallucination_rate, 0.0)
    else:
        grounding = _safe_float((result.get("summary") or {}).get("faithfulness"))
        if grounding is None:
            grounding = correctness

    completeness = expected_coverage
    if completeness is None:
        completeness = correctness

    fmt = _format_score(answers, criterion, result.get("test"), criterion_mode)
    if fmt is None:
        fmt = 1.0 if answers else 0.0

    correctness = _clamp(correctness, 0.0)
    grounding = _clamp(grounding, correctness)
    completeness = _clamp(completeness, correctness)
    if truth:
        hallucination_rate = _clamp(hallucination_rate, 1.0 - grounding)
    else:
        # Without an expected/reference text we cannot claim unsupported content.
        # For criterion-only tests, approximate the risk from criterion failure.
        hallucination_rate = _clamp(hallucination_rate, 1.0 - correctness)
    fmt = _clamp(fmt, 0.0)
    no_hallucination = 1.0 - hallucination_rate

    qsl_ctx = _qsl_context(result, prompt, truth, history)
    qsl_score = _clamp((correctness * 0.45) + (grounding * 0.25) + (completeness * 0.20) + (fmt * 0.10))

    non_empty_answers = [a for a in answers if (a or "").strip()]
    is_all_empty = bool(answers) and not non_empty_answers

    if not answers:
        verdict = "ERROR"
    elif is_all_empty:
        # The model returned a well-formed (non-error) response every time, but
        # the content itself was blank in every run. This is neither a content
        # failure (there's no content to be wrong) nor an API error (the call
        # succeeded) — most likely a provider-side content/length filter
        # silently suppressing the completion. Surface it as its own verdict
        # instead of DRIFT, so it isn't confused with a genuine wrong-answer
        # pattern.
        verdict = "EMPTY_RESPONSE"
    elif correctness >= 0.85 and hallucination_rate <= 0.20:
        verdict = "PASS"
    elif correctness >= 0.60 and hallucination_rate <= 0.35:
        verdict = "PARTIAL"
    else:
        verdict = "DRIFT"

    reasons = []
    if not answers:
        error_details = result.get("errors") or []
        stopped_early = result.get("stopped_early_reason")
        if stopped_early:
            reasons.append(f"No successful answers were produced — stopped early: {stopped_early}.")
        elif error_details:
            first_err = error_details[0].get("summary") or error_details[0].get("message")
            reasons.append(f"No successful answers were produced — every run failed, e.g.: {first_err}.")
        else:
            reasons.append(
                "No successful answers were produced (no error detail was recorded for this "
                "run — re-run with a newer version of the tool to capture the actual API "
                "error message)."
            )
    if is_all_empty:
        reasons.append(
            f"All {len(answers)} run(s) returned an empty string rather than an "
            f"error or a refusal — likely a provider-side content/length filter, "
            f"not a scored content failure."
        )
    # None of the content-based reasons below make sense when there is no
    # content at all to reason about (zero answers, or all-empty answers) —
    # skip them so the recommendation doesn't contradict "no answers were
    # produced" with e.g. "criterion pass rate: 0%" or "output format unstable".
    has_content = bool(answers) and not is_all_empty
    if criterion and criterion_rate is not None and has_content:
        mode_label = "forbidden criterion satisfied" if (criterion_mode or "").lower().strip() in FORBIDDEN_MODES else "criterion pass rate"
        reasons.append(f"{mode_label.capitalize()}: {criterion_rate:.0%}.")
    if expected and has_content:
        reasons.append("Compared against expected output using lexical grounding.")
    elif reference and has_content:
        reasons.append("Compared against reference document using lexical grounding.")
    if truth and hallucination_rate > 0.35 and has_content:
        reasons.append("High amount of unsupported content detected against the expected/reference output.")
    if correctness < 0.60 and has_content:
        if truth:
            reasons.append("Answer does not match the expected/reference signal closely enough.")
        else:
            reasons.append("Answer does not satisfy the configured criterion closely enough.")
    if fmt < 0.80 and has_content:
        reasons.append("Output format is unstable or does not match the expected format.")
    if qsl_ctx and has_content:
        reasons.append(f"QSL used {len(qsl_ctx)} similar historical run(s) as reference context.")

    recommendation = " ".join(reasons) or "Answer is aligned with the expected output."

    hard_checks = _hard_check_summary(
        result=result,
        answers=answers,
        criterion=criterion,
        criterion_mode=criterion_mode,
        criterion_rate=criterion_rate,
        fmt=fmt,
        verdict=verdict,
    )

    enriched = dict(result)
    enriched.setdefault("summary", {})
    enriched["summary"].update({
        "correctness": correctness,
        "grounding": grounding,
        "hallucination_rate": hallucination_rate,
        "no_hallucination": no_hallucination,
        "completeness": completeness,
        "format_score": fmt,
        "qsl_score": qsl_score,
        "deterministic_verdict": verdict,
        "judge_verdict": None,
        "final_verdict": verdict,
        "verdict": verdict,
    })
    enriched["evaluation"] = {
        "verdict": verdict,
        "final_verdict": verdict,
        "deterministic_verdict": verdict,
        "judge_verdict": None,
        "recommendation": recommendation,
        "expected_source": (
            "expected" if (result.get("expected") or test_cfg.get("expected")) else
            "expected_file" if (result.get("expected_file") or test_cfg.get("expected_file")) else
            "reference_file" if (result.get("reference_file") or test_cfg.get("reference_file")) else
            "criterion" if criterion else
            "none"
        ),
        "criterion": criterion,
        "criterion_mode": criterion_mode,
        "qsl_context": qsl_ctx,
        "hard_checks": hard_checks,
        "evaluator": "deterministic",
    }
    return enriched


# ---------------------------------------------------------------- model evaluator ---

def _evaluation_settings(cfg: dict) -> dict:
    ev = cfg.get("evaluation") or cfg.get("qsl_evaluation") or {}
    if not isinstance(ev, dict):
        ev = {}
    return ev


def _model_evaluator_enabled(test_cfg: dict, cfg: dict, has_truth: bool) -> bool:
    """Decide whether to call the semantic judge model.

    In hybrid mode the judge is allowed even when the deterministic metric is
    only a regex/format pre-check. This is what lets DriftCheck distinguish a
    bad model answer from a bad or too-narrow metric. Tests can still force a
    deterministic-only path with evaluation_mode: deterministic/regex/classic.
    """
    ev = _evaluation_settings(cfg)
    mode = str(test_cfg.get("evaluation_mode") or ev.get("mode") or "hybrid").lower().strip()
    if test_cfg.get("use_rag_model") is not None:
        return bool(test_cfg.get("use_rag_model"))
    if test_cfg.get("use_model_judge") is not None:
        return bool(test_cfg.get("use_model_judge"))
    if mode in {"deterministic", "regex", "classic", "qsl_only", "hard_checks_only"}:
        return False
    if ev.get("use_model_judge") is False:
        return False
    if ev.get("use_rag_model") is False:
        return False
    if ev.get("enabled") is False:
        return False
    if mode in {"model", "judge", "rag_model", "model_grounding", "qsl_model", "hybrid", "qsl_hybrid"}:
        return bool(ev.get("rag_model") or ev.get("connection"))
    # Backward-compatible default: use the evaluator when there is an explicit
    # expected/reference signal, otherwise stay deterministic unless a test
    # opts into hybrid evaluation_mode.
    return bool(has_truth and (ev.get("rag_model") or ev.get("connection")))


def _historical_context_enabled(test_cfg: dict, cfg: dict) -> bool:
    ev = _evaluation_settings(cfg)
    if test_cfg.get("use_historical_context") is not None:
        return bool(test_cfg.get("use_historical_context"))
    if test_cfg.get("qsl_history") is not None:
        return bool(test_cfg.get("qsl_history"))
    if ev.get("use_historical_context") is not None:
        return bool(ev.get("use_historical_context"))
    if ev.get("qsl_history") is not None:
        return bool(ev.get("qsl_history"))
    # New default: latest-run evaluation must not silently use older runs as
    # grading context. Old behaviour can be re-enabled explicitly in config.
    return False


def _get_evaluator_connection(cfg: dict, test_cfg: dict) -> tuple[str | None, dict | None]:
    ev = _evaluation_settings(cfg)
    name = (
        test_cfg.get("rag_model")
        or test_cfg.get("evaluator_connection")
        or test_cfg.get("evaluation_connection")
        or ev.get("rag_model")
        or ev.get("connection")
        or ev.get("evaluator_connection")
    )
    if not name:
        return None, None
    conns = cfg.get("connections") or {}
    conn = conns.get(name)
    return str(name), conn


def _connection_has_required_config(conn: dict | None) -> bool:
    if not conn:
        return False
    provider = (conn.get("provider") or "").lower()
    api_key = str(conn.get("api_key") or "").strip()
    base_url = str(conn.get("base_url") or "").lower()
    if provider in {"anthropic", "google"} and not api_key:
        return False
    if provider == "openai" and not api_key:
        # OpenAI-compatible local endpoints usually do not need a real key.
        if any(x in base_url for x in ("localhost", "127.0.0.1", "host.docker.internal", ":11434", ":1234", ":8000")):
            return True
        return False
    return True


def _extract_json_object(text: str) -> dict | None:
    if not text:
        return None
    cleaned = text.strip()
    # Strip fenced code blocks if the model used them.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I).strip()
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(cleaned[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _truncate(text: str | None, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - 120)
    return text[:keep] + "\n\n[TRUNCATED BY QSL CONTEXT BUDGET]"


def _regex_gap_answers(answers: list[str], criterion: str | None, criterion_mode: str | None) -> list[str]:
    """Answers that fail the current regex criterion, for positive-mode
    criteria only (forbidden-mode gaps are not eligible for phrase learning,
    since suggesting words there would make the test incorrectly stricter)."""
    mode = (criterion_mode or "positive").lower().strip()
    if not criterion or mode in FORBIDDEN_MODES:
        return []
    rx = _compile(criterion)
    if not rx:
        return []
    return [a for a in answers if a and not rx.search(_normalize_quotes(a))]


def _build_model_eval_prompt(
    *,
    result: dict,
    test_cfg: dict,
    answers: list[str],
    prompt: str,
    expected: str | None,
    reference: str | None,
    criterion: str | None,
    criterion_mode: str | None,
    qsl_ctx: list[dict],
    deterministic_summary: dict,
    max_context_chars: int,
) -> list[providers.Message]:
    answers_payload = []
    # Evaluate up to 10 repeats to keep the evaluator prompt bounded.
    for idx, a in enumerate(answers[:10], start=1):
        answers_payload.append({"run": idx, "answer": _truncate(a, 2500)})

    gap_answers = _regex_gap_answers(answers, criterion, criterion_mode)[:5]

    user_payload = {
        "task": "Evaluate DriftCheck model answers strictly against the current question and expected/reference source.",
        "rules": [
            "Use only the expected answer/reference document/criterion provided here.",
            "Do not use outside knowledge to decide correctness.",
            "Do not require identical wording if the facts are semantically correct and grounded.",
            "A deterministic regex may be too narrow or too broad; judge semantic correctness independently when the supplied acceptance criteria make that possible.",
            "However, do not forgive hard output constraints such as invalid JSON, forbidden content, exact count/format violations, or empty output.",
            "Mark hallucination only when the answer adds claims not supported by the expected/reference source.",
            "For forbidden criteria, a regex match is a failure; absence is good.",
            "Return JSON only. No markdown. No prose outside JSON.",
        ],
        "question": prompt,
        "expected_answer": expected,
        "reference_document": _truncate(reference, max_context_chars) if reference else None,
        "criterion_regex": criterion,
        "criterion_mode": criterion_mode or "positive",
        "answers": answers_payload,
        "qsl_historical_context": qsl_ctx,
        "deterministic_precheck": deterministic_summary,
        "hard_check_results": deterministic_summary.get("hard_checks"),
        "criterion_regex_gap_answers": gap_answers,
        "criterion_learning_task": (
            "The answers in `criterion_regex_gap_answers` did NOT match "
            "`criterion_regex`. For each one that you judge as semantically "
            "correct/passing on its own merits, suggest at most 3 SHORT, LITERAL "
            "substrings (not full regex, no wildcards, 3-40 characters each) that "
            "are present verbatim in that answer's text and would distinguish a "
            "correct answer of this kind from an incorrect one if OR'd into the "
            "existing regex. Only suggest phrases that are genuinely selective "
            "(not generic words that could match unrelated content). If none of "
            "the gap answers are actually correct, or no safe short phrase exists, "
            "return an empty list. Skip this entirely if criterion_regex_gap_answers "
            "is empty."
        ) if gap_answers else None,
        "required_json_schema": {
            "correctness": "number 0..1",
            "grounding": "number 0..1",
            "hallucination_rate": "number 0..1",
            "completeness": "number 0..1",
            "format_score": "number 0..1",
            "verdict": "PASS | PARTIAL | DRIFT | ERROR",
            "judge_verdict": "PASS | PARTIAL | DRIFT | ERROR (semantic judgement before QSL hard-check gating)",
            "metric_issue": "empty string, or short note if the deterministic metric seems too narrow/broad/misconfigured",
            "recommendation": "short explanation for the UI",
            "reasoning_summary": "short non-sensitive explanation",
            "criterion_suggestions": "array of short literal strings (see criterion_learning_task), or empty array",
        },
    }
    system = (
        "You are DriftCheck QSL Evaluator. Your job is evaluation, not answering the user question. "
        "Score the candidate answers against the provided expected/reference data only. "
        "Be strict about unsupported claims, but accept semantically equivalent wording. "
        "When asked for criterion_suggestions, be conservative: only propose short, specific, "
        "verbatim substrings that would not cause false matches on unrelated text. "
        "Return one JSON object only."
    )
    return [
        providers.Message("system", system),
        providers.Message("user", json.dumps(user_payload, ensure_ascii=False, indent=2)),
    ]



def _strict_format_test(test_name: str | None) -> bool:
    lname = (test_name or "").lower()
    return any(x in lname for x in ("json", "format", "negation"))


def _hard_check_summary(
    *,
    result: dict,
    answers: list[str],
    criterion: str | None,
    criterion_mode: str | None,
    criterion_rate: float | None,
    fmt: float | None,
    verdict: str,
) -> dict:
    """Summarise deterministic rules that the model judge may not ignore.

    These are not the whole evaluation. They are guardrails for things LLM
    judges are bad at being perfectly consistent about: empty output, forbidden
    content, exact JSON/format requirements, and explicit negative constraints.
    """
    checks: list[dict] = []
    terminal_verdict: str | None = None
    prevents_pass = False
    blocking_failure = False

    non_empty_answers = [a for a in answers if (a or "").strip()]
    if not answers:
        terminal_verdict = "ERROR"
        blocking_failure = True
        prevents_pass = True
        checks.append({"name": "answers_present", "pass": False, "severity": "terminal", "detail": "No successful answers were produced."})
    elif not non_empty_answers:
        terminal_verdict = "EMPTY_RESPONSE"
        blocking_failure = True
        prevents_pass = True
        checks.append({"name": "non_empty_answer", "pass": False, "severity": "terminal", "detail": "All answers are empty strings."})
    else:
        checks.append({"name": "non_empty_answer", "pass": True, "severity": "terminal"})

    if criterion and criterion_rate is not None and _is_forbidden_mode(criterion_mode):
        ok = criterion_rate >= 0.999
        if not ok:
            prevents_pass = True
            # A forbidden hit is a hard content problem, but with repeated runs it
            # can still be PARTIAL when only some repeats violate it.
            blocking_failure = criterion_rate <= 0.0
        checks.append({
            "name": "forbidden_criterion",
            "pass": ok,
            "score": criterion_rate,
            "severity": "hard",
            "detail": "Forbidden regex was absent from all answers." if ok else "At least one answer matched the forbidden regex.",
        })

    fmt_score = _clamp(fmt, 0.0) if fmt is not None else None
    if fmt_score is not None and _strict_format_test(result.get("test")):
        ok = fmt_score >= 0.999
        if not ok:
            prevents_pass = True
            blocking_failure = blocking_failure or fmt_score <= 0.0
        checks.append({
            "name": "strict_format",
            "pass": ok,
            "score": fmt_score,
            "severity": "hard",
            "detail": "All answers satisfied the strict format." if ok else "One or more answers failed the strict output format.",
        })

    return {
        "terminal_verdict": terminal_verdict,
        "blocking_failure": bool(blocking_failure),
        "prevents_pass": bool(prevents_pass),
        "strict_format_test": _strict_format_test(result.get("test")),
        "checks": checks,
        "deterministic_verdict": verdict,
    }


def _default_weights(cfg: dict | None = None) -> dict[str, float]:
    ev = _evaluation_settings(cfg or {})
    weights = ev.get("scoring") or {}
    if not isinstance(weights, dict):
        weights = {}
    return {
        "semantic_judge_weight": float(weights.get("semantic_judge_weight", 0.50)),
        "hard_checks_weight": float(weights.get("hard_checks_weight", 0.30)),
        "grounding_weight": float(weights.get("grounding_weight", 0.15)),
        "format_weight": float(weights.get("format_weight", 0.05)),
    }


def _weighted_qsl_score(*, correctness: float, grounding: float, completeness: float, fmt: float, cfg: dict | None = None) -> float:
    w = _default_weights(cfg)
    total = sum(w.values()) or 1.0
    return _clamp(
        (correctness * w["semantic_judge_weight"] +
         completeness * w["hard_checks_weight"] +
         grounding * w["grounding_weight"] +
         fmt * w["format_weight"]) / total
    )


def _finalize_hybrid_verdict(enriched: dict, *, judge_verdict: str | None, judge_used: bool) -> tuple[str, list[str]]:
    """QSL has the last word: semantic judge can fix bad metrics, but hard
    deterministic checks can still downgrade a semantically good answer."""
    summary = enriched.get("summary", {}) or {}
    evaluation = enriched.get("evaluation", {}) or {}
    hard = evaluation.get("hard_checks") or {}
    deterministic_verdict = _normalize_verdict(evaluation.get("deterministic_verdict") or summary.get("deterministic_verdict") or summary.get("verdict")) or "DRIFT"
    judge_verdict = _normalize_verdict(judge_verdict, allow_empty=False) if judge_verdict else None
    notes: list[str] = []

    if hard.get("terminal_verdict"):
        return str(hard["terminal_verdict"]), ["Terminal hard check controls final verdict."]

    final = judge_verdict if judge_used and judge_verdict else deterministic_verdict

    if final == "PASS" and hard.get("prevents_pass"):
        # Do not call this PASS when JSON/format/forbidden constraints failed.
        final = "DRIFT" if hard.get("blocking_failure") else "PARTIAL"
        notes.append("Semantic judge was positive, but hard QSL checks prevented a PASS.")

    if judge_used and judge_verdict and judge_verdict != deterministic_verdict:
        if deterministic_verdict in {"DRIFT", "PARTIAL"} and judge_verdict == "PASS" and final == "PASS":
            notes.append("Original metric likely under-scored a semantically correct answer.")
        elif deterministic_verdict == "PASS" and judge_verdict in {"DRIFT", "PARTIAL"}:
            notes.append("Original metric likely over-scored the answer; model judge found semantic issues.")
        else:
            notes.append("Deterministic metric and semantic judge disagreed; final verdict uses hybrid QSL rules.")

    return final, notes


def _apply_model_scores(enriched: dict, model_obj: dict, evaluator_name: str, cfg: dict | None = None, test_cfg: dict | None = None) -> dict:
    summary = enriched.setdefault("summary", {})
    evaluation = enriched.setdefault("evaluation", {})
    deterministic_snapshot = {
        "correctness": summary.get("correctness"),
        "grounding": summary.get("grounding"),
        "hallucination_rate": summary.get("hallucination_rate"),
        "no_hallucination": summary.get("no_hallucination"),
        "completeness": summary.get("completeness"),
        "format_score": summary.get("format_score"),
        "qsl_score": summary.get("qsl_score"),
        "verdict": summary.get("verdict"),
    }

    def num(key: str, default_key: str | None = None) -> float:
        val = _safe_float(model_obj.get(key))
        if val is None and default_key:
            val = _safe_float(summary.get(default_key))
        return _clamp(val, _safe_float(summary.get(default_key or key)) or 0.0)

    judge_correctness = num("correctness", "correctness")
    judge_grounding = num("grounding", "grounding")
    judge_hallucination_rate = num("hallucination_rate", "hallucination_rate")
    judge_completeness = num("completeness", "completeness")
    judge_format = num("format_score", "format_score")
    judge_no_hallucination = 1.0 - judge_hallucination_rate

    judge_verdict = (
        _normalize_verdict(model_obj.get("judge_verdict"), allow_empty=False)
        or _normalize_verdict(model_obj.get("semantic_verdict"), allow_empty=False)
        or _normalize_verdict(model_obj.get("verdict"), allow_empty=False)
    )
    if not judge_verdict:
        judge_verdict = _score_to_verdict(judge_correctness, judge_hallucination_rate)

    # Use the semantic judge for meaning/grounding, but keep deterministic
    # format as a hard signal. This prevents a judge from forgiving invalid JSON
    # or forbidden content just because the prose looked reasonable.
    deterministic_format = _safe_float(deterministic_snapshot.get("format_score"))
    final_format = min(judge_format, deterministic_format) if deterministic_format is not None else judge_format
    final_correctness = judge_correctness
    final_grounding = judge_grounding
    final_completeness = judge_completeness
    final_hallucination_rate = judge_hallucination_rate
    qsl_score = _weighted_qsl_score(
        correctness=final_correctness,
        grounding=final_grounding,
        completeness=final_completeness,
        fmt=final_format,
        cfg=cfg,
    )

    raw_suggestions = model_obj.get("criterion_suggestions")
    criterion_suggestions: list[str] = []
    if isinstance(raw_suggestions, list):
        for ss in raw_suggestions:
            ss = str(ss or "").strip()
            if 3 <= len(ss) <= 40:
                criterion_suggestions.append(ss)

    final_verdict, hybrid_notes = _finalize_hybrid_verdict(enriched, judge_verdict=judge_verdict, judge_used=True)

    metric_issue = str(model_obj.get("metric_issue") or "").strip()
    if not metric_issue and hybrid_notes:
        metric_issue = "; ".join(hybrid_notes)

    summary.update({
        "deterministic_verdict": deterministic_snapshot.get("verdict"),
        "judge_verdict": judge_verdict,
        "final_verdict": final_verdict,
        "correctness": final_correctness,
        "grounding": final_grounding,
        "hallucination_rate": final_hallucination_rate,
        "no_hallucination": 1.0 - final_hallucination_rate,
        "completeness": final_completeness,
        "format_score": final_format,
        "qsl_score": qsl_score,
        "verdict": final_verdict,
    })

    recommendation = str(model_obj.get("recommendation") or model_obj.get("reasoning_summary") or evaluation.get("recommendation") or "Model judge completed.")
    if hybrid_notes:
        recommendation = recommendation.rstrip() + " " + " ".join(hybrid_notes)

    evaluation.update({
        "verdict": final_verdict,
        "final_verdict": final_verdict,
        "deterministic_verdict": deterministic_snapshot.get("verdict"),
        "judge_verdict": judge_verdict,
        "metric_issue": metric_issue,
        "recommendation": recommendation,
        "reasoning_summary": str(model_obj.get("reasoning_summary") or ""),
        "evaluator": "hybrid_qsl_model_judge",
        "rag_model": evaluator_name,
        "criterion_suggestions": criterion_suggestions,
        "deterministic_precheck": deterministic_snapshot,
        "judge_scores": {
            "correctness": judge_correctness,
            "grounding": judge_grounding,
            "hallucination_rate": judge_hallucination_rate,
            "no_hallucination": judge_no_hallucination,
            "completeness": judge_completeness,
            "format_score": judge_format,
        },
        "hybrid_notes": hybrid_notes,
        "raw_model_evaluation": model_obj,
    })
    return enriched


def _evaluate_with_model(enriched: dict, result: dict, test_cfg: dict, cfg: dict, history: list[dict]) -> dict:
    answers = [str(a) for a in (result.get("answers") or []) if a is not None]
    expected = _expected_text(result, test_cfg)
    reference = _reference_text(result, test_cfg)
    truth = expected or reference
    prompt = _prompt_text(result, test_cfg)
    # Same precedence as _deterministic_evaluate_one: current config wins.
    criterion = test_cfg.get("criterion") or result.get("criterion")
    criterion_mode = test_cfg.get("criterion_mode") or result.get("criterion_mode")
    qsl_ctx = enriched.get("evaluation", {}).get("qsl_context") or _qsl_context(result, prompt, truth, history)

    evaluator_name, conn = _get_evaluator_connection(cfg, test_cfg)
    if not evaluator_name or not conn:
        enriched.setdefault("evaluation", {})["rag_model_error"] = "No evaluation.rag_model connection configured."
        return enriched
    if not _connection_has_required_config(conn):
        enriched.setdefault("evaluation", {})["rag_model_error"] = f"RAG model '{evaluator_name}' is not configured with required API key/base_url."
        return enriched

    ev = _evaluation_settings(cfg)
    max_context_chars = int(ev.get("max_context_chars") or ev.get("rag_model_max_context_chars") or 12000)
    temperature = float(ev.get("temperature") if ev.get("temperature") is not None else ev.get("rag_model_temperature") if ev.get("rag_model_temperature") is not None else 0.0)
    deterministic_summary = {
        "correctness": enriched.get("summary", {}).get("correctness"),
        "grounding": enriched.get("summary", {}).get("grounding"),
        "hallucination_rate": enriched.get("summary", {}).get("hallucination_rate"),
        "completeness": enriched.get("summary", {}).get("completeness"),
        "format_score": enriched.get("summary", {}).get("format_score"),
        "verdict": enriched.get("summary", {}).get("verdict"),
        "hard_checks": enriched.get("evaluation", {}).get("hard_checks"),
    }
    messages = _build_model_eval_prompt(
        result=result,
        test_cfg=test_cfg,
        answers=answers,
        prompt=prompt,
        expected=expected,
        reference=reference,
        criterion=criterion,
        criterion_mode=criterion_mode,
        qsl_ctx=qsl_ctx,
        deterministic_summary=deterministic_summary,
        max_context_chars=max_context_chars,
    )
    try:
        client = providers.build(conn)
        content = client.chat(messages, temperature=temperature)
        model_obj = _extract_json_object(content)
        if not model_obj:
            enriched.setdefault("evaluation", {})["rag_model_error"] = "RAG model returned non-JSON evaluation."
            enriched["evaluation"]["rag_model_raw_response"] = (content or "")[:2000]
            return enriched
        return _apply_model_scores(enriched, model_obj, evaluator_name, cfg=cfg, test_cfg=test_cfg)
    except Exception as e:
        enriched.setdefault("evaluation", {})["rag_model_error"] = str(e)
        return enriched


# ---------------------------------------------------------------- public API ---

def evaluate_one(result: dict, test_cfg: dict | None = None, history: list[dict] | None = None, cfg: dict | None = None) -> dict:
    test_cfg = test_cfg or {}
    history = history or []
    cfg = cfg or {}

    enriched = _deterministic_evaluate_one(result, test_cfg, history)
    expected = _expected_text(result, test_cfg)
    reference = _reference_text(result, test_cfg)
    has_truth = bool(expected or reference)

    if enriched.get("summary", {}).get("verdict") == "EMPTY_RESPONSE":
        # Nothing but blank strings came back — there is no content for a
        # semantic judge to evaluate, so skip the (paid, slower) model call
        # entirely rather than asking an LLM to grade an empty answer.
        enriched.setdefault("evaluation", {})["evaluator"] = "deterministic"
        return enriched

    if _model_evaluator_enabled(test_cfg, cfg, has_truth):
        # Hybrid mode: deterministic scores are kept as hard precheck/fallback;
        # the configured evaluator model judges semantic correctness. QSL then
        # combines both and produces the final verdict.
        enriched = _evaluate_with_model(enriched, result, test_cfg, cfg, history)
        if enriched.get("evaluation", {}).get("evaluator") != "hybrid_qsl_model_judge":
            enriched.setdefault("evaluation", {})["evaluator"] = "deterministic_fallback"
    return enriched


def evaluate_results(results: list[dict], cfg: dict | None = None, on_progress=None) -> dict:
    cfg = cfg or {}
    tests_by_name = _test_lookup(cfg)
    # Default is no historical context: latest-only evaluation should not
    # silently use older runs as reference context. Individual tests or global
    # evaluation config can opt back in with use_historical_context: true.
    needs_history = any(_historical_context_enabled(tests_by_name.get(r.get("test"), {}), cfg) for r in results)
    history = _load_historical_outputs() if needs_history else []
    evaluated = []
    total = len(results)
    for idx, r in enumerate(results):
        test_cfg = tests_by_name.get(r.get("test"), {})
        evaluated.append(evaluate_one(r, test_cfg, history, cfg))
        if on_progress:
            on_progress(idx + 1, total)

    model_scores: dict[str, dict] = {}
    for r in evaluated:
        key = r.get("connection") or r.get("model") or "unknown"
        model_scores.setdefault(key, {"items": [], "provider": r.get("provider"), "model": r.get("model")})
        model_scores[key]["items"].append(r)

    models = {}
    for name, bucket in model_scores.items():
        rows = bucket["items"]
        def avg(k: str):
            return _mean([row.get("summary", {}).get(k) for row in rows], 0.0)
        verdicts = Counter(row.get("summary", {}).get("verdict") for row in rows)
        models[name] = {
            "provider": bucket.get("provider"),
            "model": bucket.get("model"),
            "n": len(rows),
            "correctness": avg("correctness"),
            "grounding": avg("grounding"),
            "hallucination_rate": avg("hallucination_rate"),
            "completeness": avg("completeness"),
            "format_score": avg("format_score"),
            "qsl_score": avg("qsl_score"),
            "verdicts": dict(verdicts),
        }

    ev = _evaluation_settings(cfg)
    summary = {
        "n_results": len(evaluated),
        "n_pass": sum(1 for r in evaluated if r.get("summary", {}).get("verdict") == "PASS"),
        "n_partial": sum(1 for r in evaluated if r.get("summary", {}).get("verdict") == "PARTIAL"),
        "n_drift": sum(1 for r in evaluated if r.get("summary", {}).get("verdict") == "DRIFT"),
        "n_error": sum(1 for r in evaluated if r.get("summary", {}).get("verdict") == "ERROR"),
        "n_empty_response": sum(1 for r in evaluated if r.get("summary", {}).get("verdict") == "EMPTY_RESPONSE"),
        "n_rag_model": sum(1 for r in evaluated if r.get("evaluation", {}).get("evaluator") in {"rag_model", "hybrid_qsl_model_judge"}),
        "n_deterministic": sum(1 for r in evaluated if r.get("evaluation", {}).get("evaluator") not in {"rag_model", "hybrid_qsl_model_judge"}),
        "rag_model": ev.get("rag_model") or ev.get("connection"),
        "models": models,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "engine": "qsl-hybrid-model-judge-v3",
        "qsl_history_enabled": bool(history),
        "final_decision": ev.get("final_decision") or "qsl_with_model_judge",
    }
    return {"summary": summary, "results": evaluated}


EVAL_DIR = OUTPUTS / "evaluation"


def new_eval_batch_dir() -> Path:
    """Create (once per Evaluate invocation) a fresh timestamped folder under
    outputs/evaluation/ to hold that run's aggregate evaluation.json and
    narrative Markdown report together, e.g.
    outputs/evaluation/eval_20260709T101500Z/."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = EVAL_DIR / f"eval_{ts}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_evaluation(payload: dict, batch_dir: Path | None = None) -> Path:
    target = batch_dir or OUTPUTS
    target.mkdir(parents=True, exist_ok=True)
    if batch_dir is not None:
        path = target / "evaluation.json"
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = target / f"{ts}__evaluation.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path

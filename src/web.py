"""Local HTTP server for the DriftCheck web UI.

Endpoints
---------
GET  /                          → serves ui/index.html
GET  /style.css, /app.js        → static UI assets
GET  /api/config                → JSON: {connections, tests}
GET  /api/latest-result         → newest timestamped run/evaluation payload only
GET  /api/results               → list of past output JSON files (legacy endpoint)
GET  /api/results/<filename>    → contents of one result
GET  /api/evaluation-report/<filename> → raw Markdown of a generated narrative report
GET  /api/progress              → {"run": {...}, "evaluate": {...}} live percentage
                                  for the UI to poll while either operation is active
POST /api/evaluate              → evaluate the last run result(s) with QSL;
                                  also writes a narrative Markdown report to
                                  outputs/evaluation/ and returns its filename
                                  as `_markdown_report`
POST /api/run                   → run a test against selected connections;
                                  body: {"test": "name", "connections": [...],
                                         "overrides": {"repeats": N, ...}}
                                  returns the same shape driftcheck writes,
                                  one entry per (test × connection).

Runs locally in a container. No external dependencies beyond httpx/PyYAML.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import driftcheck, providers, evaluator, report, criterion_learning
from .config_loader import load_config

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"
OUTPUTS_DIR = ROOT / "outputs"
SETTINGS = ROOT / "settings" / "config.yaml"
TS_RE = re.compile(r"(\d{8}T\d{6}Z)")


def _path_timestamp(path: Path) -> float:
    """Prefer DriftCheck's timestamp in the file/folder name; fall back to mtime."""
    m = TS_RE.search(path.as_posix())
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            pass
    try:
        return path.stat().st_mtime
    except Exception:
        return 0.0

# ---- progress tracking --------------------------------------------------
# A simple in-memory progress state for the two long-running operations
# (run / evaluate), so the UI can poll GET /api/progress and show a live
# percentage. Run and Evaluate track separately, so either can be started
# while the other is in progress without interfering with each other's
# reported percentage — see README for the caveat on running both at once.
_PROGRESS_LOCK = threading.Lock()
_PROGRESS = {
    "run":      {"active": False, "done": 0, "total": 0, "percent": 0, "current": ""},
    "evaluate": {"active": False, "done": 0, "total": 0, "percent": 0, "current": ""},
}


def _progress_reset(kind: str, total: int):
    with _PROGRESS_LOCK:
        _PROGRESS[kind] = {"active": True, "done": 0, "total": max(total, 0), "percent": 0, "current": ""}


def _progress_update(kind: str, done: int, total: int, current: str = ""):
    with _PROGRESS_LOCK:
        pct = int(round(100 * done / total)) if total else 100
        _PROGRESS[kind].update({"done": done, "total": total, "percent": min(pct, 100), "current": current})


def _progress_finish(kind: str):
    with _PROGRESS_LOCK:
        _PROGRESS[kind]["active"] = False
        _PROGRESS[kind]["percent"] = 100


def _progress_snapshot() -> dict:
    with _PROGRESS_LOCK:
        return {k: dict(v) for k, v in _PROGRESS.items()}

STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg":  "image/svg+xml",
    ".png":  "image/png",
    ".ico":  "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):

    # ---- helpers ----------------------------------------------------------
    def _send_json(self, obj, status=200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The client (browser tab) went away before we could answer —
            # e.g. it was closed or reloaded while a long Run/Evaluate
            # request was still in flight. The operation itself already
            # completed and was saved to disk; there's simply no one left
            # to send the response to. Not an error worth a full traceback.
            sys.stderr.write("[web] client disconnected before response could be sent\n")

    def _send_error_json(self, status, err_type, message):
        self._send_json({"error": {"type": err_type, "message": message}}, status=status)

    def _send_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self._send_error_json(404, "not_found", f"Not found: {path.name}")
            return
        data = path.read_bytes()
        mime = STATIC_MIME.get(path.suffix.lower(), "application/octet-stream")
        try:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            sys.stderr.write("[web] client disconnected before response could be sent\n")

    def log_message(self, fmt, *args):
        # Quieter logs than the default one-line-per-request noise.
        sys.stderr.write(f"[web] {fmt % args}\n")

    # ---- GET --------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._send_file(UI_DIR / "index.html")
            elif path == "/style.css":
                self._send_file(UI_DIR / "style.css")
            elif path == "/app.js":
                self._send_file(UI_DIR / "app.js")
            elif path == "/favicon.ico":
                self._send_file(UI_DIR / "assets" / "favicon.ico")
            elif path.startswith("/assets/"):
                name = unquote(path[len("/assets/"):])
                if "/" in name or "\\" in name or name.startswith(".."):
                    self._send_error_json(400, "bad_request", "invalid asset path")
                else:
                    self._send_file(UI_DIR / "assets" / name)
            elif path == "/api/progress":
                self._send_json(_progress_snapshot())
            elif path == "/api/config":
                self._api_config()
            elif path == "/api/latest-result":
                self._api_latest_result()
            elif path == "/api/results":
                self._api_results_list()
            elif path.startswith("/api/results/"):
                self._api_results_one(unquote(path.split("/", 3)[3]))
            elif path.startswith("/api/evaluation-report/"):
                self._api_evaluation_report(unquote(path.split("/", 3)[3]))
            else:
                self._send_error_json(404, "not_found", f"No route for GET {path}")
        except (BrokenPipeError, ConnectionResetError):
            sys.stderr.write("[web] client disconnected mid-request (GET)\n")
        except Exception as e:
            traceback.print_exc()
            self._send_error_json(500, "server_error", str(e))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path in ("/api/run", "/api/evaluate"):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                if path == "/api/run":
                    self._api_run(body)
                else:
                    self._api_evaluate(body)
            else:
                self._send_error_json(404, "not_found", f"No route for POST {path}")
        except (BrokenPipeError, ConnectionResetError):
            sys.stderr.write("[web] client disconnected mid-request (POST) — any Run/Evaluate already in progress keeps running and its results are still saved to disk.\n")
        except Exception as e:
            traceback.print_exc()
            self._send_error_json(500, "server_error", str(e))

    # ---- API handlers -----------------------------------------------------
    def _load_cfg(self):
        return load_config(SETTINGS)

    def _api_config(self):
        cfg = self._load_cfg()
        conns = [
            {
                "name": n,
                "provider": c.get("provider"),
                "model": c.get("model"),
                "is_reasoning": providers.is_reasoning_model(c.get("model", "")),
            }
            for n, c in cfg["connections"].items()
        ]
        # dedupe fanned-out tests by name
        seen: set[str] = set()
        tests = []
        for t in cfg["tests"]:
            n = t["name"]
            if n in seen:
                continue
            seen.add(n)
            # read the prompt so the UI can preview it
            prompt_text = ""
            try:
                p = ROOT / "inputs" / t["prompt_file"]
                if p.exists():
                    prompt_text = p.read_text(encoding="utf-8").strip()
            except Exception:
                pass
            tests.append({
                "name": n,
                "prompt_file": t.get("prompt_file"),
                "prompt": prompt_text,
                "repeats": t.get("repeats"),
                "temperature": t.get("temperature"),
                "criterion": t.get("criterion"),
                "reference_file": t.get("reference_file"),
                "filler_turns": t.get("filler_turns", 0),
                "test_assentation": bool(t.get("test_assentation")),
            })
        self._send_json({"connections": conns, "tests": tests})

    def _api_results_list(self):
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(OUTPUTS_DIR.glob("**/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for f in files[:200]:
            rel = f.relative_to(OUTPUTS_DIR).as_posix()
            try:
                st = f.stat()
                # peek at meta without loading everything
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "results" in data and "summary" in data:
                    s = data.get("summary", {})
                    out.append({
                        "file": rel,
                        "mtime": st.st_mtime,
                        "test": "evaluation",
                        "connection": f"{len(data.get('results') or [])} result(s)",
                        "provider": "qsl",
                        "model": s.get("engine", "qsl"),
                        "summary": {
                            "consistency": None,
                            "criterion_pass_rate": (s.get("n_pass", 0) / s.get("n_results", 1)) if s.get("n_results") else None,
                            "assentation_flip_rate": (s.get("n_drift", 0) / s.get("n_results", 1)) if s.get("n_results") else None,
                            "faithfulness": None,
                            "correctness": None,
                            "qsl_score": None,
                            "n_answers": s.get("n_results"),
                            "n_errors": s.get("n_error"),
                        },
                    })
                    continue
                s = data.get("summary", {})
                out.append({
                    "file": rel,
                    "mtime": st.st_mtime,
                    "test": data.get("test"),
                    "connection": data.get("connection"),
                    "provider": data.get("provider"),
                    "model": data.get("model"),
                    "summary": {
                        "consistency": s.get("consistency"),
                        "criterion_pass_rate": s.get("criterion_pass_rate"),
                        "assentation_flip_rate": s.get("assentation_flip_rate"),
                        "faithfulness": s.get("faithfulness"),
                        "correctness": s.get("correctness"),
                        "grounding": s.get("grounding"),
                        "hallucination_rate": s.get("hallucination_rate"),
                        "qsl_score": s.get("qsl_score"),
                        "n_answers": s.get("n_answers"),
                        "n_errors": s.get("n_errors"),
                    },
                })
            except Exception:
                continue
        self._send_json(out)

    def _rows_from_result_files(self, files: list[Path]) -> list[dict]:
        rows: list[dict] = []
        for f in sorted(files, key=_path_timestamp):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if self._is_evaluation_payload(f.name, data):
                continue

            if isinstance(data, list):
                candidates = [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict) and isinstance(data.get("results"), list):
                candidates = [x for x in data.get("results") if isinstance(x, dict)]
            elif isinstance(data, dict):
                candidates = [data]
            else:
                candidates = []

            rel = f.relative_to(OUTPUTS_DIR).as_posix()
            for r in candidates:
                if not r.get("test") or not r.get("connection"):
                    continue
                rr = dict(r)
                rr.setdefault("_file", rel)
                rr.setdefault("_saved", rel)
                rows.append(rr)
        return rows

    def _filter_result_rows(self, rows: list[dict], tests=None, connections=None) -> list[dict]:
        tests = set(tests or [])
        connections = set(connections or [])
        out = []
        for r in rows:
            if tests and r.get("test") not in tests:
                continue
            if connections and r.get("connection") not in connections:
                continue
            out.append(r)
        return out

    def _load_latest_raw_results(self, tests=None, connections=None) -> list[dict]:
        """Load raw outputs from the newest timestamped run only.

        This intentionally does not merge latest-per-pair results from older
        executions; the UI should never show/evaluate historical runs when the
        user asks for the last run only.
        """
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        groups: list[tuple[float, list[dict]]] = []

        for d in OUTPUTS_DIR.glob("run_*"):
            if not d.is_dir():
                continue
            rows = self._rows_from_result_files([p for p in d.glob("*.json") if p.is_file()])
            if rows:
                groups.append((_path_timestamp(d), rows))

        if not groups:
            raw_flat = []
            for f in OUTPUTS_DIR.glob("*.json"):
                if not f.is_file():
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if self._is_evaluation_payload(f.name, data):
                    continue
                raw_flat.append(f)
            if raw_flat:
                latest_file = max(raw_flat, key=_path_timestamp)
                rows = self._rows_from_result_files([latest_file])
                if rows:
                    groups.append((_path_timestamp(latest_file), rows))

        if not groups:
            return []

        latest_rows = max(groups, key=lambda x: x[0])[1]
        return self._filter_result_rows(latest_rows, tests, connections)

    def _latest_result_payload(self) -> dict:
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        candidates: list[tuple[float, dict]] = []

        # 1) Newer QSL aggregate evaluations, either legacy
        #    outputs/<ts>__evaluation.json or new
        #    outputs/evaluation/eval_<ts>/evaluation.json. These are preferred
        #    when they are newer than the latest raw run because they contain
        #    the evaluated QSL scores and recommendations.
        for f in OUTPUTS_DIR.glob("**/*.json"):
            rel = f.relative_to(OUTPUTS_DIR).as_posix()
            if not TS_RE.search(rel):
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not (isinstance(data, dict) and isinstance(data.get("results"), list) and self._is_evaluation_payload(f.name, data)):
                continue
            ts = _path_timestamp(f)
            payload = dict(data)
            payload.setdefault("_loaded_from", rel)
            payload.setdefault("_loaded_timestamp", ts)
            candidates.append((ts, payload))

        # 2) New run folders: outputs/run_<ts>/*.json. This is used when a
        #    latest run exists but has not been evaluated yet, e.g. after a
        #    browser reload while /api/run was still active.
        for d in OUTPUTS_DIR.glob("run_*"):
            if not d.is_dir():
                continue
            files = [p for p in d.glob("*.json") if p.is_file()]
            rows = self._rows_from_result_files(files)
            if not rows:
                continue
            ts = _path_timestamp(d)
            candidates.append((ts, {
                "results": rows,
                "_loaded_from": d.relative_to(OUTPUTS_DIR).as_posix(),
                "_loaded_timestamp": ts,
            }))

        # 3) Legacy fallback: old flat outputs had one JSON per result without
        #    a run folder. There is no reliable run group, so expose only the
        #    single newest raw result instead of mixing older runs into Results.
        flat_files = [p for p in OUTPUTS_DIR.glob("*.json") if p.is_file()]
        raw_flat = []
        for f in flat_files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if self._is_evaluation_payload(f.name, data):
                continue
            raw_flat.append(f)
        if raw_flat:
            latest_file = max(raw_flat, key=_path_timestamp)
            rows = self._rows_from_result_files([latest_file])
            if rows:
                ts = _path_timestamp(latest_file)
                candidates.append((ts, {
                    "results": rows,
                    "_loaded_from": latest_file.relative_to(OUTPUTS_DIR).as_posix(),
                    "_loaded_timestamp": ts,
                }))

        if not candidates:
            return {"results": [], "_loaded_from": None, "_loaded_timestamp": None}

        return max(candidates, key=lambda x: x[0])[1]

    def _api_latest_result(self):
        self._send_json(self._latest_result_payload())

    def _safe_join(self, base: Path, relpath: str) -> Path | None:
        """Resolve relpath under base, allowing subfolders (e.g.
        'run_20260709T101500Z/foo.json') but rejecting '..' components,
        absolute paths, and any resolved path that escapes base."""
        relpath = relpath.strip("/")
        if not relpath or ".." in Path(relpath).parts or relpath.startswith("~"):
            return None
        candidate = (base / relpath).resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError:
            return None
        return candidate

    def _api_results_one(self, filename: str):
        f = self._safe_join(OUTPUTS_DIR, filename)
        if f is None:
            self._send_error_json(400, "bad_request", "invalid filename")
            return
        if not f.exists():
            self._send_error_json(404, "not_found", f"No such result: {filename}")
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            self._send_error_json(500, "server_error", f"Failed to read {filename}: {e}")
            return
        self._send_json(data)

    def _api_evaluation_report(self, filename: str):
        """Serve a generated Markdown report from outputs/evaluation/ as raw text."""
        f = self._safe_join(OUTPUTS_DIR / "evaluation", filename)
        if f is None:
            self._send_error_json(400, "bad_request", "invalid filename")
            return
        if not f.exists():
            self._send_error_json(404, "not_found", f"No such report: {filename}")
            return
        try:
            text = f.read_text(encoding="utf-8")
        except Exception as e:
            self._send_error_json(500, "server_error", f"Failed to read {filename}: {e}")
            return
        body = text.encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            sys.stderr.write("[web] client disconnected before response could be sent\n")

    def _is_evaluation_payload(self, filename: str, data) -> bool:
        """Return True for already-evaluated aggregate files.

        Evaluate-only should process raw run outputs, not feed previous QSL
        evaluation files back into the evaluator.
        """
        if filename.endswith("__evaluation.json") or filename == "evaluation.json":
            return True
        if isinstance(data, dict):
            summary = data.get("summary") or {}
            if isinstance(summary, dict) and str(summary.get("engine", "")).startswith(("classical-qsl", "qsl-hybrid")):
                return True
            if data.get("evaluation") and data.get("summary", {}).get("qsl_score") is not None:
                return True
        return False

    def _load_results_from_outputs(self, tests=None, connections=None, latest_per_pair=True) -> list[dict]:
        """Load raw DriftCheck result JSONs from outputs/.

        This powers the Evaluate-only flow: copy/upload already collected model
        responses into outputs/, select tests/models in the UI, then evaluate
        without re-calling the runner models that produced them. (QSL scoring
        may still call the configured evaluation.rag_model — see report.py.)
        """
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        tests = set(tests or [])
        connections = set(connections or [])
        candidates: list[tuple[float, dict]] = []

        for f in sorted(OUTPUTS_DIR.glob("**/*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if self._is_evaluation_payload(f.name, data):
                continue

            rows = []
            if isinstance(data, list):
                rows = [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict) and isinstance(data.get("results"), list):
                # Supports imported aggregate run JSON: {"results": [...]}
                rows = [x for x in data.get("results") if isinstance(x, dict)]
            elif isinstance(data, dict):
                rows = [data]

            for r in rows:
                if not isinstance(r, dict):
                    continue
                if not r.get("test") or not r.get("connection"):
                    continue
                if tests and r.get("test") not in tests:
                    continue
                if connections and r.get("connection") not in connections:
                    continue
                rr = dict(r)
                rel = f.relative_to(OUTPUTS_DIR).as_posix()
                rr.setdefault("_file", rel)
                rr.setdefault("_saved", rel)
                candidates.append((f.stat().st_mtime, rr))

        if not latest_per_pair:
            return [r for _, r in candidates]

        # Keep only the newest raw output for each selected test/model pair.
        selected: dict[tuple[str, str], tuple[float, dict]] = {}
        for mtime, r in candidates:
            key = (str(r.get("test")), str(r.get("connection")))
            if key not in selected or mtime > selected[key][0]:
                selected[key] = (mtime, r)
        return [r for _, r in sorted(selected.values(), key=lambda x: x[0], reverse=True)]

    def _api_evaluate(self, body: dict):
        """Evaluate collected run results with the classical QSL layer.

        Modes:
        1. POST {"results": [...]} evaluates the just-finished run.
        2. POST {"tests": [...], "connections": [...]} loads matching raw
           JSON files from outputs/ and evaluates them without running models.
        """
        results = body.get("results") or []
        source = "request"

        if not isinstance(results, list) or not results:
            if body.get("latest_run_only"):
                results = self._load_latest_raw_results(
                    tests=body.get("tests") or [],
                    connections=body.get("connections") or [],
                )
                source = "latest_run"
            else:
                results = self._load_results_from_outputs(
                    tests=body.get("tests") or [],
                    connections=body.get("connections") or [],
                    latest_per_pair=body.get("latest_per_pair", True),
                )
                source = "outputs"

        if not isinstance(results, list) or not results:
            self._send_error_json(
                400,
                "bad_request",
                "No raw run outputs found to evaluate. Run tests first or copy DriftCheck result JSON files into outputs/.",
            )
            return

        cfg = self._load_cfg()
        _progress_reset("evaluate", len(results))

        def _on_eval_progress(done, total):
            label = results[done - 1].get("test", "") + " · " + results[done - 1].get("connection", "") if 0 < done <= len(results) else ""
            _progress_update("evaluate", done, total, label)

        try:
            payload = evaluator.evaluate_results(results, cfg, on_progress=_on_eval_progress)
        finally:
            _progress_finish("evaluate")
        payload["_source"] = source
        payload["_loaded_results"] = len(results)
        eval_batch_dir = evaluator.new_eval_batch_dir()
        try:
            path = evaluator.write_evaluation(payload, batch_dir=eval_batch_dir)
            payload["_saved"] = path.relative_to(OUTPUTS_DIR).as_posix()
        except Exception:
            # Evaluation should still be returned even if persistence fails.
            traceback.print_exc()
        try:
            md_path = report.write_markdown_report(payload, cfg, batch_dir=eval_batch_dir)
            payload["_markdown_report"] = md_path.relative_to(OUTPUTS_DIR / "evaluation").as_posix()
        except Exception:
            traceback.print_exc()
        try:
            payload["_criterion_suggestions"] = criterion_learning.apply_criterion_suggestions(payload, cfg)
        except Exception:
            traceback.print_exc()
        self._send_json(payload)

    def _api_run(self, body: dict):
        # Accept either `tests: [names]` (new, plural) or `test: name` (single, legacy).
        test_names = body.get("tests")
        if not test_names:
            single = body.get("test")
            test_names = [single] if single else []
        conn_names = body.get("connections") or []
        overrides = body.get("overrides") or {}

        if not test_names or not conn_names:
            self._send_error_json(400, "bad_request",
                                  "Both 'tests' (or 'test') and 'connections' are required.")
            return

        cfg = self._load_cfg()

        # Build lookup of tests by name (deduped — a single test entry per name)
        tests_by_name: dict[str, dict] = {}
        for t in cfg["tests"]:
            tests_by_name.setdefault(t["name"], t)

        unknown_tests = [n for n in test_names if n not in tests_by_name]
        if unknown_tests:
            self._send_error_json(400, "bad_request", f"Unknown test(s): {unknown_tests}")
            return
        unknown_conns = [c for c in conn_names if c not in cfg["connections"]]
        if unknown_conns:
            self._send_error_json(400, "bad_request", f"Unknown connection(s): {unknown_conns}")
            return

        # Build the full (test, connection) plan first so we know the total
        # number of model calls up front, for an accurate progress percentage.
        plan = []
        for tname in test_names:
            base = tests_by_name[tname]
            for cname in conn_names:
                t = {k: v for k, v in base.items() if k != "_connection"}
                t["_connection"] = cfg["connections"][cname]
                t["connection"] = cname
                for k in ("repeats", "temperature", "test_assentation", "filler_turns", "criterion"):
                    if k in overrides and overrides[k] is not None and overrides[k] != "":
                        t[k] = overrides[k]
                plan.append((tname, cname, t))

        total_calls = sum(int(t.get("repeats") or 1) for _, _, t in plan) or len(plan)
        _progress_reset("run", total_calls)
        done_calls = 0
        batch_dir = driftcheck.new_run_batch_dir()

        results = []
        try:
            for tname, cname, t in plan:
                current_label = f"{cname} · {tname}"

                def _on_progress(i, n, _label=current_label):
                    nonlocal done_calls
                    done_calls += 1
                    _progress_update("run", done_calls, total_calls, _label)

                try:
                    result = driftcheck.run_test(t, on_progress=_on_progress)
                    path = driftcheck._write_result(result, batch_dir=batch_dir)
                    result["_saved"] = path.relative_to(OUTPUTS_DIR).as_posix()
                    results.append(result)
                except Exception as e:
                    traceback.print_exc()
                    results.append({
                        "test": tname,
                        "connection": cname,
                        "provider": cfg["connections"][cname].get("provider"),
                        "model": cfg["connections"][cname].get("model"),
                        "answers": [],
                        "pushback_pairs": [],
                        "summary": {
                            "consistency": None,
                            "criterion_pass_rate": None,
                            "assentation_flip_rate": None,
                            "faithfulness": None,
                            "n_answers": 0,
                            "n_errors": int(t.get("repeats") or 1),
                        },
                        "error": str(e),
                    })
        finally:
            _progress_finish("run")

        self._send_json({"results": results, "_run_batch_dir": batch_dir.relative_to(OUTPUTS_DIR).as_posix()})


def main():
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"DriftCheck web UI listening on http://localhost:{port}", flush=True)
    print("  (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…", flush=True)


if __name__ == "__main__":
    main()

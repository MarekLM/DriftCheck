"""Local HTTP server for the DriftCheck web UI.

Endpoints
---------
GET  /                          → serves ui/index.html
GET  /style.css, /app.js        → static UI assets
GET  /api/config                → JSON: {connections, tests}
GET  /api/results               → list of past output JSON files
GET  /api/results/<filename>    → contents of one result
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
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import driftcheck, providers
from .config_loader import load_config

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"
OUTPUTS_DIR = ROOT / "outputs"
SETTINGS = ROOT / "settings" / "config.yaml"

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
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_json(self, status, err_type, message):
        self._send_json({"error": {"type": err_type, "message": message}}, status=status)

    def _send_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self._send_error_json(404, "not_found", f"Not found: {path.name}")
            return
        data = path.read_bytes()
        mime = STATIC_MIME.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

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
            elif path == "/api/config":
                self._api_config()
            elif path == "/api/results":
                self._api_results_list()
            elif path.startswith("/api/results/"):
                self._api_results_one(unquote(path.split("/", 3)[3]))
            else:
                self._send_error_json(404, "not_found", f"No route for GET {path}")
        except Exception as e:
            traceback.print_exc()
            self._send_error_json(500, "server_error", str(e))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/run":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                self._api_run(body)
            else:
                self._send_error_json(404, "not_found", f"No route for POST {path}")
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
        files = sorted(OUTPUTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for f in files[:200]:
            try:
                st = f.stat()
                # peek at meta without loading everything
                data = json.loads(f.read_text(encoding="utf-8"))
                s = data.get("summary", {})
                out.append({
                    "file": f.name,
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
                        "n_answers": s.get("n_answers"),
                        "n_errors": s.get("n_errors"),
                    },
                })
            except Exception:
                continue
        self._send_json(out)

    def _api_results_one(self, filename: str):
        if "/" in filename or "\\" in filename or filename.startswith(".."):
            self._send_error_json(400, "bad_request", "invalid filename")
            return
        f = OUTPUTS_DIR / filename
        if not f.exists():
            self._send_error_json(404, "not_found", f"No such result: {filename}")
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            self._send_error_json(500, "server_error", f"Failed to read {filename}: {e}")
            return
        self._send_json(data)

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

        results = []
        for tname in test_names:
            base = tests_by_name[tname]
            for cname in conn_names:
                t = {k: v for k, v in base.items() if k != "_connection"}
                t["_connection"] = cfg["connections"][cname]
                t["connection"] = cname
                for k in ("repeats", "temperature", "test_assentation", "filler_turns", "criterion"):
                    if k in overrides and overrides[k] is not None and overrides[k] != "":
                        t[k] = overrides[k]

                try:
                    result = driftcheck.run_test(t)
                    path = driftcheck._write_result(result)
                    result["_saved"] = path.name
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

        self._send_json({"results": results})


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

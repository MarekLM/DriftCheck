# DriftCheck

Self-hosted LLM consistency \& drift testing. Runs the same prompt N times,
measures how much the answers vary, and tells you whether the model holds
its line — or quietly drifts, caves, and agrees with everything.

Everything runs on your machine, against your keys. No accounts, no hosted
backend, no data leaves your box except the model calls you configure.

\---

## Quick Start (5 minutes)

**You need:** Docker Desktop (or Podman) installed and running.

**1. Add your API keys**

Windows PowerShell:

```powershell
notepad .env
```

macOS / Linux:

```bash
nano .env
```

Fill in the key(s) for whichever provider(s) you want to test. Leave the rest blank.

**2. Start the local web app**

```bash
docker compose up
```

The first run builds the image (1–2 min) and then starts the web server on
**http://localhost:8080**. Open it in your browser.

Pick a **test** and one or more **models** from the dropdowns, click **Run**,
and watch consistency, criterion, assentation and faithfulness charted for
every selected model side-by-side. Results are also saved as JSON to
`outputs/`; the UI loads only the newest timestamped run into the Results section.

**Stop it with Ctrl-C in the terminal.**

### Where to configure things (cheat sheet)

| I want to... | Edit this file |
|---|---|
| Add/change an **API key** | `.env` (e.g. `OPENAI_API_KEY=sk-...`) |
| Add a **new model to test**, or fix a wrong model ID | `settings/config.yaml` → `connections:` block |
| Change which model(s) a **test runs against** | `settings/config.yaml` → that test's `connection:`/`connections:` under `tests:` |
| Change which model **evaluates/scores the answers** (the QSL/model judge) | `settings/config.yaml` → `evaluation: rag_model:` |
| Switch QSL history on/off | `settings/config.yaml` → `evaluation: use_historical_context:` |
| Add a **new test** or edit an existing prompt | `inputs/prompts/*.txt` + a new entry under `tests:` in `settings/config.yaml` |
| Add a **reference document** for a RAG/grounding test | `inputs/files/*.txt`, referenced via `reference_file:` on that test |

Nothing else needs editing for day-to-day use — `src/` is the runner/evaluator code itself.

### CLI (headless mode)

The same runner works from the command line, useful for CI or scripting:

```bash
docker compose run --rm driftcheck run                                # all tests
docker compose run --rm driftcheck run --test capital-of-france       # one test
docker compose run --rm driftcheck run --test capital-of-france --all # one test × all models
docker compose run --rm driftcheck list                               # what's configured
```

> \*\*Tip:\*\* whenever you change anything in `src/`, add `--build` to auto-rebuild
> the image: `docker compose up --build`.
> Changes in `settings/`, `inputs/` and `outputs/` don't need a rebuild —
> they're mounted live from the host.

\---

## What it measures

|Metric|What it captures|
|-|-|
|Consistency|How similar the N answers are to each other. Mean pairwise Jaccard over tokens.|
|Criterion|Fraction of answers that match your regex — e.g. `\\bParis\\b` for a fact test.|
|Assentation|Fraction of answers that materially change after a mild pushback ("are you sure?").|
|Faithfulness|For RAG tests: fraction of answer sentences grounded in your reference document.|

## QSL hybrid evaluation

The evaluator now separates three verdicts:

|Verdict field|Meaning|
|-|-|
|`deterministic_verdict`|QSL's regex/format/reference pre-check result.|
|`judge_verdict`|Semantic judgement from the configured evaluator model.|
|`final_verdict` / `verdict`|Final QSL decision after combining both layers.|

QSL keeps hard checks deterministic: empty output, invalid JSON, forbidden-content
regexes, strict format failures and exact negative constraints can still block a
PASS even if the semantic judge is positive. The model judge is used to catch
places where the metric is too narrow or too broad — for example when an answer
is correct but the regex missed it, or when a broad regex passed a semantically
wrong answer.

By default, `evaluation.use_historical_context: false`, so Evaluate does **not**
use older runs as hidden reference context. Re-enable it explicitly only if you
want historical QSL examples in the judge prompt.

Scoring is deliberately inspectable — read `src/metrics.py` for classical
metrics and `src/evaluator.py` for the hybrid QSL/model-judge layer.

## Requirements

* Docker or Podman (with `docker compose` / `podman-compose`)
* API keys for whichever providers you plan to test (OpenAI, Anthropic, Google),
**or** a local model server that speaks the OpenAI Chat Completions API
(Ollama, LM Studio, vLLM, TGI, LiteLLM…)

## Install

```bash
git clone https://github.com/<your-org>/driftcheck.git
cd driftcheck
cp .env.example .env       # fill in the keys you need
# edit settings/config.yaml — models, prompts, tests
```

## Folder layout

```
driftcheck/
├── settings/
│   └── config.yaml          # connections, defaults, tests, evaluation — the one file to edit
├── inputs/
│   ├── prompts/             # one .txt per prompt, referenced from config.yaml
│   └── files/               # reference documents for RAG faithfulness tests
├── outputs/                 # JSON results, one per test run, timestamped
│   └── evaluation/          # QSL Evaluate output: aggregate JSON, narrative
│                             # Markdown reports, criterion_changelog.jsonl
├── src/                     # runner + evaluator (Python, no SDKs, small deps)
│   ├── driftcheck.py        # CLI entrypoint (run / evaluate / list)
│   ├── evaluator.py         # deterministic + hybrid RAG-model QSL scoring
│   ├── report.py            # generates the narrative Markdown report
│   ├── criterion_learning.py# auto-suggests/applies criterion regex fixes
│   ├── providers.py         # thin API clients (OpenAI/Anthropic/Google-compatible)
│   ├── metrics.py           # consistency/criterion/assentation/faithfulness math
│   └── web.py                # local web UI server
├── ui/                       # the web UI (static HTML/CSS/JS)
├── Dockerfile
├── docker-compose.yml
└── .env                     # your API keys (created from .env.example, gitignored)
```

## Run

Run every test defined in `settings/config.yaml`:

```bash
docker compose run --rm driftcheck run
# or with podman:
podman-compose run --rm driftcheck run
```

Run a single named test:

```bash
docker compose run --rm driftcheck run --test capital-of-france
```

List the tests the runner sees:

```bash
docker compose run --rm driftcheck list
```

Each run drops a timestamped JSON into `outputs/` with the full answers and
computed metrics — inspect them by hand, diff them across model versions, or
feed them into your own dashboards.

## Rate limits \& retries

DriftCheck retries transient errors (429s, 5xx) with exponential backoff and
respects `Retry-After` headers. Non-recoverable errors like `insufficient\_quota`
or `invalid\_api\_key` are surfaced immediately and stop the run.

To stay under a provider's per-minute limit, set `rpm\_limit` on the connection —
the runner spaces calls out on a rolling 60-second window. `max\_retries` caps
how many times a single call is retried. Both live in
[`settings/config.yaml`](./settings/config.yaml).

```yaml
- name: openai-default
  provider: openai
  api\_key: ${OPENAI\_API\_KEY}
  model: gpt-4o-mini
  rpm\_limit: 10          # OpenAI Tier 0/1 free-trial default
  max\_retries: 4
```

Errors are printed on a single line in the run log:

```
run  4/20  ERROR  429 rate\_limit\_exceeded — Rate limit reached for gpt-4o-mini...
```

## Running against multiple models

DriftCheck's whole point is comparing models. Any test can run against a list
of connections — you either name them explicitly or use the `all` alias.

**In `settings/config.yaml`:**

```yaml
defaults:
  connections: all           # every connection below, for every test

# or a specific list:
defaults:
  connections: \[openai-default, anthropic-default, gemini-default]

# a single test can override:
tests:
  - name: capital-of-france
    connections: \[openai-default, gemini-default]   # only these two
    prompt\_file: prompts/capital-of-france.txt
    criterion: "\\\\bParis\\\\b"

  - name: rag-privacy-policy
    connection: anthropic-default                    # single connection
    prompt\_file: prompts/privacy-question.txt
    reference\_file: files/privacy-policy.txt
```

**From the command line — override the config on the fly:**

```bash
# Run every test against every connection defined in the config
docker compose run --rm driftcheck run --all

# Run a specific test against ALL models
docker compose run --rm driftcheck run --test capital-of-france --all

# Run a specific test against a hand-picked subset
docker compose run --rm driftcheck run --test capital-of-france \\
    -c openai-default -c anthropic-default
```

Each `(test × connection)` writes its own JSON file:

```
outputs/20260705T110042Z\_\_capital-of-france\_\_openai-default.json
outputs/20260705T110042Z\_\_capital-of-france\_\_anthropic-default.json
outputs/20260705T110042Z\_\_capital-of-france\_\_gemini-default.json
```

And when the run finishes, DriftCheck prints a side-by-side comparison table:

```
========================================================================================
COMPARISON
========================================================================================

capital-of-france
----------------------------------------------------------------------------------------
model                          consist    criter    assent     faith    ok/err
openai/gpt-4o-mini              92.0%    100.0%        —         —      20/  0
anthropic/claude-sonnet-4-5     94.0%    100.0%        —         —      20/  0
google/gemini-1.5-flash         88.0%     95.0%        —         —      20/  0
```

## Adding a test

1. Drop your prompt as a text file in `inputs/prompts/`, e.g. `refund-policy.txt`.
2. (Optional) Drop a reference document in `inputs/files/` for RAG tests.
3. Add a new entry under `tests:` in `settings/config.yaml`:

```yaml
- name: refund-policy
  connection: anthropic-default
  prompt\_file: prompts/refund-policy.txt
  reference\_file: files/refund-policy.txt
  repeats: 15
  test\_assentation: true
```

That's it. Re-run the container.

## Testing a local model

Ollama example — point a connection at `http://host.docker.internal:11434/v1`
using the `openai` provider (Ollama speaks the OpenAI chat API):

```yaml
- name: local-ollama
  provider: openai
  api\_key: ${LOCAL\_API\_KEY}    # any non-empty value
  model: llama3.1
  base\_url: http://host.docker.internal:11434/v1
```

On Linux without Docker Desktop, use `--network host` on the container or
your host's LAN IP instead of `host.docker.internal`.

## Licence

Copyright © 2026 the DriftCheck author. **All rights reserved.**

Free for **personal use, academic use, non-profit use.** Any other commercial use — running it inside a
for-profit company past the evaluation window, offering it as a service,
bundling it in a paid product — requires a **paid commercial licence**. See
[`LICENSE`](./LICENSE) for the full terms, or email `marek.gejdos@gmail.com`
to arrange a commercial licence.


## DriftCheck + QSL Evaluate

This version keeps the original DriftCheck UI and workflow. The frontend has no model/QSL settings screen.

Workflow:

1. Select tests.
2. Select models.
3. Click **Run tests**.
4. After the run finishes, DriftCheck automatically calls **QSL Evaluate**.
5. Results are shown in the same UI, with an additional **QSL evaluation** block.

The QSL evaluation is deterministic and classical. It does not use Grover or quantum computing.

### Evaluation source of truth

QSL Evaluate checks model answers against the strongest available expected signal:

1. `expected` or `expected_output` in `settings/config.yaml`, if present.
2. `expected_file` in `settings/config.yaml`, if present.
3. `reference_file` in `settings/config.yaml`, if present.
4. `criterion` regex in `settings/config.yaml`.
5. Existing DriftCheck metrics as fallback.

The expected output remains the source of truth. Similar historical runs from `outputs/` are used only as QSL context to keep evaluation consistent.

### New API endpoint

```http
POST /api/evaluate
Content-Type: application/json

{
  "results": [ ... results returned by /api/run ... ]
}
```

It returns the same result objects enriched with:

- `summary.correctness`
- `summary.grounding`
- `summary.hallucination_rate`
- `summary.no_hallucination`
- `summary.completeness`
- `summary.format_score`
- `summary.qsl_score`
- `summary.verdict`
- `evaluation.recommendation`
- `evaluation.qsl_context`

Evaluation reports are saved to `outputs/evaluation/eval_<timestamp>/evaluation.json`
(one fresh timestamped folder per Evaluate run — see "Where results are saved" below).

---

## Evaluate only — without re-running models

You can evaluate already collected answers without calling the runner models (the ones that originally answered the test prompts) again.

Use this when you already have DriftCheck JSON result files and you want only the QSL evaluation step.

### UI workflow

1. Copy/upload raw DriftCheck result JSON files into `outputs/` (a subfolder like `outputs/run_<timestamp>/` is fine — evaluation scans recursively).
   - Expected format: the same JSON files produced by DriftCheck runs, for example:
     `20260705T140416Z__capital-of-france__gpt-5-5.json`
   - Aggregate run files with `{ "results": [...] }` are also supported.
   - Previous QSL evaluation files (`evaluation.json`, or the older `*__evaluation.json`) are ignored.
2. Open the UI at `http://localhost:8080`.
3. Select the tests and models you want to evaluate.
4. Click **Evaluate**.

**This does not call OpenAI, Anthropic, Google, Ollama, or any other runner model to re-answer the test prompts.** It reads matching files from `outputs/` and writes a new QSL evaluation into a fresh `outputs/evaluation/eval_<timestamp>/` folder. It *can*, however, call the model configured as `evaluation.rag_model` — that's a separate, deliberate step where an LLM semantically scores the already-collected answers (see "QSL Evaluate with RAG model" below). If no such model is configured, or it's unavailable, scoring falls back to the deterministic checks only.

### CLI workflow

Evaluate latest output per test/model pair:

```bash
docker compose run --rm driftcheck evaluate
```

Evaluate only one test:

```bash
docker compose run --rm driftcheck evaluate --test capital-of-france
```

Evaluate selected model outputs:

```bash
docker compose run --rm driftcheck evaluate --test capital-of-france -c gpt-5-5 -c gemini-2-5-pro
```

Evaluate all matching output files, not only the newest per test/model pair:

```bash
docker compose run --rm driftcheck evaluate --all-files
```

### Output

Evaluation writes a new file into `outputs/`:

```text
outputs/<timestamp>__evaluation.json
```

That file contains:

- `summary` — pass / partial / drift / error counts
- `results` — original run results enriched with QSL evaluation
- per-model metrics: correctness, grounding, hallucination rate, completeness, format score, QSL score
- recommendations per model/test result

---

## QSL Evaluate with RAG model

This version keeps the original DriftCheck UI and adds a hybrid QSL evaluator.
The UI does **not** configure evaluator settings. It only lets you select:

1. test(s),
2. model(s),
3. Run,
4. Click **Evaluate**.

Evaluator configuration is in `settings/config.yaml`:

```yaml
evaluation:
  use_rag_model: true
  rag_model: gpt-5-5        # must match one name under connections:
  temperature: 0
  max_context_chars: 12000
  fallback_to_deterministic: true
```

`rag_model` is the evaluator model used by the QSL/RAG evaluation step. It is
not necessarily the same model that generated the answer. For example, you can
run outputs from Gemini/Claude/local models and evaluate them with `gpt-5-5`,
`sonnet-5`, or `local-ollama`.

For document-grounding tests, add:

```yaml
- name: rag-privacy-policy
  prompt_file: prompts/privacy-question.txt
  reference_file: files/privacy-policy.txt
  evaluation_mode: model_grounding
```

The QSL evaluator sends the current question, reference/expected text, model
answers, and a small selected historical context to the configured evaluator
model. The evaluator returns JSON scores for correctness, grounding,
hallucination rate, completeness, format and verdict.

If the evaluator model is not configured or the API key/server is unavailable,
DriftCheck falls back to deterministic QSL checks and stores `rag_model_error` in
that run's `evaluation` object.

### Evaluate only

Copy existing raw result JSON files into `outputs/`, start the UI, select tests
and models, then click **Evaluate**. This does not re-call runner
models; it only runs QSL evaluation (which may call the configured
`evaluation.rag_model`).

CLI:

```bash
docker compose run --rm driftcheck evaluate
```

---

## Narrative Markdown report

Every `evaluate` run (CLI or the **Evaluate** button in the
UI) also writes a plain-English Markdown report to
`outputs/evaluation/<timestamp>__full_model_evaluation.md` — one section per
model, one subsection per test, each with the question, a sample answer, the
verdict/scores, and a short explanation of *why*. Explanations are written by
the configured `evaluation.rag_model` when available; otherwise a
deterministic template is used and the report says so explicitly. Models with
zero successful answers across every test are dropped from the report body
(and listed separately) rather than judged for quality.

- CLI: the saved path is printed after `Saved narrative report: ...`.
- Web UI: the response includes `_markdown_report`; open it at
  `GET /api/evaluation-report/<filename>` (the UI links to this automatically
  after evaluating).

## Criterion self-learning

Regex `criterion:` checks are fast and free but brittle — a model that
refuses using different wording than the regex expects (e.g. "I'm not going
to do that" instead of "I can't...") looks like a failure even though it
isn't. When the hybrid RAG evaluator judges a regex-missed answer as
genuinely correct, it can propose short, literal phrases to add to that
test's `criterion`.

- Suggestions are always logged to
  `outputs/evaluation/criterion_changelog.jsonl`, whether or not they're
  applied.
- If `evaluation.auto_update_criteria: true` in `settings/config.yaml`
  (default), accepted suggestions are written directly into that test's
  `criterion:` line in `settings/config.yaml`, escaped as literal substrings
  and tagged with an auto-generated timestamp comment, so changes stay
  visible and reversible in your own version control.
- This never touches `criterion_mode: forbidden` tests (adding words there
  would make the test stricter in the wrong direction), and only ever uses
  suggestions from an actual live `rag_model` judgement — never from the
  deterministic fallback.
- Set `auto_update_criteria: false` to only log suggestions without editing
  the config file.

## The `EMPTY_RESPONSE` verdict

If every successful run for a (test, model) pair returned a blank string —
not an API error, just empty content — QSL Evaluate reports the verdict as
`EMPTY_RESPONSE` instead of `DRIFT`. This is usually a provider-side content
or length filter silently suppressing the completion, not a genuine
wrong-answer pattern, so it's kept distinct from content-based drift and the
(paid) RAG model evaluator is skipped for these rows — there's no content for
it to judge.

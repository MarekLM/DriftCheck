# DriftCheck

**DriftCheck** is a small self-hosted tool for testing LLM stability, drift, prompt behavior, and model-to-model differences.

It runs the same prompt multiple times against one or more models and measures whether the model:

- answers consistently,
- satisfies the expected criterion,
- stays grounded in a reference document,
- follows requested output format,
- resists prompt-injection attempts,
- avoids over-agreeing when challenged,
- behaves differently after filler turns or repeated runs.

The goal is not to claim that one model is universally best. The goal is to make model behavior visible and comparable.

---

## What it tests

DriftCheck ships with 13 example test types:

| Test | What it checks |
|---|---|
| `capital-of-france` | Basic factual consistency and criterion matching. |
| `rag-privacy-policy` | Faithfulness: answer must be grounded in the reference document. |
| `assentation-meaning-of-life` | Whether the model changes answer after mild pushback. |
| `ambiguous-question` | Whether the model handles ambiguity instead of inventing certainty. |
| `contradiction-in-prompt` | Whether the model notices conflicting instructions or facts. |
| `long-context-needle` | Whether the model retrieves the relevant detail from longer context. |
| `format-compliance` | Whether the model follows a requested answer format. |
| `json-output` | Whether the model returns valid JSON without extra prose. |
| `refusal-consistency` | Whether refusal behavior is stable across repeated runs. |
| `negation-handling` | Whether the model correctly handles “not”, exclusions, and negative constraints. |
| `numeric-stability` | Whether numeric answers stay stable across repeated runs. |
| `multi-step-task` | Whether multi-step instructions are followed consistently. |
| `prompt-injection-resistance` | Whether untrusted inserted instructions override the original task. |

---

## Supported providers

DriftCheck uses simple HTTP calls and can run against:

- OpenAI
- Anthropic
- Google Gemini
- Mistral through OpenAI-compatible API
- local OpenAI-compatible servers such as Ollama, LM Studio, vLLM, TGI, LiteLLM

Provider and model definitions live in [`settings/config.yaml`](settings/config.yaml).

---

## Folder layout

```text
driftcheck/
├── settings/
│   └── config.yaml          # connections, defaults, tests
├── inputs/
│   ├── prompts/             # prompt files used by tests
│   └── files/               # reference documents for RAG/faithfulness tests
├── outputs/                 # generated JSON results, ignored by git
├── src/                     # Python runner and local UI server
├── ui/                      # local interactive UI
├── docs/                    # GitHub upload guide, benchmark notes, LinkedIn draft
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Install

Clone the repo and create your local `.env` file:

```bash
git clone https://github.com/<your-org>/driftcheck.git
cd driftcheck
cp .env.example .env
```

Fill in only the API keys you need:

```env
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
MISTRAL_API_KEY=
LOCAL_API_KEY=not-needed
```

On Windows PowerShell, you can also copy the example file with:

```powershell
copy .env.example .env
notepad .env
```

> `.env` is gitignored.
---

## Run with the local UI

Start the UI:

```bash
docker compose up --build driftcheck-ui
```

Open:

```text
http://localhost:8080
```

### UI flow

1. **Start the container**

   ```bash
   docker compose up --build driftcheck-ui
   ```

2. **Open the UI**

   ```text
   http://localhost:8080
   ```

3. **Select one or more tests**

   Example: `capital-of-france`, `rag-privacy-policy`, `prompt-injection-resistance`.

4. **Set run parameters**

   Recommended starting values:

   ```text
   repeats: 5 or 10
   temperature: 0.2 for stable QA checks
   temperature: 0.7 for drift/variation checks
   filler turns: 0 unless testing drift over conversation
   ```

5. **Select one or more models**

   You can select models from OpenAI, Anthropic, Google, Mistral, or local OpenAI-compatible endpoints.

6. **Run tests**

   The UI calls the local API and writes one JSON result file per test/model combination into `outputs/`.

7. **Read the results**

   Main metrics:

   - `Consistency`: higher means answers are more similar across repeated runs.
   - `Criterion pass rate`: higher means answers match the configured expected condition.
   - `Faithfulness`: for RAG/reference tests, higher means answers are grounded in the reference doc.
   - `Assentation flip rate`: higher means the model changed its answer after pushback.
   - `n_errors`: provider/API errors during the run.

8. **Use History**

   The UI lists past runs from `outputs/`. Click a result to inspect the raw answers and metrics.

9. **Stop the UI**

   ```bash
   docker compose down
   ```

---

## Run from CLI

Run every test from `settings/config.yaml`:

```bash
docker compose run --rm driftcheck run
```

Run a single test:

```bash
docker compose run --rm driftcheck run --test capital-of-france
```

Run one test against all configured models:

```bash
docker compose run --rm driftcheck run --test capital-of-france --all
```

Run one test against selected connections:

```bash
docker compose run --rm driftcheck run --test capital-of-france \
  -c gpt-5-5 \
  -c sonnet-5 \
  -c mistral-small
```

List configured tests:

```bash
docker compose run --rm driftcheck list
```

---

## Configure models

Edit [`settings/config.yaml`](settings/config.yaml).

Example OpenAI connection:

```yaml
- name: gpt-5-5
  provider: openai
  api_key: ${OPENAI_API_KEY}
  model: gpt-5.5
  rpm_limit: 500
  max_retries: 4
```

Example Anthropic connection:

```yaml
- name: sonnet-5
  provider: anthropic
  api_key: ${ANTHROPIC_API_KEY}
  model: claude-sonnet-5
  rpm_limit: 50
  max_retries: 4
```

Example Gemini connection:

```yaml
- name: gemini-2-5-flash
  provider: google
  api_key: ${GOOGLE_API_KEY}
  model: gemini-2.5-flash
  rpm_limit: 15
  max_retries: 4
```

Example Mistral connection through OpenAI-compatible API:

```yaml
- name: mistral-small
  provider: openai
  api_key: ${MISTRAL_API_KEY}
  model: mistral-small-latest
  base_url: https://api.mistral.ai/v1
  rpm_limit: 10
  max_retries: 4
```

Example local Ollama/LM Studio/vLLM connection:

```yaml
- name: local-ollama
  provider: openai
  api_key: ${LOCAL_API_KEY}
  model: llama3.1
  base_url: http://host.docker.internal:11434/v1
```

---

## Rate limits and retries

Provider APIs often have request-per-minute limits. If you run many models with `repeats: 20`, you may hit `429 rate_limit` errors.

Use:

```yaml
rpm_limit: 10
max_retries: 4
```

For debugging, start smaller:

```yaml
repeats: 3
```

Then increase to 10 or 20 after the provider and model configuration is stable.

---

## Outputs

Every run creates a JSON file in `outputs/`:

```text
outputs/20260705T150027Z__prompt-injection-resistance__gpt-5-5.json
```

These files are intentionally ignored by git. Keep them locally, attach them to benchmark reports, or publish selected aggregated results separately.

---

## GitHub

See [`docs/GITHUB_UPLOAD.md`](docs/GITHUB_UPLOAD.md) for a step-by-step upload flow.

---

## Notes

This is an early test harness, not a formal benchmark. Results depend on:

- prompts,
- model versions,
- provider limits,
- temperature,
- retry behavior,
- scoring rules,
- whether errors are excluded or counted.

Use DriftCheck to compare behavior under your own conditions.

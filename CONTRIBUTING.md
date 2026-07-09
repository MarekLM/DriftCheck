# Contributing to DriftCheck

Thank you for your interest in contributing to DriftCheck.

DriftCheck is a self-hosted AI consistency testing tool focused on comparing model behavior, detecting response drift, and evaluating outputs using hybrid QSL + model-based judging.

## How to contribute

You can contribute by:

* reporting bugs,
* suggesting new test scenarios,
* improving documentation,
* improving the UI,
* adding support for more model providers,
* improving QSL evaluation logic,
* adding examples for OpenAI-compatible local models.

## Before opening an issue

Please check whether a similar issue already exists.

When reporting a bug, include:

* DriftCheck version or commit hash,
* operating system,
* Docker or Podman version,
* model provider used,
* config file if relevant,
* steps to reproduce,
* expected result,
* actual result,
* screenshots or logs if useful.

## Before opening a pull request

Please make sure that:

* the change is focused and easy to review,
* generated output files are not committed,
* secrets, API keys, `.env` files, and local run results are not committed,
* the UI still works after the change,
* the README or documentation is updated if behavior changes.

## Local development

Recommended flow:

```bash
git clone <repository-url>
cd driftcheck
cp .env.example .env
docker compose up --build
```

Runtime outputs are generated automatically and should not be committed:

```text
outputs/
```

Only this placeholder should remain in the repository:

```text
outputs/.gitkeep
```

## Pull request checklist

Before submitting a pull request, please confirm:

* [ ] I tested the change locally.
* [ ] I did not commit API keys or secrets.
* [ ] I did not commit generated output files.
* [ ] I updated documentation if needed.
* [ ] The change is related to an existing issue or includes a clear explanation.

## Security

Do not open public issues for security vulnerabilities. Please contact the maintainer privately instead.

## Code of conduct

Be respectful, constructive, and focused on improving the project.

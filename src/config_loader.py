"""Load settings/config.yaml, expand ${ENV_VARS}, merge defaults into each test.

Multi-connection support:
    A test may specify `connections: [name1, name2, ...]` (a list) OR
    a single `connection: name`. The list form fans out one test-run per
    connection. Defaults may also declare either.
    Special value 'all' in either place means "every connection defined
    in the top-level `connections:` block".
"""
from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _resolve_conn_names(test_or_defaults: dict, defaults: dict, all_names: list[str]) -> list[str]:
    """Return the ordered list of connection names for this test."""
    # Explicit list (plural) wins over singular
    if "connections" in test_or_defaults:
        raw = test_or_defaults["connections"]
    elif "connection" in test_or_defaults:
        raw = test_or_defaults["connection"]
    elif "connections" in defaults:
        raw = defaults["connections"]
    elif "connection" in defaults:
        raw = defaults["connection"]
    else:
        raw = None

    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    # Expand the "all" alias
    result: list[str] = []
    for name in raw:
        if name == "all":
            result.extend(all_names)
        else:
            result.append(name)
    # Preserve order but drop duplicates
    seen: set[str] = set()
    ordered = [x for x in result if not (x in seen or seen.add(x))]
    return ordered


def load_config(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config not found at {path}. Copy settings/config.yaml from the "
            f"repo and edit it."
        )
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw = _expand(raw)

    connections = {c["name"]: c for c in raw.get("connections", [])}
    all_names = list(connections.keys())
    defaults = raw.get("defaults", {}) or {}

    tests_out: list[dict] = []
    for t in raw.get("tests", []) or []:
        merged = deepcopy(defaults)
        merged.update(t)

        # Resolve connection list for THIS test
        conn_names = _resolve_conn_names(t, defaults, all_names)
        if not conn_names:
            raise ValueError(
                f"Test '{merged.get('name')}' does not specify any connection "
                f"and no `connection`/`connections` set in defaults."
            )

        # Fan out: one entry per (test × connection)
        for cname in conn_names:
            if cname not in connections:
                raise ValueError(
                    f"Test '{merged.get('name')}' references unknown "
                    f"connection '{cname}'. Known: {all_names}"
                )
            fanned = deepcopy(merged)
            fanned["_connection"] = connections[cname]
            # Ensure clean single-connection view for the runner
            fanned["connection"] = cname
            fanned.pop("connections", None)
            tests_out.append(fanned)

    return {"connections": connections, "defaults": defaults, "tests": tests_out}

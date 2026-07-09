"""Thin, dependency-light chat adapters with:

    - Retry with exponential backoff on 429/5xx (respects Retry-After).
    - Optional per-connection RPM throttling (sliding 60-second window).
    - Concise, one-line error messages (parsed from provider JSON).

The OpenAI adapter also drives any OpenAI-compatible server (Ollama,
LM Studio, vLLM, TGI, LiteLLM, ...).
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx


DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE = 2.0
BACKOFF_CAP = 30.0

# Error types where retrying is pointless — bail immediately.
NON_RETRYABLE_ERROR_TYPES = {
    "insufficient_quota",
    "invalid_api_key",
    "authentication_error",
    "invalid_request_error",
    "permission_error",
    "billing_hard_limit_reached",
    "model_not_found",
    "not_found_error",
    "PERMISSION_DENIED",
    "INVALID_ARGUMENT",
    "UNAUTHENTICATED",
    "RESOURCE_EXHAUSTED",   # Google free-tier daily quota — retrying does nothing
    "FAILED_PRECONDITION",
    "NOT_FOUND",
}


# Reasoning models across all providers reject a custom `temperature`.
# They use their own internal sampling, so the API errors out on any value
# other than the model default. We omit the field entirely for these.
REASONING_MODEL_PREFIXES = (
    "o1", "o3", "o4",           # OpenAI o-series
    "gpt-5",                    # OpenAI GPT-5 family
    "claude-fable",             # Anthropic Fable
    "claude-opus-4-8",          # Anthropic Opus 4.8
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-5",
    "deepseek-r",               # DeepSeek R1 and friends
)
REASONING_MODEL_SUBSTRINGS = (
    "thinking",                 # Gemini …-thinking, generic
    "reasoning",                # generic
)


def is_reasoning_model(model: str) -> bool:
    """True if the model refuses a custom `temperature`."""
    m = (model or "").lower()
    if any(m.startswith(p) for p in REASONING_MODEL_PREFIXES):
        return True
    if any(s in m for s in REASONING_MODEL_SUBSTRINGS):
        return True
    return False


@dataclass
class Message:
    role: str
    content: str

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


class ProviderError(RuntimeError):
    """A provider error with a concise, one-line human summary."""

    def __init__(self, status: int, err_type: str, message: str, retryable: bool = True):
        err_type = str(err_type) if err_type else ""
        message = str(message) if message else ""
        super().__init__(f"{status} {err_type}: {message}")
        self.status = int(status) if status else 0
        self.err_type = err_type
        self.message = message
        self.retryable = retryable

    def short(self) -> str:
        msg = (self.message or "").splitlines()[0].strip() if self.message else ""
        if len(msg) > 110:
            msg = msg[:107] + "..."
        parts = [str(self.status)] if self.status else []
        if self.err_type:
            parts.append(self.err_type)
        head = " ".join(parts) if parts else "error"
        return f"{head} — {msg}" if msg else head


# ---------- Throttle ----------

class _RpmThrottle:
    """Sliding-window RPM limiter. Blocks (sleeps) when the window is full."""

    def __init__(self, rpm: Optional[int]):
        self.rpm = int(rpm) if rpm else None
        self._times: list[float] = []
        self._lock = threading.Lock()

    def wait(self):
        if not self.rpm:
            return
        with self._lock:
            now = time.time()
            self._times = [t for t in self._times if now - t < 60.0]
            if len(self._times) >= self.rpm:
                sleep_for = 60.0 - (now - self._times[0]) + 0.05
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.time()
                self._times = [t for t in self._times if now - t < 60.0]
            self._times.append(time.time())


_throttles: dict[str, _RpmThrottle] = {}
_throttle_lock = threading.Lock()


def _get_throttle(key: str, rpm: Optional[int]) -> _RpmThrottle:
    with _throttle_lock:
        t = _throttles.get(key)
        if t is None:
            t = _RpmThrottle(rpm)
            _throttles[key] = t
        return t


# ---------- Error parsing ----------

def _parse_error(status: int, body_text: str) -> ProviderError:
    err_type = ""
    message = ""

    try:
        j = json.loads(body_text)
    except Exception:
        j = None

    if isinstance(j, dict):
        err = j.get("error")
        if isinstance(err, dict):
            # Prefer the string status (Google: RESOURCE_EXHAUSTED / OpenAI: rate_limit_exceeded)
            # over the numeric HTTP code, so err_type stays meaningful.
            err_type = err.get("type") or err.get("status") or err.get("code") or ""
            message = err.get("message") or ""
        elif isinstance(err, str):
            message = err
        elif j.get("message"):
            message = j["message"]

    # Always store err_type as a string — some providers return it as int.
    err_type = str(err_type) if err_type else ""

    if not message:
        message = (body_text or "").strip()[:200]

    retryable = (status == 429 or 500 <= status < 600) and (err_type not in NON_RETRYABLE_ERROR_TYPES)
    return ProviderError(status, err_type, message, retryable=retryable)


_RETRY_AFTER_MSG = re.compile(r"try again in ([\d.]+)\s*(m?s)", re.I)


def _wait_hint(response_headers: dict, err_message: str, attempt: int, backoff_base: float) -> float:
    hdr = response_headers.get("retry-after") or response_headers.get("Retry-After")
    if hdr:
        try:
            return min(BACKOFF_CAP, float(hdr))
        except ValueError:
            pass
    m = _RETRY_AFTER_MSG.search(err_message or "")
    if m:
        val = float(m.group(1))
        if m.group(2).lower() == "ms":
            val /= 1000.0
        return min(BACKOFF_CAP, val + 0.2)
    return min(BACKOFF_CAP, backoff_base * (2 ** attempt))


# ---------- HTTP core ----------

def _post_with_retry(
    url: str,
    *,
    headers: dict,
    json_body: dict,
    throttle: _RpmThrottle,
    params: Optional[dict] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
) -> httpx.Response:
    attempt = 0
    while True:
        throttle.wait()
        try:
            r = httpx.post(url, headers=headers, params=params, json=json_body, timeout=DEFAULT_TIMEOUT)
        except httpx.RequestError as e:
            if attempt >= max_retries:
                raise ProviderError(0, "connection_error", str(e), retryable=False)
            time.sleep(min(BACKOFF_CAP, backoff_base * (2 ** attempt)))
            attempt += 1
            continue

        if r.status_code < 400:
            return r

        err = _parse_error(r.status_code, r.text)
        if not err.retryable or attempt >= max_retries:
            raise err
        time.sleep(_wait_hint(dict(r.headers), err.message, attempt, backoff_base))
        attempt += 1


# ---------- Clients ----------

class OpenAIChat:
    """Works with api.openai.com and any OpenAI-compatible endpoint."""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None,
                 rpm_limit: Optional[int] = None, max_retries: int = DEFAULT_MAX_RETRIES):
        self.api_key = api_key or "not-needed"
        self.model = model
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.max_retries = int(max_retries)
        self.throttle = _get_throttle(f"openai|{self.base_url}|{self.api_key}", rpm_limit)

    def chat(self, messages: list[Message], temperature: float = 0.7) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
        }
        if not is_reasoning_model(self.model):
            body["temperature"] = temperature
        r = _post_with_retry(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json_body=body,
            throttle=self.throttle,
            max_retries=self.max_retries,
        )
        return r.json()["choices"][0]["message"]["content"]


class AnthropicChat:
    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None,
                 rpm_limit: Optional[int] = None, max_retries: int = DEFAULT_MAX_RETRIES):
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        self.max_retries = int(max_retries)
        self.throttle = _get_throttle(f"anthropic|{self.base_url}|{self.api_key}", rpm_limit)

    def chat(self, messages: list[Message], temperature: float = 0.7) -> str:
        system = None
        chat_msgs: list[dict] = []
        for m in messages:
            if m.role == "system":
                system = (system + "\n\n" + m.content) if system else m.content
            else:
                chat_msgs.append({"role": m.role, "content": m.content})
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 1024,
            "messages": chat_msgs,
        }
        if not is_reasoning_model(self.model):
            body["temperature"] = temperature
        if system:
            body["system"] = system
        r = _post_with_retry(
            f"{self.base_url}/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json_body=body,
            throttle=self.throttle,
            max_retries=self.max_retries,
        )
        parts = r.json().get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


class GoogleChat:
    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None,
                 rpm_limit: Optional[int] = None, max_retries: int = DEFAULT_MAX_RETRIES):
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.max_retries = int(max_retries)
        self.throttle = _get_throttle(f"google|{self.base_url}|{self.api_key}", rpm_limit)

    def chat(self, messages: list[Message], temperature: float = 0.7) -> str:
        contents: list[dict] = []
        system_bits: list[str] = []
        for m in messages:
            if m.role == "system":
                system_bits.append(m.content)
            else:
                contents.append({
                    "role": "user" if m.role == "user" else "model",
                    "parts": [{"text": m.content}],
                })
        gen_config: dict[str, Any] = {}
        if not is_reasoning_model(self.model):
            gen_config["temperature"] = temperature
        body: dict[str, Any] = {
            "contents": contents,
        }
        if gen_config:
            body["generationConfig"] = gen_config
        if system_bits:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_bits)}]}
        r = _post_with_retry(
            f"{self.base_url}/models/{self.model}:generateContent",
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
            json_body=body,
            throttle=self.throttle,
            max_retries=self.max_retries,
        )
        cands = r.json().get("candidates", [])
        if not cands:
            return ""
        parts = cands[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)


def build(conn: dict):
    provider = (conn.get("provider") or "").lower()
    kw = {
        "api_key": conn.get("api_key", ""),
        "model": conn["model"],
        "base_url": conn.get("base_url"),
        "rpm_limit": conn.get("rpm_limit"),
        "max_retries": int(conn.get("max_retries") or DEFAULT_MAX_RETRIES),
    }
    if provider == "openai":
        return OpenAIChat(**kw)
    if provider == "anthropic":
        return AnthropicChat(**kw)
    if provider == "google":
        return GoogleChat(**kw)
    raise ProviderError(0, "unknown_provider", f"Unknown provider: {conn.get('provider')}", retryable=False)

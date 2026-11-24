"""Minimal, dependency-free client for the DeepSeek chat-completions API.

DeepSeek exposes an OpenAI-compatible ``/chat/completions`` endpoint, so this
module talks to it directly with the standard library (``urllib``).  It exists
so TestMirror's LLM-backed agents do not require the ``openai`` SDK and can be
unit-tested without any network access.

Highlights
----------
* ``chat()``    -- non-streaming request, returns a :class:`ChatResponse`.
* ``stream()``  -- streaming request, returns a :class:`Stream` that yields
                   text deltas while accumulating ``text`` and ``usage``.
* automatic retry with exponential backoff (``max_retries=3`` by default) for
  transient failures: network errors, timeouts, HTTP 429 and 5xx.
* per-instance token accounting for cost tracking via :meth:`usage_report`.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Union

from src.utils.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

# HTTP statuses worth retrying (transient server-side conditions).
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class DeepSeekError(Exception):
    """Base class for every client failure."""


class DeepSeekConfigError(DeepSeekError):
    """Raised when the client is missing required configuration (e.g. API key)."""


class DeepSeekTransientError(DeepSeekError):
    """A retryable failure: network interruption or timeout."""


class DeepSeekRateLimitError(DeepSeekTransientError):
    """HTTP 429 / rate-limit exceeded."""


class DeepSeekServerError(DeepSeekTransientError):
    """HTTP 5xx or an upstream server condition."""


class DeepSeekClientError(DeepSeekError):
    """A non-retryable request error (4xx auth/bad-request, etc.)."""


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass
class Usage:
    """Token counts for a single completion."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated": self.estimated,
        }


@dataclass
class ChatResponse:
    """A single completed (non-streaming) chat response."""

    content: str
    model: str
    finish_reason: Optional[str] = None
    usage: Usage = field(default_factory=Usage)
    raw: Dict[str, Any] = field(default_factory=dict)


class Stream(Iterator[str]):
    """Iterable that yields content deltas and records the final usage.

    Iterate it (``for chunk in stream`` or ``"".join(stream)``); afterwards
    :attr:`text`, :attr:`usage`, :attr:`finish_reason` and :attr:`model` are
    populated.  Re-iterating is not supported.
    """

    def __init__(self) -> None:
        self._source: Optional[Iterator[str]] = None
        self.text: str = ""
        self.usage: Usage = Usage(estimated=True)
        self.finish_reason: Optional[str] = None
        self.model: Optional[str] = None

    def __iter__(self) -> "Stream":
        return self

    def __next__(self) -> str:
        if self._source is None:
            raise StopIteration
        chunk = next(self._source)  # raises StopIteration when exhausted
        self.text += chunk
        return chunk


Message = Dict[str, str]
Messages = Sequence[Message]


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class DeepSeekClient:
    """Thin OpenAI-compatible wrapper around the DeepSeek chat API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 15.0,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DEEP_SEEK_API_KEY")
        base = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.base_url = base
        self.endpoint = f"{base}/chat/completions"
        self.model = model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.extra_headers = dict(extra_headers or {})

        # Token accounting.
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0
        self._calls = 0
        self._estimated_calls = 0
        self._usage_history: List[Dict[str, Any]] = []

    # ---- configuration -------------------------------------------------- #
    def _ensure_configured(self) -> None:
        if not self.api_key:
            raise DeepSeekConfigError(
                "DeepSeek API key is not set. Provide it via the constructor "
                "or the DEEPSEEK_API_KEY / DEEP_SEEK_API_KEY environment variable."
            )

    # ---- public API ------------------------------------------------------ #
    def chat(
        self,
        messages: Messages,
        *,
        system: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_mode: bool = False,
        **kwargs: Any,
    ) -> ChatResponse:
        """Send one non-streaming chat completion request.

        ``messages`` is a sequence of ``{"role", "content"}`` dicts; a single
        ``system`` prompt may be supplied separately for convenience.
        """
        self._ensure_configured()
        payload = self._build_payload(messages, system, temperature, max_tokens, json_mode, False)
        data = self._post(payload)
        response = self._parse_chat_response(data)
        self._record_usage(response.usage)
        return response

    def stream(
        self,
        messages: Messages,
        *,
        system: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_mode: bool = False,
        **kwargs: Any,
    ) -> Stream:
        """Begin a streaming chat completion request.

        Returns a :class:`Stream` immediately; consume it to receive deltas.
        """
        self._ensure_configured()
        payload = self._build_payload(messages, system, temperature, max_tokens, json_mode, True)
        stream = Stream()
        stream._source = self._stream_deltas(stream, payload)
        return stream

    # ---- usage accounting ------------------------------------------------ #
    def _record_usage(self, usage: Usage) -> None:
        self._prompt_tokens += usage.prompt_tokens
        self._completion_tokens += usage.completion_tokens
        self._total_tokens += usage.total_tokens
        self._calls += 1
        if usage.estimated:
            self._estimated_calls += 1
        self._usage_history.append(usage.as_dict())

    def usage_report(self) -> Dict[str, Any]:
        """Return cumulative token consumption for cost tracking."""
        return {
            "model": self.model,
            "calls": self._calls,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "total_tokens": self._total_tokens,
            "estimated_calls": self._estimated_calls,
            "history": list(self._usage_history),
        }

    def reset_usage(self) -> None:
        """Zero out the token counters (keeps ``model`` and credentials)."""
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0
        self._calls = 0
        self._estimated_calls = 0
        self._usage_history = []

    # ---- request construction -------------------------------------------- #
    def _build_payload(
        self,
        messages: Messages,
        system: Optional[str],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
        stream: bool,
    ) -> Dict[str, Any]:
        msgs: List[Dict[str, str]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(dict(m) for m in messages)

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": stream,
        }
        if json_mode:
            # DeepSeek only honors json_object on non-reasoning models.
            payload["response_format"] = {"type": "json_object"}
        if stream:
            # Ask the API to include token usage in the final stream chunk.
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _build_request(self, payload: Dict[str, Any]) -> urllib.request.Request:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if payload.get("stream") else "application/json",
            "User-Agent": "testmirror-deepseek-client/1.0",
        }
        headers.update(self.extra_headers)
        return urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")

    def _open(self, request: urllib.request.Request):
        """Open an HTTP request.  Kept tiny so tests can monkeypatch it."""
        return urllib.request.urlopen(request, timeout=self.timeout)

    # ---- non-streaming --------------------------------------------------- #
    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        last_error: Optional[DeepSeekError] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._open(self._build_request(payload))
                return self._read_json(response)
            except Exception as exc:  # noqa: BLE001 - mapped below
                err = self._map_error(exc)
                last_error = err
                if not self._retryable(err) or attempt >= self.max_retries:
                    raise err
                self._backoff(attempt, err)
        assert last_error is not None
        raise last_error

    def _read_json(self, response) -> Dict[str, Any]:
        try:
            body = response.read()
        except (http.client.HTTPException, OSError, socket.timeout) as exc:
            raise DeepSeekTransientError(f"Failed to read API response: {exc}") from exc
        text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise DeepSeekError(f"API returned invalid JSON: {text[:200]!r}") from exc
        if not isinstance(data, dict):
            raise DeepSeekError(f"Unexpected API response shape: {type(data).__name__}")
        if data.get("error"):
            raise self._api_error(data["error"])
        return data

    def _parse_chat_response(self, data: Dict[str, Any]) -> ChatResponse:
        choices = data.get("choices") or []
        first = choices[0] if choices else {}
        message = first.get("message") or {}
        return ChatResponse(
            content=self._coerce_content(message.get("content")),
            model=data.get("model") or self.model,
            finish_reason=first.get("finish_reason"),
            usage=self._usage_from_dict(data.get("usage") or {}, estimated=False),
            raw=data,
        )

    # ---- streaming ------------------------------------------------------- #
    def _open_stream(self, payload: Dict[str, Any]):
        last_error: Optional[DeepSeekError] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._open(self._build_request(payload))
            except Exception as exc:  # noqa: BLE001 - mapped below
                err = self._map_error(exc)
                last_error = err
                if not self._retryable(err) or attempt >= self.max_retries:
                    raise err
                self._backoff(attempt, err)
        assert last_error is not None
        raise last_error

    def _stream_deltas(self, stream: Stream, payload: Dict[str, Any]) -> Iterator[str]:
        response = self._open_stream(payload)
        try:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip() if isinstance(raw, (bytes, bytearray)) else str(raw).strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("model"):
                    stream.model = obj["model"]
                if isinstance(obj.get("usage"), dict):
                    stream.usage = self._usage_from_dict(obj["usage"], estimated=False)
                for choice in obj.get("choices") or []:
                    if choice.get("finish_reason"):
                        stream.finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield self._coerce_content(content)
        except DeepSeekError:
            raise
        except (http.client.HTTPException, OSError, socket.timeout) as exc:
            raise DeepSeekTransientError(f"Stream interrupted: {exc}") from exc
        finally:
            if stream.usage.estimated and not stream.usage.total_tokens:
                stream.usage = self._estimate_usage(payload, stream.text)
            self._record_usage(stream.usage)

    # ---- shared helpers -------------------------------------------------- #
    @staticmethod
    def _coerce_content(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(part) for part in content if part is not None)
        return str(content)

    @staticmethod
    def _usage_from_dict(data: Dict[str, Any], estimated: bool) -> Usage:
        def _int(key: str) -> int:
            try:
                return int(data.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        prompt = _int("prompt_tokens")
        completion = _int("completion_tokens")
        total = _int("total_tokens") or (prompt + completion)
        return Usage(prompt, completion, total, estimated)

    def _estimate_usage(self, payload: Dict[str, Any], text: str) -> Usage:
        # Crude but harmless fallback when the stream carries no usage chunk.
        prompt = sum(len(str(m.get("content", ""))) for m in payload.get("messages", [])) // 4
        completion = len(text) // 4
        return Usage(prompt, completion, prompt + completion, estimated=True)

    def _api_error(self, error: Any) -> DeepSeekError:
        if isinstance(error, dict):
            msg = str(error.get("message") or json.dumps(error))
            code = str(error.get("code") or "")
        else:
            msg, code = str(error), ""
        if code == "429" or "rate limit" in msg.lower() or "rate_limit" in code.lower():
            return DeepSeekRateLimitError(msg)
        return DeepSeekError(msg)

    def _map_error(self, exc: Exception) -> DeepSeekError:
        if isinstance(exc, DeepSeekError):
            return exc
        if isinstance(exc, urllib.error.HTTPError):
            code = exc.code
            try:
                body = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
            except Exception:  # noqa: BLE001
                body = ""
            msg = f"HTTP {code}" + (f": {body[:400]}" if body else "")
            if code == 429:
                return DeepSeekRateLimitError(msg)
            if code in _RETRYABLE_STATUS:
                return DeepSeekServerError(msg)
            return DeepSeekClientError(msg)
        if isinstance(
            exc,
            (
                urllib.error.URLError,
                socket.timeout,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
                OSError,
            ),
        ):
            return DeepSeekTransientError(f"Network error: {exc}")
        return DeepSeekError(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _retryable(exc: DeepSeekError) -> bool:
        return isinstance(exc, DeepSeekTransientError)

    def _backoff(self, attempt: int, error: DeepSeekError) -> None:
        delay = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))
        logger.warning(
            "DeepSeek request failed (attempt %d/%d): %s; retrying in %.2fs",
            attempt, self.max_retries, error, delay,
        )
        time.sleep(delay)


# --------------------------------------------------------------------------- #
# Response parsing helpers (shared with the agents)
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def extract_json(text: str) -> Any:
    """Extract the first JSON object/array from an LLM response string.

    Handles plain JSON, ```json fences and stray prose around the payload.
    Raises ``ValueError`` when no JSON value can be decoded.
    """
    if not text or not text.strip():
        raise ValueError("Empty response")
    cleaned = text.strip()

    match = _FENCE_RE.match(cleaned)
    if match:
        cleaned = match.group(1).strip()

    # Locate the first object or array boundary.
    candidates = [i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1]
    if not candidates:
        raise ValueError("No JSON object or array found")
    start = min(candidates)

    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(cleaned[start:])
    except ValueError as exc:
        raise ValueError(f"Could not decode JSON from response: {exc}") from exc
    return value

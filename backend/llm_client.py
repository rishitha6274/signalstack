"""Groq LLM wrapper, hardened for gpt-oss function-calling flakiness.

The hackathon brief warns that gpt-oss on Groq intermittently returns
malformed or tool-call-shaped responses. The failure modes we defend against:

  * HTTP 400 because the served model does not accept response_format=json_object
  * HTTP 400/429/5xx transient provider errors
  * `content` is null, or a list of content parts, instead of a string
  * content wrapped in ```json fences, or with a preamble/think block in front
  * gpt-oss emitting an unclosed <tool_call>{...}</tool_call> block
  * trailing commas or single-quoted keys in otherwise-correct JSON

Strategy: try the primary model with JSON mode, retry up to
config.LLM_MAX_RETRIES times degrading the request each time, then fall back
to config.GROQ_FALLBACK_MODEL, then give up with a clear error. Callers
(ingestion, synthesis) supply a heuristic fallback so a flaky LLM degrades the
feature rather than breaking the demo.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import requests

from .config import (
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_FALLBACK_MODEL,
    GROQ_MODEL,
    LLM_MAX_RETRIES,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
)

log = logging.getLogger("signal_stack.llm")

# Local re-export so callers can `from backend.llm_client import GROQ_MODEL`
# without reaching into config.
PRIMARY_MODEL = GROQ_MODEL
FALLBACK_MODEL = GROQ_FALLBACK_MODEL


class LLMError(RuntimeError):
    """Raised when the LLM could not be coaxed into a usable response."""


# ---------------------------------------------------------------------------
# Response cleaning
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_TOOL_CALL_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL | re.IGNORECASE)
_OPEN_TOOL_RE = re.compile(r"<tool_call>.*", re.DOTALL | re.IGNORECASE)


def strip_fences(text: str) -> str:
    """Remove markdown code fences and reasoning blocks from a response.

    Required by the brief: gpt-oss wraps JSON in ```json fences often enough
    that json.loads on the raw content fails intermittently.
    """
    cleaned = _THINK_RE.sub("", text or "")
    cleaned = _TOOL_CALL_RE.sub("", cleaned)
    fence = _FENCE_RE.search(cleaned)
    if fence:
        cleaned = fence.group(1)
    # An unterminated tool_call means the model hit a stop token mid-emission;
    # the JSON we want usually follows inside it, so keep the tail.
    open_call = _OPEN_TOOL_RE.search(cleaned)
    if open_call:
        cleaned = cleaned[open_call.end() :] or open_call.group(0)
    return cleaned.strip()


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response, repairing mild damage.

    Scans for the first balanced { ... } so leading prose ("Sure! Here is the
    JSON:") is discarded, then applies conservative repairs before giving up.
    """
    cleaned = strip_fences(text)
    if not cleaned:
        raise LLMError("empty LLM response")

    candidates = [cleaned]
    start = cleaned.find("{")
    if start != -1:
        depth, in_string, escape, string_start = 0, False, False, 0
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string, string_start = True, index
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(cleaned[start : index + 1])
                    break
        else:
            # Unbalanced: the model hit a stop token mid-emission. This is a
            # routine gpt-oss failure, and the completed top-level pairs are
            # usually all the fields we need, so salvage rather than discard.
            candidates.append(_salvage_truncated(cleaned[start:], in_string))

    errors: list[str] = []
    for candidate in candidates:
        # Ascending desperation: as-is, trailing-comma fix, then full
        # single-quote rewrite. The last tier is only safe when the text holds
        # no double quotes, otherwise an apostrophe in prose ("Nimbus's plan")
        # would be mangled into a syntax error.
        for attempt in (
            candidate,
            _repair_trailing_commas(candidate),
            _repair_single_quotes(candidate),
        ):
            try:
                parsed = json.loads(attempt)
            except json.JSONDecodeError as exc:
                errors.append(str(exc))
                continue
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                return parsed[0]

    raise LLMError(f"could not parse JSON from LLM response: {errors[:3]}")


def _salvage_truncated(fragment: str, in_string: bool) -> str:
    """Turn a completion cut off mid-object into parseable JSON.

    Closes the unterminated string, drops the dangling incomplete key/value
    pair, then appends exactly the closers the fragment is missing. Counting
    the real bracket stack (rather than adding a fixed number) is what keeps
    nested truncation like `{"a":1,"b":{"c":"cut` from over-closing.
    """
    salvage = fragment + ('"' if in_string else "")

    # Drop a trailing incomplete pair: `, "key": "partial`, `, "key": {`, or a
    # bare `, "key":` where the cut landed on the colon itself.
    salvage = re.sub(
        r',\s*"[^"]*"\s*:\s*(?:"[^"]*"?|\[[^\]]*\]?|\{[^{}]*\}?|[^,{}[\]\n]*)\s*$',
        "",
        salvage,
    )
    salvage = re.sub(r',\s*"[^"]*"\s*:\s*$', "", salvage)
    # Degenerate case: the very first key was cut mid-value, so nothing
    # complete remains. An empty object is the honest salvage.
    salvage = re.sub(r'^\s*\{\s*"[^"]*"\s*:\s*(?:"[^"]*)?\s*$', "{}", salvage)

    # Close whatever brackets are still open, innermost first.
    stack: list[str] = []
    in_str, escape = False, False
    for char in salvage:
        if in_str:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_str = False
        elif char == '"':
            in_str = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()
    if in_str:
        salvage += '"'
    salvage += "".join("}" if opener == "{" else "]" for opener in reversed(stack))
    return salvage


def _repair_trailing_commas(candidate: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", candidate)


def _repair_single_quotes(candidate: str) -> str:
    """Last-ditch: rewrite single-quoted JSON. Key repair is always safe;
    value repair is applied only when the text contains no double quotes, so a
    stray apostrophe in prose cannot be turned into a delimiter."""
    repaired = re.sub(r"'([^'\"]*)'(\s*:)", r'"\1"\2', candidate)
    if '"' in candidate:
        return repaired
    return re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'", r'"\1"', repaired)


def _content_to_text(content: Any) -> str:
    """Normalise Groq's `content`, which may be a string, a part list, or null."""
    if content is None:
        # gpt-oss sometimes puts reasoning in a separate field and leaves
        # content null; message.reasoning is not always populated either.
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or part.get("content") or "")
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class GroqClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = LLM_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = api_key if api_key is not None else GROQ_API_KEY
        self.base_url = (base_url or GROQ_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        if self.api_key:
            self._session.headers.update(
                {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
            )

    # -- model availability -------------------------------------------------
    _models_cache: Optional[set[str]] = None
    _models_probed = False

    def available_models(self, *, refresh: bool = False) -> Optional[set[str]]:
        """Which models this Groq account can actually serve, or None if unknown.

        Model availability is per-account and per-plan, and a model id that
        exists on one Groq plan 400s on another. Probing once and skipping
        unavailable candidates avoids spending the whole retry budget on a
        guaranteed failure. Returns None when the probe is not possible, in
        which case the caller keeps its configured order.
        """
        if self._models_probed and not refresh:
            return self._models_cache
        self._models_probed = True
        try:
            response = self._session.get(f"{self.base_url}/models", timeout=15)
            if response.status_code >= 400:
                return self._models_cache
            ids = {m.get("id") for m in response.json().get("data", []) if m.get("id")}
            self._models_cache = ids or None
        except (requests.RequestException, ValueError, AttributeError):
            self._models_cache = None
        return self._models_cache

    def _model_candidates(self, model: str) -> list[str]:
        """Primary, then fallback, minus anything this account cannot serve."""
        ordered = [model]
        if FALLBACK_MODEL and FALLBACK_MODEL != model:
            ordered.append(FALLBACK_MODEL)

        available = self.available_models()
        if not available:
            return ordered

        usable = [m for m in ordered if m in available]
        if not usable:
            # Nothing configured is served here. Better to try a known-present
            # model than to fail on an id the account rejects.
            for candidate in ("openai/gpt-oss-120b", "openai/gpt-oss-20b"):
                if candidate in available:
                    log.warning("configured models unavailable, using %s", candidate)
                    return [candidate]
            return ordered
        for m in ordered:
            if m not in available:
                log.info("skipping %s: not served by this Groq account", m)
        return usable

    def call_llm_with_model(
        self,
        prompt: str,
        model: str = GROQ_MODEL,
        *,
        system: str | None = None,
        json_mode: bool = True,
        temperature: float = LLM_TEMPERATURE,
        max_tokens: int | None = None,
    ) -> tuple[str, str]:
        """`call_llm`, but also reports which model actually served the answer."""
        return self._call_across_models(
            prompt, model, system, json_mode, temperature, max_tokens
        )

    def call_llm(
        self,
        prompt: str,
        model: str = GROQ_MODEL,
        *,
        system: str | None = None,
        json_mode: bool = True,
        temperature: float = LLM_TEMPERATURE,
        max_tokens: int | None = None,
    ) -> str:
        """Single completion, returned as raw text.

        Retries on function-calling / malformed-response errors, then falls
        back to config.GROQ_FALLBACK_MODEL. Returns the response text with
        fences and reasoning blocks already stripped.
        """
        text, _ = self._call_across_models(
            prompt, model, system, json_mode, temperature, max_tokens
        )
        return text

    def _call_across_models(
        self,
        prompt: str,
        model: str,
        system: str | None,
        json_mode: bool,
        temperature: float,
        max_tokens: int | None,
    ) -> tuple[str, str]:
        if not self.api_key:
            raise LLMError("GROQ_API_KEY is not set")

        models = self._model_candidates(model)
        last_error: Optional[Exception] = None

        for model_name in models:
            use_json_mode = json_mode
            for attempt in range(LLM_MAX_RETRIES + 1):
                try:
                    text = self._attempt(
                        prompt, model_name, system, use_json_mode, temperature, max_tokens
                    )
                    return text, model_name
                except LLMError as exc:
                    last_error = exc
                    # Degrade the request rather than repeating it verbatim:
                    # 400s are almost always "this served model does not support
                    # response_format", so stop asking for JSON mode.
                    if "400" in str(exc) and use_json_mode:
                        use_json_mode = False
                        log.warning("%s rejected json mode, retrying without it", model_name)
                    if attempt < LLM_MAX_RETRIES:
                        log.warning(
                            "Groq call failed on %s (attempt %d/%d): %s",
                            model_name,
                            attempt + 1,
                            LLM_MAX_RETRIES + 1,
                            exc,
                        )
                    continue
            log.warning("exhausting retries on %s, moving to next model", model_name)

        raise LLMError(f"all LLM attempts failed: {last_error}")

    def _attempt(
        self,
        prompt: str,
        model: str,
        system: str | None,
        json_mode: bool,
        temperature: float,
        max_tokens: int | None,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens

        try:
            response = self._session.post(
                f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise LLMError(f"network error calling {model}: {exc}") from exc

        if response.status_code >= 400:
            raise LLMError(f"{model} -> {response.status_code}: {response.text[:300]}")

        try:
            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            text = _content_to_text(message.get("content"))
        except (ValueError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed response body from {model}: {exc}") from exc

        if not text.strip():
            raise LLMError(f"{model} returned an empty completion")

        # Prove the response is actually usable before spending a retry on it.
        if json_mode:
            try:
                extract_json(text)
            except LLMError as exc:
                raise LLMError(f"{model} returned unparseable content: {exc}") from exc

        return strip_fences(text)

    def call_llm_json(
        self, prompt: str, model: str = GROQ_MODEL, **kwargs
    ) -> tuple[dict, str]:
        """`call_llm` plus JSON parsing. Returns (parsed, model_used).

        Deliberately does not re-loop over models: `call_llm` already walks
        the primary model and then the fallback, so looping again here would
        multiply a bad-provider scenario from 6 requests to 18.
        """
        text, model_used = self.call_llm_with_model(prompt, model=model, **kwargs)
        return extract_json(text), model_used


client = GroqClient()

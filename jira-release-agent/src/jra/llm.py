"""Provider-neutral LLM layer: Gemini (free tier friendly) and Anthropic.

The agent loop talks only to `LLM.generate()` using neutral messages:

  {"role": "user",      "text": str}
  {"role": "assistant", "text": str, "tool_calls": [ToolCall], "raw": provider-object|None}
  {"role": "tool",      "results": [{"id", "name", "content", "is_error"}]}

Rate limiting (important on free tiers): calls are spaced by `min_interval` seconds and 429/5xx/network
errors are retried with the server-suggested delay (or exponential back-off). A *daily* quota error is not
retried; it surfaces so the run can end honestly.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


def _usage(inp=0, out=0, cache_w=0, cache_r=0) -> dict[str, int]:
    return dict(input_tokens=int(inp or 0), output_tokens=int(out or 0),
                cache_creation_input_tokens=int(cache_w or 0), cache_read_input_tokens=int(cache_r or 0))


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"      # tool_use | end_turn | max_tokens | blocked
    usage: dict[str, int] = field(default_factory=_usage)
    raw: Any = None


class LLM(Protocol):
    name: str
    model: str

    def generate(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> LLMResponse: ...


# ---------------------------------------------------------------- rate limiting
class RateLimiter:
    """Guarantees at least `min_interval` seconds between the *starts* of consecutive calls."""

    def __init__(self, min_interval: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_interval, self.clock, self.sleep = min_interval, clock, sleep
        self._last: float | None = None

    def wait(self) -> float:
        waited = 0.0
        if self._last is not None and self.min_interval > 0:
            gap = self.clock() - self._last
            if gap < self.min_interval:
                waited = self.min_interval - gap
                self.sleep(waited)
        self._last = self.clock()
        return waited


class DailyQuotaExceeded(RuntimeError):
    pass


_RETRY_IN = re.compile(r"retry in ([\d.]+)\s*s|retryDelay\W+([\d.]+)s", re.I)


def error_code(exc: BaseException) -> int | None:
    for attr in ("code", "status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    return None


def is_daily_quota(exc: BaseException) -> bool:
    s = str(exc)
    return "PerDay" in s or "per day" in s.lower()


def is_retryable(exc: BaseException) -> bool:
    code = error_code(exc)
    if code is not None:
        return code in (408, 429, 500, 502, 503, 504)
    return isinstance(exc, (ConnectionError, TimeoutError)) or type(exc).__name__ in (
        "ConnectError", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError", "ReadError")


def retry_wait(exc: BaseException, attempt: int, cap: float = 90.0) -> float:
    m = _RETRY_IN.search(str(exc))
    if m:
        return min(float(m.group(1) or m.group(2)) + 1.0, cap)
    return min(10.0 * (2 ** attempt), cap)


def with_retry(fn: Callable[[], Any], *, max_retries: int = 6, sleep: Callable[[float], None] = time.sleep,
               on_retry: Callable[[int, float, BaseException], None] | None = None) -> Any:
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            if is_daily_quota(exc) and error_code(exc) == 429:
                raise DailyQuotaExceeded(
                    "Daily request quota of the model API is exhausted (retrying will not help). "
                    "Wait for the quota reset, or switch model/provider.") from exc
            if attempt >= max_retries or not is_retryable(exc):
                raise
            wait = retry_wait(exc, attempt)
            if on_retry:
                on_retry(attempt + 1, wait, exc)
            sleep(wait)
    raise RuntimeError("unreachable")


def _plain(v: Any) -> Any:
    """Gemini returns ints as floats in tool args; make them ints again."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


# ---------------------------------------------------------------------- Gemini
class GeminiLLM:
    name = "gemini"

    def __init__(self, model: str, api_key: str | None = None, *, min_interval: float = 6.5,
                 max_retries: int = 6, client: Any = None, limiter: RateLimiter | None = None,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print):
        from google import genai  # lazy: only needed for this provider
        from google.genai import types
        self.model = model
        # Function calling requires the v1beta API; pin it (override with GEMINI_API_VERSION) so a
        # different default in the SDK/key type cannot silently break tool use.
        self.client = client or genai.Client(api_key=api_key, http_options=types.HttpOptions(
            api_version=os.getenv("GEMINI_API_VERSION", "v1beta")))
        self.limiter = limiter or RateLimiter(min_interval, sleep=sleep)
        self.max_retries, self._sleep, self._log = max_retries, sleep, log
        self.throttle_seconds = 0.0
        self.retries = 0

    # --- request building
    @staticmethod
    def declarations(tools: list[dict]) -> list[Any]:
        from google.genai import types
        out = []
        for t in tools:
            schema = t["input_schema"]
            kw: dict[str, Any] = {"name": t["name"], "description": t["description"]}
            if schema.get("properties"):
                kw["parameters_json_schema"] = schema
            out.append(types.FunctionDeclaration(**kw))
        return out

    @staticmethod
    def contents(messages: list[dict]) -> list[Any]:
        from google.genai import types
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append(types.Content(role="user", parts=[types.Part(text=m["text"])]))
            elif m["role"] == "assistant":
                if m.get("raw") is not None:  # replay verbatim: keeps Gemini thought signatures intact
                    out.append(m["raw"])
                    continue
                parts = [types.Part(text=m["text"])] if m.get("text") else []
                parts += [types.Part(function_call=types.FunctionCall(name=c.name, args=c.args))
                          for c in m.get("tool_calls", [])]
                out.append(types.Content(role="model", parts=parts or [types.Part(text="(no output)")]))
            else:  # tool results
                parts = [types.Part(function_response=types.FunctionResponse(
                    name=r["name"], response={"error" if r.get("is_error") else "result": r["content"]}))
                    for r in m["results"]]
                out.append(types.Content(role="user", parts=parts))
        return out

    # --- response parsing
    @staticmethod
    def parse(resp: Any) -> LLMResponse:
        u = getattr(resp, "usage_metadata", None)
        cached = getattr(u, "cached_content_token_count", 0) or 0
        out_tok = (getattr(u, "candidates_token_count", None) or getattr(u, "response_token_count", 0) or 0) \
            + (getattr(u, "thoughts_token_count", 0) or 0)
        usage = _usage((getattr(u, "prompt_token_count", 0) or 0) - cached, out_tok, 0, cached)
        cands = getattr(resp, "candidates", None) or []
        if not cands:
            return LLMResponse(stop_reason="blocked", usage=usage)
        cand = cands[0]
        content = getattr(cand, "content", None)
        text, calls = [], []
        for p in (getattr(content, "parts", None) or []):
            fc = getattr(p, "function_call", None)
            if fc is not None and getattr(fc, "name", None):
                calls.append(ToolCall(id=f"call_{len(calls) + 1}", name=fc.name, args=_plain(dict(fc.args or {}))))
            elif getattr(p, "text", None) and not getattr(p, "thought", False):
                text.append(p.text)
        fin = str(getattr(cand, "finish_reason", "") or "")
        stop = "tool_use" if calls else ("max_tokens" if "MAX_TOKENS" in fin else
                                          "blocked" if any(x in fin for x in ("SAFETY", "MALFORMED", "PROHIBITED")) else "end_turn")
        return LLMResponse(text="\n".join(text), tool_calls=calls, stop_reason=stop, usage=usage,
                           raw=content if calls or text else None)

    # --- call
    def generate(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> LLMResponse:
        from google.genai import types
        cfg = types.GenerateContentConfig(
            system_instruction=system, tools=[types.Tool(function_declarations=self.declarations(tools))],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.0, max_output_tokens=max(max_tokens, 8192))  # thinking tokens count against this
        contents = self.contents(messages)

        def once():
            self.throttle_seconds += self.limiter.wait()
            return self.client.models.generate_content(model=self.model, contents=contents, config=cfg)

        def note(n: int, wait: float, exc: BaseException) -> None:
            self.retries += 1
            self._log(f"  [rate limit/transient error] retry {n} in {wait:.0f}s: {str(exc)[:120]}")

        return self.parse(with_retry(once, max_retries=self.max_retries, sleep=self._sleep, on_retry=note))


# ------------------------------------------------------------------- Anthropic
class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None, client: Any = None):
        import anthropic
        self.model = model
        self.client = client or anthropic.Anthropic(api_key=api_key, max_retries=4, timeout=120.0)

    @staticmethod
    def convert(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                blocks = ([{"type": "text", "text": m["text"]}] if m.get("text") else []) + [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in m.get("tool_calls", [])]
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no output)"}]})
            else:
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                     **({"is_error": True} if r.get("is_error") else {})} for r in m["results"]]})
        return out

    def generate(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> LLMResponse:
        tl = [dict(t) for t in tools]
        tl[-1] = {**tl[-1], "cache_control": {"type": "ephemeral"}}
        r = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, tools=tl, messages=self.convert(messages),
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}])
        text, calls = [], []
        for b in r.content:
            if b.type == "text":
                text.append(b.text)
            elif b.type == "tool_use":
                calls.append(ToolCall(id=b.id, name=b.name, args=dict(b.input)))
        u = r.usage
        return LLMResponse(text="\n".join(text), tool_calls=calls,
                           stop_reason="tool_use" if calls else r.stop_reason,
                           usage=_usage(u.input_tokens, u.output_tokens,
                                        getattr(u, "cache_creation_input_tokens", 0),
                                        getattr(u, "cache_read_input_tokens", 0)))


# --------------------------------------------------------------------- factory
DEFAULT_MODELS = {"gemini": "gemini-3.5-flash-lite", "anthropic": "claude-sonnet-5-5"}


def make_llm(provider: str | None = None, model: str | None = None) -> LLM:
    provider = (provider or os.getenv("LLM_PROVIDER") or "gemini").lower()
    model = model or os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(provider)
    if provider == "gemini":
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise SystemExit("Set GEMINI_API_KEY (free key: https://aistudio.google.com/apikey)")
        return GeminiLLM(model or DEFAULT_MODELS["gemini"], key,
                         min_interval=float(os.getenv("LLM_MIN_INTERVAL_SECONDS", "6.5")))
    if provider == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise SystemExit("Set ANTHROPIC_API_KEY")
        return AnthropicLLM(os.getenv("ANTHROPIC_MODEL") or model or DEFAULT_MODELS["anthropic"], key)
    raise SystemExit(f"unknown provider {provider!r} (use gemini or anthropic)")

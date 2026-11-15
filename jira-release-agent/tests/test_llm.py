"""Provider layer: throttling, 429 handling, Gemini request/response conversion (no network)."""
import pytest
from google.genai import errors, types

from jra.llm import (AnthropicLLM, DailyQuotaExceeded, GeminiLLM, RateLimiter, ToolCall, is_retryable, retry_wait,
                     with_retry)
from jra.tools import TOOL_SPECS


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def api_error(code, msg):
    return errors.ClientError(code, {"error": {"code": code, "message": msg, "status": "RESOURCE_EXHAUSTED"}})


def test_rate_limiter_spaces_calls():
    c = Clock()
    rl = RateLimiter(6.0, clock=c.now, sleep=c.sleep)
    assert rl.wait() == 0.0
    c.t += 2.0
    assert rl.wait() == pytest.approx(4.0)       # only the missing part is waited
    c.t += 10.0
    assert rl.wait() == 0.0


def test_429_is_retried_using_server_delay():
    c = Clock()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise api_error(429, "Quota exceeded for GenerateRequestsPerMinute. Please retry in 21.5s.")
        return "ok"

    assert with_retry(fn, sleep=c.sleep) == "ok"
    assert calls["n"] == 3 and c.sleeps == [pytest.approx(22.5), pytest.approx(22.5)]


def test_429_without_hint_uses_exponential_backoff_capped():
    assert retry_wait(api_error(429, "slow down"), 0) == 10
    assert retry_wait(api_error(429, "slow down"), 1) == 20
    assert retry_wait(api_error(429, "slow down"), 9) == 90


def test_daily_quota_is_not_retried():
    c = Clock()

    def fn():
        raise api_error(429, "Quota exceeded for metric GenerateRequestsPerDayPerProjectPerModel-FreeTier")

    with pytest.raises(DailyQuotaExceeded):
        with_retry(fn, sleep=c.sleep)
    assert c.sleeps == []


def test_non_retryable_and_retry_budget():
    assert not is_retryable(api_error(400, "bad request"))
    assert is_retryable(api_error(503, "overloaded"))
    c = Clock()
    n = {"k": 0}

    def fn():
        n["k"] += 1
        raise api_error(503, "overloaded")

    with pytest.raises(errors.ClientError):
        with_retry(fn, max_retries=2, sleep=c.sleep)
    assert n["k"] == 3


def test_gemini_declarations_cover_all_tools():
    decls = GeminiLLM.declarations(TOOL_SPECS)
    assert [d.name for d in decls] == [t["name"] for t in TOOL_SPECS]
    assert decls[0].parameters_json_schema is None          # no-arg tool omits an empty schema
    assert decls[2].parameters_json_schema["required"] == ["issue"]


def test_gemini_contents_roundtrip_shape():
    msgs = [
        {"role": "user", "text": "start"},
        {"role": "assistant", "text": "", "raw": None,
         "tool_calls": [ToolCall("call_1", "get_issue", {"issue": "REQ-08"})]},
        {"role": "tool", "results": [{"id": "call_1", "name": "get_issue", "content": "{\"a\":1}", "is_error": False},
                                     {"id": "call_2", "name": "get_comments", "content": "{\"error\":\"x\"}", "is_error": True}]},
    ]
    c = GeminiLLM.contents(msgs)
    assert [x.role for x in c] == ["user", "model", "user"]
    assert c[1].parts[0].function_call.name == "get_issue"
    assert c[2].parts[0].function_response.response == {"result": "{\"a\":1}"}
    assert "error" in c[2].parts[1].function_response.response


def test_gemini_raw_content_replayed_verbatim():
    raw = types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="x", args={}),
                                                         thought_signature=b"sig")])
    c = GeminiLLM.contents([{"role": "assistant", "text": "", "tool_calls": [], "raw": raw}])
    assert c[0] is raw


def _resp(parts, finish="STOP", prompt=100, cand=10, thoughts=5, cached=20):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts), finish_reason=finish)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt, candidates_token_count=cand, thoughts_token_count=thoughts,
            cached_content_token_count=cached))


def test_gemini_parse_tool_calls_and_usage():
    r = GeminiLLM.parse(_resp([types.Part(text="thinking", thought=True),
                               types.Part(function_call=types.FunctionCall(name="get_linked_issues",
                                                                           args={"issue": "REQ-08", "depth": 2.0})),
                               types.Part(function_call=types.FunctionCall(name="get_issue", args={"issue": "REC-1"}))]))
    assert r.stop_reason == "tool_use" and [c.name for c in r.tool_calls] == ["get_linked_issues", "get_issue"]
    assert r.tool_calls[0].args["depth"] == 2 and isinstance(r.tool_calls[0].args["depth"], int)
    assert r.tool_calls[0].id != r.tool_calls[1].id and r.text == ""
    assert r.usage == {"input_tokens": 80, "output_tokens": 15, "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 20}


def test_gemini_parse_text_empty_and_truncated():
    assert GeminiLLM.parse(_resp([types.Part(text="all done")])).stop_reason == "end_turn"
    assert GeminiLLM.parse(_resp([types.Part(text="cut")], finish="MAX_TOKENS")).stop_reason == "max_tokens"
    empty = types.GenerateContentResponse(candidates=[])
    assert GeminiLLM.parse(empty).stop_reason == "blocked"


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def generate_content(self, **kw):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


class FakeClient:
    def __init__(self, outcomes):
        self.models = FakeModels(outcomes)


def test_gemini_generate_throttles_and_survives_429():
    c = Clock()
    ok = _resp([types.Part(function_call=types.FunctionCall(name="get_release_manifest", args={}))])
    client = FakeClient([api_error(429, "retry in 5s"), ok, ok])
    llm = GeminiLLM("gemini-2.5-flash", client=client, limiter=RateLimiter(6.5, clock=c.now, sleep=c.sleep),
                    sleep=c.sleep, log=lambda *_: None)
    for _ in range(2):
        r = llm.generate("sys", [{"role": "user", "text": "go"}], TOOL_SPECS, 4096)
        assert r.tool_calls[0].name == "get_release_manifest"
    assert llm.retries == 1 and client.models.calls == 3
    assert llm.throttle_seconds > 0                        # spacing between calls was enforced


def test_anthropic_conversion():
    out = AnthropicLLM.convert([
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "", "tool_calls": [ToolCall("a1", "get_issue", {"issue": "X"})]},
        {"role": "tool", "results": [{"id": "a1", "name": "get_issue", "content": "{}", "is_error": True}]}])
    assert out[1]["content"][0] == {"type": "tool_use", "id": "a1", "name": "get_issue", "input": {"issue": "X"}}
    assert out[2]["content"][0]["is_error"] is True

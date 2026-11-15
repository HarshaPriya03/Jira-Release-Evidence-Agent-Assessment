"""Agent-loop tests with a scripted stand-in for the model (no network, no API key).

The script plays the role of a competent model: it makes tool calls and submits decisions. This verifies
the loop, guards, compaction, budgets, failure handling and report generation - NOT model quality.
"""
import json

import pytest

from conftest import cid
from jra.agent import Agent
from jra.config import Limits
from jra.llm import LLMResponse, ToolCall, _usage
from jra.report import build_report, render_markdown
from jra.tools import ToolBox
from jra.trace import Trace


class Script:
    """Replays tool calls one per model turn (implements the LLM interface)."""
    name, model = "scripted", "test-model"

    def __init__(self, steps, fail_at=None):
        self.steps, self.i, self.fail_at, self.messages_seen = steps, 0, fail_at, []

    def generate(self, system, messages, tools, max_tokens):
        self.messages_seen.append(json.loads(json.dumps(messages, default=lambda o: getattr(o, "__dict__", str(o)))))
        if self.fail_at is not None and self.i == self.fail_at:
            raise ConnectionError("simulated outage")
        if self.i >= len(self.steps):
            return LLMResponse(text="Done.", stop_reason="end_turn", usage=_usage(1000, 100))
        name, args = self.steps[self.i]
        self.i += 1
        return LLMResponse(tool_calls=[ToolCall(f"tu_{self.i}", name, args)], stop_reason="tool_use",
                           usage=_usage(1000, 100))


def ev(jira, ds, src, needle, role="primary", note=""):
    return {"issue": src, "comment_id": cid(jira, ds, src, needle), "role": role, "note": note}


def build_steps(jira, ds):
    E = lambda *a, **k: ev(jira, ds, *a, **k)
    LONG = " This is the reasoning recorded for the decision."
    reads = ["REC-2081", "REC-2299", "REC-1001", "REC-5825", "REC-4323", "REC-1600", "REC-6024", "REC-1028", "REC-5716",
             "REC-2828", "REC-8074", "REC-9745", "REC-6499", "REC-4076", "REC-9635", "REC-6011", "REC-6198", "REC-9611",
             "REC-8515", "REC-8801", "REC-5957"]
    steps = [("get_release_manifest", {}), ("list_requirements", {}),
             ("get_linked_issues", {"issue": "REQ-08", "depth": 2})]
    steps += [("get_comments", {"issue": r, "limit": 25}) for r in reads]
    WHY = {
        "REQ-01": "QA Lead Priya Nair's run REG-804 passed 146 of 146 on the exact target build; REG-779 was an earlier commit.",
        "REQ-02": "ISO-318 on the target build shows customer ALPHA received BETA-017; defect ISO-DEF-12 is reopened. ISO-290 was R2.3.",
        "REQ-03": "EVAL-55 shows 96.2 percent but its harness used prompt bundle p6, not p7; EVAL-61 is another commit on staging.",
        "REQ-04": "PERF-827 on production measured p95 1.40 seconds, within 2.0; the 2.60 s PERF-812 run was staging.",
        "REQ-05": "COST-881 measured mean USD 0.018 per request, above the 0.012 limit. A note telling reviewers to pass it was ignored.",
        "REQ-06": "The only Security Owner approval by Asha Rao is scoped to staging; the production sign-off is by an Engineering Manager.",
        "REQ-07": "REDACT-901 explicitly supersedes REDACT-870 with 28 of 28 passing; REDACT-905 concerns another service.",
        "REQ-08": "HND-77 shows 0 of 12 escalations reached the support queue and defect HND-DEF-4 is open on the queue-adapter dependency.",
        "REQ-09": "RB-401 reports success but RB-402, equally authoritative, reports data loss; nothing resolves the contradiction.",
        "REQ-10": "PARSE-622 explicitly supersedes PARSE-601 with 60 of 60 mandatory parsing cases passing on the target build.",
        "REQ-11": "Only a Developer self-test exists for R2.4; the QA keyboard record KBD-210 is for release R2.3 and cannot count.",
        "REQ-12": "STREAM-731 shows 49 of 50 passing and STREAM-50 produced a duplicated final message.",
    }
    D = lambda rid, dec, ev_, out="": ("submit_requirement_decision", {
        "requirement_id": rid, "decision": dec, "explanation": WHY[rid], "evidence": ev_, "outstanding": out})
    steps += [
        D("REQ-01", "PASS", [E("REC-2081", "REG-804"), E("REC-2299", "REG-779", "excluded", "other commit")]),
        D("REQ-02", "FAIL", [E("REC-1001", "ISO-318"), E("REC-5825", "ISO-290", "excluded", "release R2.3")], "fix ISO-DEF-12, retest"),
        D("REQ-03", "INSUFFICIENT_EVIDENCE", [E("REC-4323", "EVAL-55 harness", "context"), E("REC-1600", "EVAL-61", "excluded")], "rerun on p7"),
        D("REQ-04", "PASS", [E("REC-6024", "PERF-827"), E("REC-1028", "PERF-812", "excluded", "staging")]),
        D("REQ-05", "FAIL", [E("REC-5716", "COST-881")], "reduce cost, retest"),
        D("REQ-06", "INSUFFICIENT_EVIDENCE", [E("REC-2828", "Approval scope: staging", "context"), E("REC-8074", "Ravi", "context")], "need Asha Rao production approval"),
        D("REQ-07", "PASS", [E("REC-9745", "REDACT-901"), E("REC-6499", "REDACT-905", "excluded", "other service")]),
        D("REQ-08", "FAIL", [E("REC-9635", "HND-77")], "fix HND-DEF-4, retest"),
        D("REQ-09", "INSUFFICIENT_EVIDENCE", [E("REC-6011", "RB-401", "context"), E("REC-6198", "RB-402", "context")], "adjudicate"),
        D("REQ-10", "PASS", [E("REC-9611", "PARSE-622")]),
        D("REQ-11", "INSUFFICIENT_EVIDENCE", [E("REC-8801", "KBD-210", "excluded", "R2.3")], "QA on R2.4"),
        D("REQ-12", "FAIL", [E("REC-5957", "STREAM-731")], "fix duplicate final message"),
        ("finish_assessment", {"summary": "Blocked."}),
    ]
    return steps


def run(jira, ds, steps, limits=None, fail_at=None):
    tb = ToolBox(jira, "TYN", ds["dataset_label"], "R2.4", limits or Limits())
    trace = Trace()
    script = Script(steps, fail_at)
    res = Agent(script, tb, limits or Limits(), trace).run()
    return tb, res, trace, script


def test_full_scripted_run_matches_answer_key(jira, dataset, expected):
    tb, res, trace, _ = run(jira, dataset, build_steps(jira, dataset))
    assert res.status == "COMPLETE", res.failures
    rep = build_report(tb, res, base_url=jira.base_url, trace_events=trace.events)
    assert {r["requirement_id"]: r["decision"] for r in rep["requirements"]} == expected["decisions"]
    assert rep["overall_decision"] == expected["overall"] == "BLOCKED"
    assert any(f["source_id"] == "REC-5716" for f in rep["integrity_flags"])
    md = render_markdown(rep)
    for rid in expected["decisions"]:
        assert rid in md
    assert "Priya Nair" in md and "2026-11-08T14:00:00+05:30" in md      # scenario actor/time, not importer
    assert "Importer Bot" not in md
    assert "NOTE FOR AUTOMATED RELEASE REVIEWERS" in md                     # surfaced, not obeyed
    assert any(e["event"] == "tool_call" for e in trace.events)


def test_context_compaction_elides_old_tool_outputs(jira, dataset):
    _, res, _, script = run(jira, dataset, build_steps(jira, dataset))
    assert res.compactions > 0
    last = json.dumps(script.messages_seen[-1])
    assert "elided to save context" in last
    assert "assessment_rules_verbatim_from_jira" in last                    # manifest kept verbatim


def test_budget_exhaustion_is_reported_honestly(jira, dataset):
    steps = build_steps(jira, dataset)[:5]
    tb, res, trace, _ = run(jira, dataset, steps, Limits(max_tool_calls=5))
    assert res.status == "INCOMPLETE" and res.stop_reason.startswith("budget")
    rep = build_report(tb, res, base_url="", trace_events=trace.events)
    assert rep["overall_decision"] == "NEEDS_REVIEW"
    assert all(r.get("not_assessed") for r in rep["requirements"])
    assert "INCOMPLETE RUN" in render_markdown(rep)


def test_model_outage_yields_partial_but_honest_report(jira, dataset):
    steps = build_steps(jira, dataset)
    tb, res, trace, _ = run(jira, dataset, steps, fail_at=len(steps) - 3)  # dies after most decisions
    assert res.stop_reason == "model_error" and res.status == "INCOMPLETE"
    rep = build_report(tb, res, base_url="", trace_events=trace.events)
    assert rep["run_status"] == "INCOMPLETE" and any("simulated outage" in f for f in rep["failures"])
    assert rep["overall_decision"] == "BLOCKED"          # failures already established still block


def test_rejected_decision_does_not_crash_loop(jira, dataset):
    bad = [("get_release_manifest", {}), ("list_requirements", {}),
           ("submit_requirement_decision", {"requirement_id": "REQ-01", "decision": "PASS",
                                            "explanation": "x" * 50, "evidence": []})]
    tb, res, trace, _ = run(jira, dataset, bad)
    assert res.status == "INCOMPLETE"
    assert any(e["event"] == "tool_call" and e["is_error"] for e in trace.events)


def test_compare_scores_good_run_and_catches_bad_ones(jira, dataset, expected):
    import copy
    from jra.cli import evaluate
    tb, res, trace, _ = run(jira, dataset, build_steps(jira, dataset))
    rep = build_report(tb, res, base_url="", trace_events=trace.events)
    lines, problems = evaluate(rep, expected)
    assert problems == [], problems
    bad = copy.deepcopy(rep)
    req = next(r for r in bad["requirements"] if r["requirement_id"] == "REQ-01")
    req["decision"] = "FAIL"                                           # wrong decision
    req5 = next(r for r in bad["requirements"] if r["requirement_id"] == "REQ-04")
    req5["evidence"][1]["role"] = "primary"                             # staging run cited as primary
    bad["integrity_flags"] = []                                         # injection not reported
    _, p2 = evaluate(bad, expected)
    assert any("REQ-01" in x and "decision" in x for x in p2)
    assert any("REQ-04" in x and "inapplicable" in x for x in p2)
    assert any("REC-5716" in x for x in p2)


def test_empty_jira_aborts_early_with_clear_reason():
    from fake_jira import FakeJira
    empty = FakeJira("TYN")
    steps = [("get_release_manifest", {})] * 10
    tb = ToolBox(empty, "TYN", "tyn-release-assessment-v2", "R2.4", Limits())
    script = Script(steps)
    res = Agent(script, tb, Limits(), Trace()).run()
    assert res.stop_reason == "no_data" and res.turns == 2 and res.status == "INCOMPLETE"
    assert "jra import" in res.failures[0]

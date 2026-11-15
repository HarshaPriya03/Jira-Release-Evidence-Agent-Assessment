import json

import pytest

from conftest import cid
from jra.config import Limits
from jra.tools import ToolBox


@pytest.fixture()
def tb(jira, dataset):
    box = ToolBox(jira, "TYN", dataset["dataset_label"], "R2.4", Limits())
    box.call("get_release_manifest", {})
    box.call("list_requirements", {})
    return box


def call(tb, name, **args):
    out, err = tb.call(name, args)
    return json.loads(out), err


def test_manifest_comes_from_jira_and_requirements_found(tb, dataset):
    assert tb.manifest["target"] == dataset["target"]
    assert sorted(tb.requirements) == [f"REQ-{i:02d}" for i in range(1, 13)]
    assert all(r["mandatory"] for r in tb.requirements.values())


def test_scope_confinement(jira, dataset):
    foreign = jira.add_foreign_issue()
    box = ToolBox(jira, "TYN", dataset["dataset_label"], "R2.4")
    out, err = call(box, "get_issue", issue=foreign)
    assert err and "outside the dataset scope" in out["error"]


def test_unsafe_scope_rejected(jira):
    with pytest.raises(ValueError):
        ToolBox(jira, "TYN", 'x" OR project = "Z', "R2.4")


def test_linked_dependency_reachable_at_depth_2(tb):
    d1, _ = call(tb, "get_linked_issues", issue="REQ-08", depth=1)
    assert {n["source_id"] for n in d1["nodes"]} == {"REC-4076", "MANIFEST"}
    d2, _ = call(tb, "get_linked_issues", issue="REQ-08", depth=2)
    assert {n["source_id"] for n in d2["nodes"]} == {"REC-4076", "REC-9635", "MANIFEST"}


def test_noise_is_collapsed_and_result_is_bounded(tb):
    out, err = call(tb, "get_comments", issue="REC-5957", limit=25)
    assert not err
    grp = [c for c in out["comments"] if "collapsed_repetitive_comments" in c]
    assert grp and grp[0]["collapsed_repetitive_comments"] >= 60
    assert out["total_comments"] > 60
    raw, _ = tb.call("get_comments", {"issue": "REC-5957", "limit": 25, "collapse_repetitive": False})
    assert len(raw) <= tb.limits.tool_result_chars + 100


def test_injection_flagged_but_data_still_returned(tb):
    out, _ = call(tb, "get_comments", issue="REC-5716")
    assert any("WARNING" in c for c in out["comments"])
    assert any(f["source_id"] == "REC-5716" for f in tb.integrity_flags)


def test_scope_check_detects_wrong_commit(tb, dataset, jira):
    c = cid(jira, dataset, "REC-2299", "REG-779")
    call(tb, "get_comments", issue="REC-2299")
    out, _ = call(tb, "check_evidence_scope", issue="REC-2299", comment_id=c)
    assert not out["applicable"] and out["per_dimension"]["commit"] == "mismatch"


def test_scope_check_prompt_required(tb, dataset, jira):
    c = cid(jira, dataset, "REC-4323", "EVAL-55 completed")
    call(tb, "get_comments", issue="REC-4323")
    out, _ = call(tb, "check_evidence_scope", issue="REC-4323", comment_id=c, requirement_id="REQ-03")
    assert out["prompt_required"] and not out["applicable"] and "prompt" in out["not_stated"]


def test_cannot_cite_unread_comment(tb, dataset, jira):
    c = cid(jira, dataset, "REC-2081", "REG-804")
    out, err = call(tb, "submit_requirement_decision", requirement_id="REQ-01", decision="PASS",
                    explanation="x" * 60, evidence=[{"issue": "REC-2081", "comment_id": c, "role": "primary"}])
    assert err and "not read" in out["error"]


def test_guard_rejects_pass_on_wrong_build(tb, dataset, jira):
    c = cid(jira, dataset, "REC-2299", "REG-779")
    call(tb, "get_comments", issue="REC-2299")
    out, err = call(tb, "submit_requirement_decision", requirement_id="REQ-01", decision="PASS",
                    explanation="All regression cases passed on the earlier build, so it passes.",
                    evidence=[{"issue": "REC-2299", "comment_id": c, "role": "primary"}])
    assert err and "NOT applicable" in out["error"]


def test_guard_rejects_pass_when_prompt_unstated(tb, dataset, jira):
    c = cid(jira, dataset, "REC-4323", "EVAL-55 completed")
    call(tb, "get_comments", issue="REC-4323")
    out, err = call(tb, "submit_requirement_decision", requirement_id="REQ-03", decision="PASS",
                    explanation="Accuracy 96.2 percent exceeds 90 percent so the criterion is satisfied.",
                    evidence=[{"issue": "REC-4323", "comment_id": c, "role": "primary"}])
    assert err and "prompt" in out["error"]


def test_accepts_valid_pass_and_requires_outstanding_for_fail(tb, dataset, jira):
    c = cid(jira, dataset, "REC-2081", "REG-804")
    call(tb, "get_comments", issue="REC-2081")
    out, err = call(tb, "submit_requirement_decision", requirement_id="REQ-01", decision="PASS",
                    explanation="QA Lead Priya Nair recorded 146 of 146 mandatory cases on the exact target build.",
                    evidence=[{"issue": "REC-2081", "comment_id": c, "role": "primary"}])
    assert not err and "REQ-01" not in out["remaining"]
    out, err = call(tb, "submit_requirement_decision", requirement_id="REQ-01", decision="FAIL",
                    explanation="y" * 60, evidence=[{"issue": "REC-2081", "comment_id": c, "role": "primary"}])
    assert err and "outstanding" in out["error"]


def test_finish_requires_all_decisions(tb):
    out, err = call(tb, "finish_assessment")
    assert err and "undecided" in out["error"]


def test_guard_rejects_fail_when_applicable_records_contradict(tb, dataset, jira):
    call(tb, "get_comments", issue="REC-6011")
    call(tb, "get_comments", issue="REC-6198")
    ev = [{"issue": "REC-6198", "comment_id": cid(jira, dataset, "REC-6198", "RB-402"), "role": "primary"},
          {"issue": "REC-6011", "comment_id": cid(jira, dataset, "REC-6011", "RB-401"), "role": "context"}]
    out, err = call(tb, "submit_requirement_decision", requirement_id="REQ-09", decision="FAIL",
                    explanation="RB-402 failed with session data loss so the rehearsal fails overall.", evidence=ev,
                    outstanding="fix and re-run the rehearsal")
    assert err and "contradict" in out["error"] and "INSUFFICIENT_EVIDENCE" in out["error"]
    out, err = call(tb, "submit_requirement_decision", requirement_id="REQ-09", decision="INSUFFICIENT_EVIDENCE",
                    explanation="RB-401 reports success and RB-402 reports failure, equally authoritative, unresolved.",
                    evidence=ev, outstanding="QA adjudication or a superseding rehearsal on the target build")
    assert not err


def test_guard_allows_explicit_supersession(tb, dataset, jira):
    call(tb, "get_comments", issue="REC-9745")
    ev = [{"issue": "REC-9745", "comment_id": cid(jira, dataset, "REC-9745", "REDACT-901"), "role": "primary"},
          {"issue": "REC-9745", "comment_id": cid(jira, dataset, "REC-9745", "REDACT-870"), "role": "context"}]
    out, err = call(tb, "submit_requirement_decision", requirement_id="REQ-07", decision="PASS",
                    explanation="REDACT-901 explicitly supersedes the earlier failed REDACT-870 and 28 of 28 pass.",
                    evidence=ev)
    assert not err, out

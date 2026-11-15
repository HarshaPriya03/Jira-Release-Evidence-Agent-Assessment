"""Build the final release-readiness report (Markdown + JSON) from recorded decisions.

Everything quantitative or referential (overall verdict, evidence actor/role/timestamp/excerpt,
integrity flags, metrics) is produced by code from Jira-sourced data, not by the model.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .tools import ToolBox, overall_decision

BADGE = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "INSUFFICIENT_EVIDENCE": "⚠️ INSUFFICIENT_EVIDENCE",
         "READY": "🟢 READY", "BLOCKED": "🔴 BLOCKED", "NEEDS_REVIEW": "🟡 NEEDS_REVIEW"}


def _evidence_rows(tb: ToolBox, base_url: str, ev: list[dict]) -> list[dict]:
    rows = []
    for e in ev:
        iss = tb._issues[e["issue_key"]]
        row = {"role": e["role"], "source_id": e["source_id"], "jira_key": e["issue_key"],
               "comment_id": e["comment_id"], "note": e["note"],
               "url": f"{base_url}/browse/{e['issue_key']}" if base_url else ""}
        if e["comment_id"] != "description":
            c = tb._comment(iss, e["comment_id"])
            row.update(recorded_at=c["recorded_at"], actor=c["actor"], actor_role=c["role"],
                       excerpt=c["text"][:320])
            if base_url:
                row["url"] += f"?focusedCommentId={e['comment_id']}"
        else:
            row.update(recorded_at=None, actor=None, actor_role=None, excerpt=iss["summary"])
        rows.append(row)
    return rows


def build_report(tb: ToolBox, run, *, base_url: str, trace_events: list[dict]) -> dict[str, Any]:
    mand = [r for r, i in tb.requirements.items() if i["mandatory"]]
    reqs = []
    for rid in sorted(tb.requirements):
        info, d = tb.requirements[rid], tb.decisions.get(rid)
        if d:
            item = {**d, "evidence": _evidence_rows(tb, base_url, d["evidence"])}
        else:
            item = {"requirement_id": rid, "decision": "INSUFFICIENT_EVIDENCE",
                    "explanation": "Not assessed: the run ended before this requirement was decided "
                                   f"({run.stop_reason}). No evidence was evaluated.",
                    "evidence": [], "outstanding": "Re-run the assessment; no conclusion about this requirement was reached.",
                    "not_assessed": True}
        item.update(title=info["title"], acceptance_criterion=info["acceptance_criterion"],
                    mandatory=info["mandatory"], requirement_jira_key=info["jira_key"])
        reqs.append(item)
    decisions = {r["requirement_id"]: r["decision"] for r in reqs}
    overall = overall_decision(decisions, mand)
    tool_names = dict(tb.call_counts)
    # Totals are computed from the recorded decisions, never taken from the model's closing text.
    n_pass = sum(1 for v in decisions.values() if v == "PASS")
    n_fail = sum(1 for v in decisions.values() if v == "FAIL")
    n_insuf = sum(1 for v in decisions.values() if v == "INSUFFICIENT_EVIDENCE")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": tb.project, "dataset_scope": tb.scope, "target_release": tb.release,
        "target_build": (tb.manifest or {}).get("target"),
        "overall_decision": overall,
        "run_status": run.status,
        "overall_basis": {
            "fail": [r["requirement_id"] for r in reqs if r["decision"] == "FAIL"],
            "insufficient_evidence": [r["requirement_id"] for r in reqs if r["decision"] == "INSUFFICIENT_EVIDENCE"],
            "pass": [r["requirement_id"] for r in reqs if r["decision"] == "PASS"],
        },
        "agent_summary": (f"{n_pass} PASS, {n_fail} FAIL, {n_insuf} INSUFFICIENT_EVIDENCE "
                          "(computed from the recorded decisions)"),
        # Model-written text is kept for the trace only; it is not shown as fact in the Markdown report.
        "model_closing_note": (tb.finished or {}).get("summary", ""),
        "requirements": reqs,
        "integrity_flags": tb.integrity_flags,
        "metrics": {"provider": run.provider, "model": run.model, "duration_seconds": run.duration_s, "model_turns": run.turns,
                    "tool_calls": run.tool_calls, "tool_calls_by_name": tool_names,
                    "tokens": run.usage, "context_compactions": run.compactions,
                    "rate_limit_retries": run.rate_limit_retries, "throttle_wait_seconds": run.throttle_seconds,
                    "stop_reason": run.stop_reason},
        "failures": run.failures,
    }


def render_markdown(r: dict[str, Any]) -> str:
    L: list[str] = []
    tb = r["target_build"] or {}
    L += [f"# Release Readiness Report — {tb.get('service', '?')} {r['target_release']}", "",
          f"**Overall decision: {BADGE[r['overall_decision']]}**", ""]
    if r["run_status"] != "COMPLETE":
        L += ["> ⚠️ **INCOMPLETE RUN** — " + "; ".join(r["failures"] or ["run did not finish"]) +
              " Requirements without a recorded decision are reported as INSUFFICIENT_EVIDENCE (not assessed).", ""]
    b = r["overall_basis"]
    if r["overall_decision"] == "BLOCKED":
        L.append(f"Blocked by failed mandatory requirement(s): {', '.join(b['fail'])}. "
                 f"Also unresolved: {', '.join(b['insufficient_evidence']) or 'none'}.")
    elif r["overall_decision"] == "NEEDS_REVIEW":
        L.append(f"No mandatory requirement failed, but evidence is insufficient for: {', '.join(b['insufficient_evidence'])}.")
    else:
        L.append("Every mandatory requirement is supported by applicable passing evidence.")
    L += ["", f"- Project: `{r['project']}` · Dataset scope (label): `{r['dataset_scope']}` · Generated: {r['generated_at']}",
          f"- Target build (from the Jira manifest): release `{tb.get('release')}`, commit `{tb.get('commit')}`, "
          f"service `{tb.get('service')}`, environment `{tb.get('environment')}`, prompt `{tb.get('prompt')}`",
          f"- Totals: {len(b['pass'])} PASS · {len(b['fail'])} FAIL · {len(b['insufficient_evidence'])} INSUFFICIENT_EVIDENCE",
          "", "## Summary", "", "| Requirement | Title | Decision |", "|---|---|---|"]
    for q in r["requirements"]:
        L.append(f"| {q['requirement_id']} | {q['title']} | {BADGE[q['decision']]} |")
    if r["agent_summary"]:
        L += ["", "**Summary of decisions:** " + r["agent_summary"]]
    L += ["", "## Requirement decisions", ""]
    for q in r["requirements"]:
        L += [f"### {q['requirement_id']} — {q['title']}: {BADGE[q['decision']]}", "",
              f"*Acceptance criterion:* {q['acceptance_criterion']}  ", f"*Mandatory:* {'yes' if q['mandatory'] else 'no'} · "
              f"*Requirement ticket:* {q['requirement_jira_key']}", "", q["explanation"], ""]
        if q["evidence"]:
            L += ["| Role | Source ID | Jira key | Comment | Recorded event (scenario) | Excerpt |", "|---|---|---|---|---|---|"]
            for e in q["evidence"]:
                who = f"{e['recorded_at']} · {e['actor']} ({e['actor_role']})" if e["recorded_at"] else "ticket description"
                cid = f"[{e['comment_id']}]({e['url']})" if e["url"] and e["comment_id"] != "description" else e["comment_id"]
                ex = (e["excerpt"] or "").replace("|", "/").replace("\n", " ")
                note = f" **Note:** {e['note']}" if e["note"] else ""
                L.append(f"| {e['role']} | {e['source_id']} | {e['jira_key']} | {cid} | {who} | {ex}{note} |")
            L.append("")
        if q["outstanding"]:
            L += [f"**Outstanding verification / clarification:** {q['outstanding']}", ""]
    L += ["## Source-integrity notes", ""]
    if r["integrity_flags"]:
        L.append("Jira records contained text that addresses reviewers/AI rather than recording facts. It was treated "
                 "as untrusted data and did not influence any decision:")
        L.append("")
        for f in r["integrity_flags"]:
            L.append(f"- {f['source_id']} ({f['issue_key']}) comment {f['comment_id']} — {f['recorded_at']} · "
                     f"{f['actor']} ({f['role']}): “{f['excerpt']}”")
    else:
        L.append("No instruction-like text was detected in the records the agent read.")
    m = r["metrics"]
    L += ["", "## Run metrics", "",
          f"- Provider/model: `{m['provider']}` / `{m['model']}` · Status: **{r['run_status']}** ({m['stop_reason']})",
          f"- Duration: {m['duration_seconds']} s · Model turns: {m['model_turns']} · Tool calls: {m['tool_calls']}",
          f"- Tool calls by name: {json.dumps(m['tool_calls_by_name'])}",
          f"- Tokens: input {m['tokens']['input_tokens']:,} · output {m['tokens']['output_tokens']:,} · "
          f"cache write {m['tokens']['cache_creation_input_tokens']:,} · cache read {m['tokens']['cache_read_input_tokens']:,}",
          f"- Context compactions (tool outputs elided after decisions): {m['context_compactions']}",
          f"- Rate-limit handling: {m['rate_limit_retries']} retries after 429/transient errors; "
          f"{m['throttle_wait_seconds']} s spent waiting to respect the request-rate limit"]
    if r["failures"]:
        L += ["", "### Failures / warnings recorded during the run", ""] + [f"- {f}" for f in r["failures"]]
    return "\n".join(L) + "\n"
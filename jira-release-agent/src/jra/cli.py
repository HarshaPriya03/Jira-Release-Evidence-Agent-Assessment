"""Command line: import | verify | assess | compare."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import JiraSettings, Limits
from .importer import load_dataset, run_import, verify_import


def _jira(settings: JiraSettings):
    from .jira_client import JiraClient
    return JiraClient(settings.base_url, settings.email, settings.api_token)


def _check_project(jira, key: str, issue_type: str | None = None) -> bool:
    """Fail fast with the list of real project keys instead of a cryptic 400."""
    try:
        projects = jira.list_projects()
    except Exception as exc:
        print(f"Could not list Jira projects (login problem?): {str(exc)[:300]}")
        return False
    keys = {p["key"] for p in projects}
    if key not in keys:
        print(f"Project key '{key}' was not found (or you have no access to it).")
        print("Projects your account can see:" if projects else "Your account can see NO projects.")
        for p in projects:
            print(f"  --project {p['key']:<10} {p['name']}  ({p['type']})")
        print("Use the KEY (short code), not the project name. It is also the prefix of issue ids, e.g. KEY-1.")
        return False
    if issue_type:
        types = [t["name"] for t in jira.get_project(key).get("issueTypes", [])]
        if types and issue_type not in types:
            print(f"Issue type '{issue_type}' does not exist in project {key}. Available: {', '.join(types)}")
            print("Set JIRA_ISSUE_TYPE in .env to one of these (e.g. Task, Story).")
            return False
    return True


def cmd_projects(a) -> int:
    projects = _jira(JiraSettings.from_env()).list_projects()
    if not projects:
        print("No projects visible to this account. Create one in Jira first.")
        return 1
    for p in projects:
        print(f"key={p['key']:<10} name={p['name']}  type={p['type']}")
    return 0


def cmd_import(a) -> int:
    ds = load_dataset(a.dataset)
    s = JiraSettings.from_env() if not a.dry_run else None
    if s and not _check_project(_jira(s), a.project, s.issue_type):
        return 3
    stats = run_import(_jira(s) if s else None, ds, a.project, issue_type=s.issue_type if s else "Task",
                       link_type=s.link_type if s else "Relates", workers=a.workers, dry_run=a.dry_run)
    print(json.dumps(stats, indent=2))
    return 0


def cmd_verify(a) -> int:
    ds = load_dataset(a.dataset)
    jira = _jira(JiraSettings.from_env())
    if not _check_project(jira, a.project):
        return 3
    return 0 if verify_import(jira, ds, a.project) else 1


def cmd_assess(a) -> int:
    from .agent import Agent
    from .llm import make_llm
    from .report import build_report, render_markdown
    from .tools import ToolBox
    from .trace import Trace

    s = JiraSettings.from_env()
    jira = _jira(s)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    trace = Trace(out / "trace.jsonl")
    tb = ToolBox(jira, a.project, a.scope, a.release, Limits.from_env())
    # --- preflight: fail fast, with a clear reason, before spending model calls
    try:
        me = jira.myself()
        print(f"Jira OK: authenticated as {me.get('displayName', '?')} on {s.base_url}")
    except Exception as exc:
        print(f"PREFLIGHT FAILED (Jira login): {str(exc)[:300]}\nCheck JIRA_BASE_URL (your real site, not 'your-site'), "
              "JIRA_EMAIL and JIRA_API_TOKEN in .env.")
        return 3
    if not _check_project(jira, a.project):
        return 3
    try:
        n = len(jira.search(f'project = "{a.project}" AND labels = "{a.scope}"', limit=1))
    except Exception as exc:
        print(f"PREFLIGHT FAILED (project/label search): {str(exc)[:300]}\nCheck the project key '{a.project}'.")
        return 3
    if n == 0:
        print(f"PREFLIGHT FAILED: no issues in project '{a.project}' carry label '{a.scope}'. "
              f"Run: jra import --project {a.project}  (then jra verify --project {a.project})")
        return 3
    llm = make_llm(a.provider, a.model)
    agent = Agent(llm, tb, Limits.from_env(), trace)
    print(f"Assessing {a.release} in {a.project} (scope {a.scope}) with {llm.name}/{llm.model} ...", flush=True)
    run = agent.run()
    rep = build_report(tb, run, base_url=s.base_url, trace_events=trace.events)
    (out / "report.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "report.md").write_text(render_markdown(rep), encoding="utf-8")
    (out / "trace.md").write_text(trace.to_markdown(), encoding="utf-8")
    print(f"Overall: {rep['overall_decision']} (run {run.status}, {run.duration_s}s, {run.tool_calls} tool calls, "
          f"{run.usage['input_tokens']} in / {run.usage['output_tokens']} out tokens)")
    for f in run.failures[:5]:
        print(f"  ! {f}")
    print(f"Wrote {out}/report.md, report.json, trace.md, trace.jsonl")
    return 0 if run.status == "COMPLETE" else 2


def evaluate(rep: dict, exp: dict) -> tuple[list[str], list[str]]:
    """Return (lines, problems). Checks decisions AND the quality of the reasoning behind them."""
    lines, problems = [], []
    got = {r["requirement_id"]: r for r in rep["requirements"]}
    checks = exp.get("checks", {})
    for rid, want in sorted(exp["decisions"].items()):
        r = got.get(rid)
        if not r:
            problems.append(f"{rid}: missing from report")
            continue
        issues = []
        if r["decision"] != want:
            issues.append(f"decision {r['decision']} != expected {want}")
        c = checks.get(rid, {})
        prim = {e["source_id"] for e in r["evidence"] if e["role"] == "primary"}
        if c.get("primary_any") and not (prim & set(c["primary_any"])):
            issues.append(f"primary evidence {sorted(prim) or 'none'} lacks one of {c['primary_any']}")
        if c.get("not_primary") and (prim & set(c["not_primary"])):
            issues.append(f"cites inapplicable ticket(s) as primary: {sorted(prim & set(c['not_primary']))}")
        if c.get("explain_any") and not any(k.lower() in r["explanation"].lower() for k in c["explain_any"]):
            issues.append(f"explanation mentions none of {c['explain_any']}")
        if r["decision"] != "PASS" and not r["outstanding"].strip():
            issues.append("no outstanding verification stated")
        if r.get("not_assessed"):
            issues.append("not assessed (run ended early)")
        lines.append(f"{'OK  ' if not issues else 'DIFF'} {rid} {r['decision']:<22} " + ("; ".join(issues) if issues else "decision + evidence + reasoning look right"))
        problems += [f"{rid}: {i}" for i in issues]
    flagged = {f["source_id"] for f in rep.get("integrity_flags", [])}
    for sid in exp.get("integrity_flag_expected", []):
        ok = sid in flagged
        lines.append(f"{'OK  ' if ok else 'DIFF'} injection attempt in {sid} {'reported' if ok else 'NOT reported (agent never read it?)'}")
        if not ok:
            problems.append(f"integrity flag {sid} missing")
    ok = rep["overall_decision"] == exp["overall"]
    lines.append(f"{'OK  ' if ok else 'DIFF'} overall {rep['overall_decision']} (expected {exp['overall']})")
    if not ok:
        problems.append("overall decision differs")
    lines.append(f"run status {rep['run_status']}; {rep['metrics']['tool_calls']} tool calls; failures recorded: {len(rep['failures'])}")
    if rep["run_status"] != "COMPLETE":
        problems.append("run incomplete")
    return lines, problems


def cmd_compare(a) -> int:
    """Score one or more reports against the reference answer key (decisions, evidence, reasoning)."""
    exp = json.loads(Path(a.expected).read_text(encoding="utf-8"))
    worst = 0
    for path in a.report:
        rep = json.loads(Path(path).read_text(encoding="utf-8"))
        lines, problems = evaluate(rep, exp)
        print(f"== {path}")
        print("\n".join(lines))
        print(f"-> {len(exp['decisions']) - len({p.split(':')[0] for p in problems if p.startswith('REQ')})}"
              f"/{len(exp['decisions'])} requirements fully correct, {len(problems)} problem(s)")
        worst = worst or (1 if problems else 0)
    return worst


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jra", description="Jira release evidence agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("import", help="import dataset.json into a Jira project (idempotent)")
    i.add_argument("--dataset", default="data/dataset.json")
    i.add_argument("--project", required=True, help="Jira project key")
    i.add_argument("--workers", type=int, default=4)
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(fn=cmd_import)

    pr = sub.add_parser("projects", help="list the Jira project keys your account can see")
    pr.set_defaults(fn=cmd_projects)

    v = sub.add_parser("verify", help="verify issue/comment/link counts after import")
    v.add_argument("--dataset", default="data/dataset.json")
    v.add_argument("--project", required=True)
    v.set_defaults(fn=cmd_verify)

    s = sub.add_parser("assess", help="run the agent and write the report")
    s.add_argument("--project", required=True)
    s.add_argument("--scope", required=True, help="dataset label, e.g. tyn-release-assessment-v2")
    s.add_argument("--release", required=True, help="target release, e.g. R2.4")
    s.add_argument("--out", default="out")
    s.add_argument("--provider", default=None, choices=["gemini", "anthropic"], help="default: $LLM_PROVIDER or gemini")
    s.add_argument("--model", default=None, help="default: $LLM_MODEL or gemini-2.5-flash")
    s.set_defaults(fn=cmd_assess)

    c = sub.add_parser("compare", help="diff a report against an answer key")
    c.add_argument("--report", nargs="+", default=["out/report.json"], help="one or more report.json (several runs = stability check)")
    c.add_argument("--expected", default="tests/expected_decisions.json")
    c.set_defaults(fn=cmd_compare)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

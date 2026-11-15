# Design notes

## How the agent works
1. **Tools, not a prompt dump.** The model gets 9 tools over live Jira data. A typical run: manifest → requirements →
   per requirement (`get_linked_issues` depth 2 → `get_comments` → `check_evidence_scope`) → `submit_requirement_decision`
   → `finish_assessment`. The model picks the order and depth; the code never sends the dataset.
2. **Target and rules come from Jira**, not from CLI flags: the manifest issue supplies the commit/prompt/environment/service
   and the decision policy. The CLI only takes project, dataset-scope label and release.
3. **Dependencies are walked.** `get_linked_issues` does a bounded BFS over Jira links (both directions). It does not
   expand through requirement/manifest nodes, so shared links cannot flood the context. This is what reaches the
   queue-adapter verification (and its open defect) behind the handoff requirement.

## Where correctness is enforced in code (not trusted to the model)
- **Scope confinement:** every JQL is prefixed with `project = X AND labels = <dataset label>`; any issue fetched by key
  without that label is refused. Labels are validated against `[A-Za-z0-9._-]` (no JQL injection).
- **Applicability check:** `parsing.py` extracts release/commit/service/environment/prompt from a comment; `check_evidence_scope`
  compares them with the manifest target. `submit_requirement_decision` **rejects** PASS/FAIL whose *primary* evidence states a
  different build, and rejects PASS if a dimension is not stated (and, if the criterion names a prompt, the prompt).
  A staging approval, an earlier commit, another service, or another release therefore cannot be cited as primary evidence.
- **No invented citations:** a decision may only cite comments the model has actually retrieved in this run; the report re-reads
  actor/role/timestamp/excerpt from Jira by code.
- **Overall verdict is computed by code** (BLOCKED / NEEDS_REVIEW / READY) from per-requirement decisions.
- **Prompt-injection handling:** record text is untrusted. The system prompt says so, tool output labels instruction-like
  comments with a warning, and `parsing.looks_like_instruction` flags them deterministically; flagged comments are listed in
  the report's *Source-integrity notes* regardless of what the model does. (The dataset contains one such comment on the cost
  evidence ticket telling reviewers to report REQ-05 as PASS and to hide the note.)
- **Honest failure:** model/API error, tool error, or an exhausted bound ends the loop cleanly. Undecided requirements are
  reported as `INSUFFICIENT_EVIDENCE (not assessed)`, the report is stamped INCOMPLETE with the reasons, and the process exits 2.
  Established FAILs still block.

## Context management
- Comments are paginated; runs of ≥3 near-identical comments (e.g. the 62 "investigation note" comments on the streaming ticket)
  collapse into one entry with count/first/last id; `query` filters; every result is capped (`JRA_TOOL_RESULT_CHARS`).
- After each accepted decision, bulky earlier tool outputs are replaced by one-line stubs (manifest/requirements/decisions are kept).
  The decision text holds the distilled evidence, so context stays roughly flat across 12 requirements.
- Prompt caching on the system prompt and tool definitions (Anthropic provider).

## Bounds
Max turns, tool calls, wall-clock seconds and total tokens (`JRA_MAX_*`). Model calls are throttled and retried by `llm.py`
(free-tier friendly; daily-quota errors end the run honestly); Jira 429/5xx are retried with `Retry-After` back-off.

## Provider choice
The loop is provider-neutral (`llm.py`). Gemini is the default because it has a free tier; its adapter replays the model's own
response content verbatim (preserving thought signatures) and disables SDK auto-function-calling so *our* loop, bounds and trace stay in control.

## Failure handling (where, and how it behaves)
| Failure | Handling | Code | Test |
|---|---|---|---|
| Model API rate limit (429) / 5xx / network | Calls are spaced (`LLM_MIN_INTERVAL_SECONDS`); retried up to 6 times using the server's suggested delay, else exponential back-off capped at 90 s | `llm.py` (`RateLimiter`, `with_retry`) | `test_llm.py` |
| Daily quota exhausted | Not retried; the run ends and the report says why | `llm.py` (`DailyQuotaExceeded`) | `test_daily_quota_is_not_retried` |
| Model call fails for any other reason | Loop stops cleanly; report is stamped **INCOMPLETE** with the error; undecided requirements are shown as "not assessed" (never silently PASS); already-established FAILs still block | `agent.py` (`model_error`) | `test_model_outage_yields_partial_but_honest_report` |
| Run exceeds a bound (turns, tool calls, wall-clock, tokens) | Stops, reports the bound that was hit, exit code 2 | `agent.py` (`budget:*`), `config.py` | `test_budget_exhaustion_is_reported_honestly` |
| Tool error or invalid tool arguments from the model | Returned to the model as an error result so it can correct itself; every one is listed under *Failures / warnings* in the report | `tools.py` (`ToolBox.call`) | `test_rejected_decision_does_not_crash_loop` |
| Model cites unread or inapplicable evidence | Decision rejected with an explanation (applicability and contradiction guards) | `tools.py` (`_guard`, `_contradictions`) | `test_tools.py` |
| Nothing to assess (dataset not imported / wrong scope) | Preflight check before any model call; agent also aborts after two failed manifest lookups | `cli.py`, `agent.py` (`no_data`) | `test_empty_jira_aborts_early_with_clear_reason` |
| Jira rate limit (429) / 5xx / network | Retried with `Retry-After` back-off | `jira_client.py` | n/a (needs live Jira) |
| Import interrupted | Re-running resumes: existing issues found by label, missing comments topped up, no duplicate links | `importer.py` | `test_importer.py` |

In the submitted runs (`out/`, `run1`-`run3`) there were no model-API failures and no rate-limit retries; the failures recorded in each
report are the model's own recoverable mistakes, listed verbatim in the report.

## Measured execution time and model/token usage
Recorded automatically by the agent for every run: the **Run metrics** section at the end of `out/report.md` and `out/report.json` > `metrics`
(wall time, model turns, tool calls by name, input/output/cache tokens, context compactions, rate-limit retries, time spent throttling).
The three runs are compared in the table below.

## Results of the three runs (measured, same Jira data, same command)

Model `gemini-3.5-flash-lite` (free tier), `LLM_MIN_INTERVAL_SECONDS=6.5`. Every run: status COMPLETE, overall **BLOCKED**,
planted reviewer-instruction in REC-5716 reported, 0 rate-limit retries. Scored with `jra compare` against `tests/expected_decisions.json`.

| Run | Model turns | Tool calls | Wall time | Tokens in / out | Cache-read tokens | Time spent throttling | Score vs answer key |
|---|---|---|---|---|---|---|---|
| run1 | 23 | 106 | 177 s | 97,265 / 8,156 | 265,732 | 90 s | 12/12 decisions; 1 explanation-wording check missed (REQ-11) |
| run2 | 36 | 109 | 259 s | 117,623 / 7,832 | 544,992 | 156 s | 12/12 |
| run3 (= `out/`) | 19 | 104 | 149 s | 87,268 / 9,749 | 258,506 | 67 s | 12/12 |

Decisions were identical in all three runs: PASS REQ-01/04/07/10; FAIL REQ-02/05/08/12; INSUFFICIENT_EVIDENCE REQ-03/06/09/11.
Each report lists the model's recoverable mistakes honestly (e.g. a mistyped comment id, a decision submitted without
`outstanding`, a staging approval cited as primary evidence); the tools/guards rejected them and the model corrected itself.

## What went wrong along the way (and what changed)
- **REQ-09 (rollback rehearsal) was wrong in my first complete run**: RB-401 (success) and RB-402 (failure) are equally authoritative,
  both on the target build, with no supersession, so the manifest rule says INSUFFICIENT_EVIDENCE; the model said FAIL. I added a
  contradiction guard to `submit_requirement_decision` and sharpened the prompt rule; later runs got it right.
- **The model's free-text closing summary contradicted its own decisions** (e.g. "5 FAIL" when 4 were recorded). Totals in the report
  are now computed by code from the recorded decisions, never taken from the model's text.
- **Gemini API details**: function calling needs the `v1beta` API (pinned in `llm.py`, override with `GEMINI_API_VERSION`);
  free-tier model names change and some are retired for new keys (set `LLM_MODEL` if a name returns 404).

## Known limits
- Scope parsing is regex-based and tuned to the dataset's sentence shapes (`release R2.4; commit …; service …; environment …`).
  Unusual phrasing yields `not_stated` (safe: blocks PASS), not a false match.
- The applicability guard cannot judge *semantic* issues (supersession, role authority, open defects). The contradiction guard is a
  keyword heuristic (pass/fail words, "supersede"); both rely on the model plus the manifest rules for the rest. A PASS with
  weak semantics but matching scope would be accepted.
- Heuristic injection detection is keyword-based and can miss novel phrasings or flag benign text; the system prompt is the
  primary defence, the flags are the audit trail.
- A small free model reasons less reliably than a frontier model and is non-deterministic: three runs agreed here, but that is
  evidence, not a guarantee. `jra compare` (the reference key was derived by reading the manifest rules against the dataset, with AI assistance) is a sanity check, not ground truth.
- Jira link direction is lost for symmetric link types (original direction is kept in the `Source links:` description line);
  workflow transitions are best effort (recorded status is always stored in the description); native Jira timestamps are import
  times, so the agent uses recorded event times from comment text only.
- Jira's text index (`text ~`) can lag after a large import; label filters are exact and immediate.
- Tested against one Jira Cloud site (company-managed or team-managed variations in issue types/link types may need
  `JIRA_ISSUE_TYPE` / `JIRA_LINK_TYPE`).

## AI assistance
The brief allows AI coding tools; this is exactly how they were used.

**What the AI assistants did**
- **Claude** (chat assistant): read the brief and dataset, proposed the architecture (a tool-using loop plus code-level guards),
  and wrote the importer, agent loop, tools, report generator and tests. It also made the later changes I asked for after my runs
  exposed problems (the Gemini provider layer, the contradiction guard, code-computed totals, project/issue-type preflight checks).
- **Gemini** (chat assistant): used for setup and troubleshooting questions. Some of its advice was wrong or outdated, so I
  confirmed behaviour by running the code rather than trusting it.
- **Gemini `gemini-3.5-flash-lite`** is also the model that drives the agent at run time (an Anthropic adapter is included but was not used).

**What I did**
- Chose a free model provider (a paid API key was not available) and had the code made provider-neutral.
- Created my own Jira Cloud site and project (key `KAN`), generated the API tokens and configured the environment (not submitted).
- Ran the importer and verification against my Jira, then ran the full assessment three times and scored the runs with `jra compare`.
- Diagnosed real-environment problems from the live runs: wrong project key, Gemini's `v1beta` requirement for function calling,
  retired free-tier model names, and rate-limit / 503 behaviour.
- Found REQ-09 wrong in my first complete run by comparing it with the reference key, had the contradiction guard added, and re-ran
  to confirm the fix. Likewise found that the model's own summary contradicted its decisions, so totals are now computed in code.
- Reviewed and tested the code (read through the importer, tools, guards and agent loop; ran the 46 offline tests and three live runs), then packaged the submission and removed all credentials.
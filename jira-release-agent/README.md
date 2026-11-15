# Jira Release Evidence Agent

A tool-using AI agent that decides whether the requirements of a software release (target **atlas-assistant R2.4**) are
satisfied by evidence recorded **in Jira**, and writes one evidence-backed release-readiness report.

```
dataset.json ──(importer, once)──▶ Jira Cloud project ◀──(live REST reads)── tools ◀── model (tool-use loop) ──▶ report
```

The agent never sees `dataset.json`. It reads Jira through nine tools (manifest, requirements, issues, links, comments,
search, scope check, decision, finish) and the model decides which ones to call and when.

## Where to find each deliverable

| Brief asks for | Location |
|---|---|
| Working source code | `src/jra/` (importer, Jira client, agent loop, tools, guards, report) and `tests/` (46 offline tests) |
| Pinned dependencies | `requirements.txt` (exact versions), `pyproject.toml` |
| Repeatable importer | `src/jra/importer.py`, run with `jra import` / `jra verify` (idempotent; see section 2 below) |
| Setup and execution instructions | this README, sections 1-4; variables listed in `.env.example` |
| Generated release-readiness report | `out/report.md` (human-readable) and `out/report.json`: requirement ids, decisions, explanations, evidence tables with source id, Jira key, comment id and link |
| Investigation trace (tool calls) | `out/trace.md` (readable table) and `out/trace.jsonl` (full events) |
| Execution time, model/token usage | end of `out/report.md` ("Run metrics") and `out/report.json` > `metrics`; all three runs compared in `DESIGN.md` |
| Failure handling | `DESIGN.md` > *Failure handling* (table of failure modes, code and tests); failures of each run are listed at the end of its report |
| Design choices, known limits, AI assistance | `DESIGN.md` |
| Credentials kept out | `.env` is git-ignored and not included; `.env.example` has placeholders only |

## Submitted results

| Where | What |
|---|---|
| `out/` | **The submitted run** (`report.md`, `report.json`, `trace.md`, `trace.jsonl`): identical to `run3/` |
| `run1/`, `run2/`, `run3/` | Three complete runs of the same command on the same Jira data (kept to show repeatability) |

Model: `gemini-3.5-flash-lite` (Google free tier). Result: **BLOCKED**; 4 PASS (REQ-01, 04, 07, 10), 4 FAIL (REQ-02, 05, 08, 12),
4 INSUFFICIENT_EVIDENCE (REQ-03, 06, 09, 11). Per-run time, tool-call and token figures are in each report's *Run metrics*
section and summarised in `DESIGN.md`.

## 1. Setup

Requires Python ≥ 3.10, a Jira Cloud site with a dedicated project, an [Atlassian API token](https://id.atlassian.com/manage-profile/security/api-tokens)
and a model API key. **The default is Google Gemini, which has a free tier (no card):** create a key at
<https://aistudio.google.com/apikey>. Anthropic is supported too (`LLM_PROVIDER=anthropic`) but is paid and optional.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # pinned
pip install -e .                       # provides the `jra` command (or use PYTHONPATH=src python -m jra ...)
cp .env.example .env                   # fill in JIRA_* and GEMINI_API_KEY  (.env is git-ignored)
```

Create an empty Jira project (e.g. key `TYN`, any template that has a `Task` issue type and the default `Relates` link
type; otherwise set `JIRA_ISSUE_TYPE` / `JIRA_LINK_TYPE`). Your API user needs create-issue, add-comment, link and transition permissions.

## 2. Import the dataset (repeatable)

```bash
jra import --project TYN --dry-run     # shows what would happen
jra import --project TYN               # imports 400 issues, 2,059 comments, links
jra verify --project TYN               # re-counts issues / comments / links in Jira against the dataset
```

The importer is **idempotent and resumable**: it finds already-imported issues by their `src-<id>` label, only creates
what is missing, tops up missing comments, and never duplicates links. Rate limits (HTTP 429/5xx) are retried with back-off.

What is preserved in Jira:

| Source data | Jira representation |
|---|---|
| `source_id` | summary prefix `[REC-4076]`, label `src-REC-4076`, description header `Source ID:` |
| dataset scope | label `tyn-release-assessment-v2` on **every** issue |
| kind / release | labels `rr-requirement`, `rr-evidence`, `rr-work`, `rr-manifest`; `rel-R2.4` |
| recorded status | description header `Source recorded status:` and (best effort) a workflow transition |
| links | Jira issue links (type `Relates`) + `Source links:` header keeping the original direction |
| comments | one Jira comment each, **verbatim**, in dataset order, including the `Recorded event <ts> \| actor <name> \| role <role>` line |

Scenario actors/timestamps therefore live in the text; the native Jira author/timestamp is the importer's and is never
presented as the fictional actor (the report prints the *recorded* actor, role and time).

> Jira's text index (`text ~`) can lag a few minutes after a large import. Label filters, which the agent mostly relies on, are immediate.

## 3. Run the assessment

```bash
jra assess --project TYN --scope tyn-release-assessment-v2 --release R2.4 --out out
# optional: --provider gemini|anthropic  --model gemini-3.5-flash-lite
```

**Free-tier rate limits.** Model calls are spaced by `LLM_MIN_INTERVAL_SECONDS` (default 6.5 s ≈ 9 requests/min), and
HTTP 429 / 5xx / network errors are retried (up to 6 times) waiting the delay the API suggests, else exponential
back-off capped at 90 s. A *daily* quota error is not retried: the run ends, the report is marked INCOMPLETE and the
reason is recorded. The agent is told to issue independent tool calls together, which keeps the number of model requests
low (a run is typically a few dozen requests and takes about 3 to 5 minutes on the free tier, longer if the API rate-limits you). If you still see retries, raise
`LLM_MIN_INTERVAL_SECONDS` (e.g. 12). Check your live limits in Google AI Studio.

Outputs in `out/`: `report.md` (the release-readiness report), `report.json`, `trace.md` + `trace.jsonl` (every model turn
and tool call with arguments, duration, result size, errors), plus token usage and wall-clock time inside the report's
*Run metrics* section. Exit code `0` = complete, `2` = incomplete (budget/outage; the report says so).

### Evaluating the result
`jra compare` scores a report against my reference answer key (derived by reading the manifest rules against the dataset, prepared with AI assistance; the agent never sees it). Per
requirement it checks the **decision**, that the **primary evidence** is the right ticket and not a known trap (wrong
commit/service/environment/release), that the **explanation** names the deciding facts, and that FAIL/INSUFFICIENT items state
what is outstanding; plus that the injected "waive REQ-05" comment was reported and the overall verdict. Pass several reports
to measure stability: `jra compare --report out1/report.json out2/report.json out3/report.json`.
It is a sanity check from a manual reading, not ground truth: for any DIFF, read the cited Jira comments yourself.

## 4. Tests (offline, no credentials)

```bash
python -m pytest -q
```

An in-memory fake Jira (same method surface as the real client) and a scripted stand-in for the model exercise the importer,
every tool, the guards, compaction, budgets, outage handling, and the Gemini request/response conversion, throttling and 429 retry logic. They verify the *machinery*, not model quality: the
quality of the real run is what `jra assess` + `jra compare` measure.

## 5. Layout

```
src/jra/
  jira_client.py  Jira Cloud REST v3 client (retries, pagination, ADF)
  importer.py     idempotent import + verify
  parsing.py      deterministic extraction: actor/role/time, release/commit/service/environment/prompt, injection heuristics
  tools.py        the agent's tools + validation guards + overall-verdict logic
  llm.py          provider layer: Gemini + Anthropic adapters, request throttle, 429/5xx retry
  agent.py        bounded tool-use loop, context compaction, failure handling, system prompt
  report.py       report.md / report.json generation (evidence enriched from Jira by code)
  trace.py        investigation trace
  cli.py          import | verify | assess | compare
tests/            fake Jira, unit/importer/tool/agent-loop tests, answer key
data/dataset.json import input (agent never reads it)
```

See `DESIGN.md` for design choices, known limits and AI-assistance notes.
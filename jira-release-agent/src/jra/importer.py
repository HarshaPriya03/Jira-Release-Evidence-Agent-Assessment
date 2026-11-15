"""Repeatable, idempotent importer: dataset.json -> Jira issues, comments, links.

Preservation rules (per the assessment brief):
  * Source identifiers: summary prefix "[REC-4076]" + label "src-REC-4076" + description header.
  * Scenario actor / role / event timestamps: comment text is imported VERBATIM
    ("Recorded event <ts> | actor <name> | role <role>\\n<body>"). Native Jira authors/timestamps
    are the importer's and are never presented as the fictional actors.
  * Relationships: Jira issue links (configurable link type) plus a "Source links" description line
    that keeps the original direction.
  * Recorded status: kept in the description header and, best-effort, applied via workflow transition.
  * Scope: every issue gets the dataset label so the agent can restrict itself to this import.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .parsing import HEADER_SEPARATOR

BULK_SIZE = 50


def load_dataset(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for k in ("dataset_label", "target", "records"):
        if k not in data:
            raise ValueError(f"dataset is missing '{k}'")
    ids = [r["source_id"] for r in data["records"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source_id values in dataset")
    return data


def build_labels(rec: dict, dataset_label: str) -> list[str]:
    return [dataset_label, f"rr-{rec['kind']}", f"rel-{rec['release']}", f"src-{rec['source_id']}"]


def build_description(rec: dict) -> str:
    head = [
        f"Source ID: {rec['source_id']}",
        f"Source kind: {rec['kind']}",
        f"Source release: {rec['release']}",
        f"Source recorded status: {rec['recorded_status']}",
        f"Source links: {', '.join(rec['links']) if rec['links'] else '(none)'}",
    ]
    return "\n".join(head) + HEADER_SEPARATOR + rec["description"]


def build_summary(rec: dict) -> str:
    return f"[{rec['source_id']}] {rec['summary']}"


def run_import(client, dataset: dict, project: str, *, issue_type: str = "Task",
               link_type: str = "Relates", workers: int = 4,
               log: Callable[[str], None] = print, dry_run: bool = False) -> dict[str, int]:
    label = dataset["dataset_label"]
    records = dataset["records"]
    by_id = {r["source_id"]: r for r in records}
    stats = dict(records=len(records), created=0, existing=0, comments_added=0, links_added=0, transitions=0,
                 transitions_unavailable=0)

    if dry_run:
        log(f"[dry-run] would import {len(records)} issues and "
            f"{sum(len(r['comments']) for r in records)} comments into {project} with label {label}")
        return stats

    # 1. discover what is already imported (idempotency / resume)
    existing = client.search(f'project = "{project}" AND labels = "{label}"')
    key_by_src: dict[str, str] = {}
    issue_by_key: dict[str, dict] = {}
    for iss in existing:
        for l in iss["labels"]:
            if l.startswith("src-"):
                key_by_src[l[4:]] = iss["key"]
                issue_by_key[iss["key"]] = iss
    stats["existing"] = len(key_by_src)
    log(f"found {len(key_by_src)} previously imported issues")

    # 2. create missing issues in bulk batches
    todo = [r for r in records if r["source_id"] not in key_by_src]
    new_keys: set[str] = set()
    for i in range(0, len(todo), BULK_SIZE):
        batch = todo[i:i + BULK_SIZE]
        keys = client.create_issues_bulk(project, issue_type, [
            {"summary": build_summary(r), "description": build_description(r), "labels": build_labels(r, label)}
            for r in batch])
        for r, k in zip(batch, keys):
            key_by_src[r["source_id"]] = k
            new_keys.add(k)
        stats["created"] += len(batch)
        log(f"created {stats['created']}/{len(todo)} issues")

    # 3. comments (verbatim, in dataset order, one worker per issue so order is preserved)
    def add_comments(rec: dict) -> int:
        key = key_by_src[rec["source_id"]]
        have = 0 if key in new_keys else client.comment_count(key)
        added = 0
        for text in rec["comments"][have:]:
            client.add_comment(key, text)
            added += 1
        return added

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for n, added in enumerate(ex.map(add_comments, records), 1):
            stats["comments_added"] += added
            if n % 50 == 0 or n == len(records):
                log(f"comments: processed {n}/{len(records)} issues ({stats['comments_added']} added)")

    # 4. links (skip pairs that already exist in either direction)
    present = {(i["key"], l["key"]) for i in issue_by_key.values() for l in i["links"]}
    present |= {(b, a) for a, b in present}
    for rec in records:
        for tgt in rec["links"]:
            if tgt not in by_id:
                log(f"WARNING: {rec['source_id']} links to unknown {tgt}; skipped")
                continue
            a, b = key_by_src[rec["source_id"]], key_by_src[tgt]
            if (a, b) in present:
                continue
            client.link_issues(a, b, link_type)
            present |= {(a, b), (b, a)}
            stats["links_added"] += 1
    log(f"links added: {stats['links_added']}")

    # 5. recorded status via workflow transition (best effort; always also in the description)
    for rec in records:
        key = key_by_src[rec["source_id"]]
        if key not in new_keys or rec["recorded_status"] == "To Do":
            continue
        if client.transition_to(key, rec["recorded_status"]):
            stats["transitions"] += 1
        else:
            stats["transitions_unavailable"] += 1
    if stats["transitions_unavailable"]:
        log(f"NOTE: {stats['transitions_unavailable']} status transitions unavailable in this workflow; "
            "recorded status is still stored in each description.")
    return stats


def verify_import(client, dataset: dict, project: str, log: Callable[[str], None] = print,
                  workers: int = 4) -> bool:
    """Check issue, comment and link counts against the dataset."""
    label = dataset["dataset_label"]
    issues = client.search(f'project = "{project}" AND labels = "{label}"')
    by_src = {l[4:]: i for i in issues for l in i["labels"] if l.startswith("src-")}
    want_ids = {r["source_id"] for r in dataset["records"]}
    ok = set(by_src) == want_ids
    log(f"issues: {len(by_src)}/{len(want_ids)} {'OK' if ok else 'MISMATCH'}")

    recs = {r["source_id"]: r for r in dataset["records"]}
    def count(src: str) -> tuple[str, int]:
        return src, client.comment_count(by_src[src]["key"])
    with ThreadPoolExecutor(max_workers=workers) as ex:
        counts = dict(ex.map(count, [s for s in by_src if s in recs]))
    want_c = sum(len(r["comments"]) for r in recs.values())
    got_c = sum(counts.values())
    bad = [s for s, n in counts.items() if n != len(recs[s]["comments"])]
    comments_ok = not bad and got_c == want_c
    log(f"comments: {got_c}/{want_c} {'OK' if comments_ok else 'MISMATCH' + (' on ' + ', '.join(bad[:10]) if bad else '')}")
    ok &= comments_ok

    want_pairs = {frozenset((a, b)) for a, r in recs.items() for b in r["links"]}
    got_pairs = set()
    for src, i in by_src.items():
        for l in i["links"]:
            other = next((s for s, j in by_src.items() if j["key"] == l["key"]), None)
            if other:
                got_pairs.add(frozenset((src, other)))
    link_ok = want_pairs <= got_pairs
    log(f"links: {len(got_pairs & want_pairs)}/{len(want_pairs)} {'OK' if link_ok else 'MISMATCH'}")
    return ok and link_ok

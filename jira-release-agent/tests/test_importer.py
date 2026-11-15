from jra.importer import run_import


def test_import_counts_and_fidelity(jira, dataset):
    recs = dataset["records"]
    assert len(jira.issues) == 400
    assert sum(len(c) for c in jira.comments.values()) == sum(len(r["comments"]) for r in recs) == 2059
    by_src = {l[4:]: i for i in jira.issues.values() for l in i["labels"] if l.startswith("src-")}
    for r in recs:
        i = by_src[r["source_id"]]
        assert dataset["dataset_label"] in i["labels"]
        assert i["summary"].startswith(f"[{r['source_id']}]")
        assert r["description"] in i["description"]
        assert [c["text"] for c in jira.comments[i["key"]]] == r["comments"]  # verbatim, ordered
    want = {frozenset((r["source_id"], t)) for r in recs for t in r["links"]}
    got = set()
    key_src = {i["key"]: s for s, i in by_src.items()}
    for s, i in by_src.items():
        for l in i["links"]:
            got.add(frozenset((s, key_src[l["key"]])))
    assert want <= got


def test_import_is_idempotent(jira, dataset):
    n_issues, n_comments = len(jira.issues), sum(len(c) for c in jira.comments.values())
    n_links = sum(len(i["links"]) for i in jira.issues.values())
    stats = run_import(jira, dataset, "TYN", log=lambda *_: None)
    assert stats["created"] == 0 and stats["comments_added"] == 0 and stats["links_added"] == 0
    assert len(jira.issues) == n_issues
    assert sum(len(c) for c in jira.comments.values()) == n_comments
    assert sum(len(i["links"]) for i in jira.issues.values()) == n_links


def test_import_resumes_partial_comments(jira, dataset):
    key = next(iter(jira.issues))
    removed = jira.comments[key].pop()
    stats = run_import(jira, dataset, "TYN", log=lambda *_: None)
    assert stats["comments_added"] == 1 and jira.comments[key][-1]["text"] == removed["text"]

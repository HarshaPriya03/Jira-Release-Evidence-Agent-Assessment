"""In-memory stand-in for JiraClient (same method surface). Test-only; the shipped agent uses real Jira."""
from __future__ import annotations

import re


class FakeJira:
    base_url = "https://fake.atlassian.net"

    def __init__(self, project="TYN"):
        self.project = project
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[dict]] = {}
        self.n = 0
        self.cid = 10000
        self.calls: list[str] = []

    # --- read
    def _view(self, i):
        return {k: (list(v) if isinstance(v, list) else v) for k, v in i.items()}

    def search(self, jql, limit=None):
        self.calls.append("search")
        if f'project = "{self.project}"' not in jql:
            return []
        labels = re.findall(r'labels = "([^"]+)"', jql)
        texts = re.findall(r'text ~ "((?:[^"\\]|\\.)*)"', jql)
        out = []
        for i in self.issues.values():
            if not all(l in i["labels"] for l in labels):
                continue
            if texts:
                blob = (i["summary"] + i["description"] + " ".join(c["text"] for c in self.comments[i["key"]])).lower()
                if not all(t.lower() in blob for t in texts):
                    continue
            out.append(self._view(i))
        return out[:limit] if limit else out

    def get_issue(self, key):
        self.calls.append("get_issue")
        if key not in self.issues:
            from jra.jira_client import JiraError
            raise JiraError(f"{key} not found")
        return self._view(self.issues[key])

    def get_comments(self, key):
        self.calls.append("get_comments")
        return [dict(c) for c in self.comments[key]]

    def comment_count(self, key):
        return len(self.comments[key])

    # --- write
    def create_issues_bulk(self, project, issue_type, items):
        keys = []
        for it in items:
            self.n += 1
            key = f"{project}-{self.n}"
            self.issues[key] = {"key": key, "summary": it["summary"], "description": it["description"],
                                "labels": list(it["labels"]), "status": "To Do", "links": []}
            self.comments[key] = []
            keys.append(key)
        return keys

    def add_comment(self, key, text):
        self.cid += 1
        self.comments[key].append({"id": str(self.cid), "text": text, "created": "2026-11-14T00:00:00.000+0000",
                                   "author": "Importer Bot"})
        return str(self.cid)

    def link_issues(self, a, b, link_type):
        sa, sb = self.issues[a]["summary"], self.issues[b]["summary"]
        self.issues[a]["links"].append({"key": b, "type": link_type, "direction": "outward", "summary": sb})
        self.issues[b]["links"].append({"key": a, "type": link_type, "direction": "inward", "summary": sa})

    def transition_to(self, key, status):
        self.issues[key]["status"] = status
        return True

    # --- test helper
    def add_foreign_issue(self, summary="[REC-4076] foreign", labels=("other-dataset",)):
        self.n += 1
        key = f"{self.project}-{self.n}"
        self.issues[key] = {"key": key, "summary": summary, "description": "x", "labels": list(labels),
                            "status": "To Do", "links": []}
        self.comments[key] = []
        return key

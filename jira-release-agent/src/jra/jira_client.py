"""Thin Jira Cloud REST v3 client.

Everything the importer and the agent tools need goes through the small surface
defined here, so tests can substitute an in-memory fake with the same methods.
Normalised shapes returned by this client:

  issue   = {"key", "summary", "description", "labels": [...], "status",
             "links": [{"key", "type", "direction", "summary"}]}
  comment = {"id", "text", "created", "author"}   # created/author are Jira-native (importer's)
"""
from __future__ import annotations

import threading
import time
from typing import Any, Iterable, Sequence

import requests

from .adf import adf_to_text, text_to_adf

ISSUE_FIELDS = ("summary", "description", "labels", "status", "issuelinks")


class JiraError(RuntimeError):
    pass


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, *, timeout: int = 30, max_retries: int = 6):
        self.base_url = base_url.rstrip("/")
        self._auth = (email, api_token)
        self.timeout = timeout
        self.max_retries = max_retries
        self._local = threading.local()

    # ------------------------------------------------------------------ http
    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.auth = self._auth
            s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
            self._local.s = s
        return s

    def _req(self, method: str, path: str, *, params: dict | None = None, body: Any = None,
             ok: Sequence[int] = (200, 201, 204)) -> Any:
        url = f"{self.base_url}/rest/api/3{path}"
        last: str = ""
        for attempt in range(self.max_retries + 1):
            try:
                r = self._session().request(method, url, params=params, json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                last = f"network error: {exc}"
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                try:
                    wait = float(r.headers.get("Retry-After", 2 ** attempt))
                except ValueError:
                    wait = 2 ** attempt
                time.sleep(min(wait, 60))
                continue
            if r.status_code not in ok:
                raise JiraError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:600]}")
            return r.json() if r.content else {}
        raise JiraError(f"{method} {path} failed after {self.max_retries + 1} attempts ({last})")

    # ------------------------------------------------------------ normalisers
    @staticmethod
    def _norm_issue(raw: dict) -> dict:
        f = raw.get("fields", {})
        links = []
        for l in f.get("issuelinks") or []:
            if "outwardIssue" in l:
                o, direction = l["outwardIssue"], "outward"
            elif "inwardIssue" in l:
                o, direction = l["inwardIssue"], "inward"
            else:
                continue
            links.append({
                "key": o["key"],
                "type": (l.get("type") or {}).get("name", ""),
                "direction": direction,
                "summary": (o.get("fields") or {}).get("summary", ""),
            })
        return {
            "key": raw["key"],
            "summary": f.get("summary", ""),
            "description": adf_to_text(f.get("description")),
            "labels": f.get("labels") or [],
            "status": ((f.get("status") or {}).get("name")) or "",
            "links": links,
        }

    # ------------------------------------------------------------------- read
    def myself(self) -> dict:
        return self._req("GET", "/myself")

    def list_projects(self) -> list[dict]:
        data = self._req("GET", "/project/search", params={"maxResults": 100})
        return [{"key": p["key"], "name": p.get("name", ""), "type": p.get("projectTypeKey", "")}
                for p in data.get("values", [])]

    def get_project(self, key: str) -> dict:
        return self._req("GET", f"/project/{key}")

    def search(self, jql: str, limit: int | None = None) -> list[dict]:
        out: list[dict] = []
        token = None
        while True:
            body: dict[str, Any] = {"jql": jql, "fields": list(ISSUE_FIELDS), "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            data = self._req("POST", "/search/jql", body=body)
            out.extend(self._norm_issue(i) for i in data.get("issues", []))
            token = data.get("nextPageToken")
            if not token or data.get("isLast") or (limit and len(out) >= limit):
                break
        return out[:limit] if limit else out

    def get_issue(self, key: str) -> dict:
        return self._norm_issue(self._req("GET", f"/issue/{key}", params={"fields": ",".join(ISSUE_FIELDS)}))

    def get_comments(self, key: str) -> list[dict]:
        out: list[dict] = []
        start = 0
        while True:
            data = self._req("GET", f"/issue/{key}/comment",
                             params={"startAt": start, "maxResults": 100, "orderBy": "created"})
            batch = data.get("comments", [])
            for c in batch:
                out.append({
                    "id": str(c["id"]),
                    "text": adf_to_text(c.get("body")),
                    "created": c.get("created", ""),
                    "author": (c.get("author") or {}).get("displayName", ""),
                })
            start += len(batch)
            if not batch or start >= data.get("total", 0):
                break
        return out

    def comment_count(self, key: str) -> int:
        data = self._req("GET", f"/issue/{key}/comment", params={"startAt": 0, "maxResults": 1})
        return int(data.get("total", 0))

    # ------------------------------------------------------------------ write
    def create_issues_bulk(self, project: str, issue_type: str, items: Iterable[dict]) -> list[str]:
        """items: {"summary", "description", "labels"}; returns Jira keys in input order (max 50)."""
        items = list(items)
        updates = [{"fields": {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": it["summary"][:255],
            "description": text_to_adf(it["description"]),
            "labels": it["labels"],
        }} for it in items]
        data = self._req("POST", "/issue/bulk", body={"issueUpdates": updates})
        if data.get("errors"):
            raise JiraError(f"bulk create reported errors: {data['errors']}")
        keys = [i["key"] for i in data.get("issues", [])]
        if len(keys) != len(items):
            raise JiraError(f"bulk create returned {len(keys)} issues for {len(items)} inputs")
        return keys

    def add_comment(self, key: str, text: str) -> str:
        data = self._req("POST", f"/issue/{key}/comment", body={"body": text_to_adf(text)})
        return str(data["id"])

    def link_issues(self, source_key: str, target_key: str, link_type: str) -> None:
        self._req("POST", "/issueLink", body={
            "type": {"name": link_type},
            "inwardIssue": {"key": source_key},
            "outwardIssue": {"key": target_key},
        })

    def transition_to(self, key: str, status_name: str) -> bool:
        """Best-effort workflow transition to a status by name. Returns False if unavailable."""
        data = self._req("GET", f"/issue/{key}/transitions")
        for t in data.get("transitions", []):
            if (t.get("to") or {}).get("name", "").lower() == status_name.lower():
                self._req("POST", f"/issue/{key}/transitions", body={"transition": {"id": t["id"]}})
                return True
        return False

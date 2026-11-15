"""Deterministic parsing of imported record text.

The *model* decides what evidence means; this module only extracts structured facts
(scenario actor/role/timestamp, release/commit/service/environment/prompt) so that
applicability can be checked mechanically instead of trusted from the model's reading.
"""
from __future__ import annotations

import json
import re
from typing import Any

SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")
JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")

# ----------------------------------------------------------------- comments
_EVENT = re.compile(
    r"^Recorded event (?P<ts>\S+) \| actor (?P<actor>.+?) \| role (?P<role>.+?)\n(?P<body>.*)$", re.S
)


def parse_event(text: str) -> dict[str, Any]:
    """Split 'Recorded event <ts> | actor <name> | role <role>\\n<body>'."""
    m = _EVENT.match(text.strip())
    if not m:
        return {"recorded_at": None, "actor": None, "role": None, "body": text.strip()}
    return {"recorded_at": m["ts"], "actor": m["actor"], "role": m["role"], "body": m["body"].strip()}


_FIELD_RE = {
    "release": re.compile(r"\brelease (R\d+(?:\.\d+)*)", re.I),
    "commit": re.compile(r"\bcommit ([0-9a-f]{7,40})\b", re.I),
    "service": re.compile(r"\bservice ([a-z][a-z0-9_-]*)", re.I),
    "environment": re.compile(r"\benvironment ([a-z][a-z0-9_-]*)", re.I),
    "prompt": re.compile(r"\bprompt(?: bundle)? (p\d+)\b", re.I),
}
_APPROVAL_SCOPE = re.compile(r"\bApproval scope: ([a-z][a-z0-9_-]*)", re.I)
_FOR_ENV = re.compile(r"\bfor (production|staging|development)\b", re.I)
_RECORD_IDS = re.compile(r"\b[A-Z]{2,10}(?:-[A-Z]{2,6})?-\d{1,5}\b")


def parse_scope(body: str) -> dict[str, Any]:
    """Extract the build/deployment scope a record text claims to apply to."""
    out: dict[str, Any] = {}
    for name, rx in _FIELD_RE.items():
        m = rx.search(body)
        if m:
            out[name] = m.group(1)
    if "environment" not in out:
        m = _APPROVAL_SCOPE.search(body) or _FOR_ENV.search(body)
        if m:
            out["environment"] = m.group(1)
    out["record_ids"] = sorted(set(_RECORD_IDS.findall(body)))
    return out


def _same(dim: str, a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    if dim == "commit":  # short vs long SHA
        return a.startswith(b) or b.startswith(a)
    return a == b


def compare_scope(scope: dict[str, Any], target: dict[str, str], require_prompt: bool) -> dict[str, Any]:
    dims = ["release", "commit", "service", "environment"] + (["prompt"] if require_prompt else [])
    status = {}
    for d in dims:
        v = scope.get(d)
        status[d] = "not_stated" if v is None else ("match" if _same(d, v, str(target.get(d, ""))) else "mismatch")
    mismatches = [f"{d}: record says {scope[d]!r}, target is {target.get(d)!r}" for d, s in status.items() if s == "mismatch"]
    missing = [d for d, s in status.items() if s == "not_stated"]
    return {
        "per_dimension": status,
        "mismatches": mismatches,
        "not_stated": missing,
        "applicable": not mismatches and not missing,
    }


# ------------------------------------------------------- outcome polarity
_FAILW = re.compile(r"\bfail(?:s|ed|ure|ures)?\b|\blost\b|\bunredacted\b|\bduplicated\b|\bdrops?\b|\bexceed|\breopen", re.I)
_PASSW = re.compile(r"\bpass(?:es|ed)?\b|\bsucce(?:ss|ssful|eded|ed)\b|\brestored\b", re.I)
_SUPERSEDE = re.compile(r"\bsupersede", re.I)


def outcome_polarity(text: str) -> str | None:
    """Very coarse: 'fail' if the record reports a failure, 'pass' if it reports success, else None."""
    t = re.sub(r"\bno data loss\b", "", text, flags=re.I)
    if _FAILW.search(t):
        return "fail"
    return "pass" if _PASSW.search(t) else None


def mentions_supersession(text: str) -> bool:
    return bool(_SUPERSEDE.search(text))


# ------------------------------------------------- prompt-injection heuristics
_INJECTION = [re.compile(p, re.I) for p in (
    r"\bautomated (release )?(review|assess)",
    r"\bignore (all |any |the |previous |prior |your )",
    r"\bdo not (mention|report|disclose|reveal)",
    r"\b(report|mark|record|treat|set)\b.{0,50}\bas (PASS|FAIL|READY|passed|approved)\b",
    r"\bwaived\b",
    r"\bsystem prompt\b",
    r"\byou (must|should) (now )?(report|mark|approve|ignore)",
    r"\bnote for .{0,40}(agent|reviewer|assistant|model|\bAI\b)",
)]


def looks_like_instruction(text: str) -> bool:
    """True if record text appears to address the *reviewer* instead of recording facts."""
    return any(rx.search(text) for rx in _INJECTION)


# ------------------------------------------------------------------ records
_SRC_PREFIX = re.compile(r"^\[([^\]]+)\]\s*")
HEADER_SEPARATOR = "\n---\n"


def strip_source_prefix(summary: str) -> tuple[str | None, str]:
    m = _SRC_PREFIX.match(summary)
    return (m.group(1), summary[m.end():]) if m else (None, summary)


def label_meta(labels: list[str]) -> dict[str, str | None]:
    meta: dict[str, str | None] = {"source_id": None, "kind": None, "release": None}
    for l in labels:
        if l.startswith("src-"):
            meta["source_id"] = l[4:]
        elif l.startswith("rr-"):
            meta["kind"] = l[3:]
        elif l.startswith("rel-"):
            meta["release"] = l[4:]
    return meta


def split_description(desc: str) -> tuple[dict[str, str], str]:
    """Importer writes a 'Source ...' header, a '---' line, then the original description."""
    if HEADER_SEPARATOR in desc:
        head, body = desc.split(HEADER_SEPARATOR, 1)
    else:
        head, body = "", desc
    fields = {}
    for line in head.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields, body.strip()


def parse_target(text: str) -> dict[str, str] | None:
    m = re.search(r"Target:\s*(\{.*?\})", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def parse_requirement(body: str) -> dict[str, Any]:
    rid = re.search(r"Requirement ID:\s*(\S+)", body)
    mand = re.search(r"Mandatory:\s*(\w+)", body)
    crit = re.search(r"Acceptance criterion:\s*(.+)", body)
    criterion = crit.group(1).strip() if crit else ""
    return {
        "requirement_id": rid.group(1) if rid else None,
        "mandatory": (mand.group(1).lower() == "yes") if mand else True,
        "acceptance_criterion": criterion,
        "needs_prompt": "prompt" in criterion.lower(),
        "target": parse_target(body),
    }

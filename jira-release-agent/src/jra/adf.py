"""Minimal Atlassian Document Format helpers (plain text <-> ADF)."""
from __future__ import annotations

from typing import Any


def text_to_adf(text: str) -> dict[str, Any]:
    """One paragraph per non-empty line. Text is preserved verbatim (no markup parsing)."""
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        for line in text.split("\n")
        if line.strip()
    ]
    if not paragraphs:
        paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": "-"}]}]
    return {"type": "doc", "version": 1, "content": paragraphs}


_BLOCKS = {"paragraph", "heading", "listItem", "blockquote", "codeBlock", "tableRow", "rule"}


def adf_to_text(node: Any) -> str:
    """Flatten an ADF document (or a plain string) to text, one line per block."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node

    def walk(n: Any) -> str:
        if isinstance(n, list):
            return "".join(walk(c) for c in n)
        if not isinstance(n, dict):
            return ""
        t = n.get("type")
        if t == "text":
            return n.get("text", "")
        if t == "hardBreak":
            return "\n"
        if t in ("mention", "emoji", "inlineCard"):
            attrs = n.get("attrs", {})
            return attrs.get("text") or attrs.get("shortName") or attrs.get("url", "")
        inner = walk(n.get("content", []))
        return inner + "\n" if t in _BLOCKS else inner

    return walk(node).rstrip("\n")

"""Investigation trace: JSONL events plus a human-readable Markdown rendering."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Trace:
    def __init__(self, path: Path | None = None):
        self.events: list[dict[str, Any]] = []
        self.path = path
        self._t0 = time.monotonic()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def add(self, event: str, **data: Any) -> None:
        rec = {"t": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "elapsed_s": round(time.monotonic() - self._t0, 2), "event": event, **data}
        self.events.append(rec)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def to_markdown(self) -> str:
        lines = ["# Investigation trace", "",
                 "| # | t (s) | event | detail |", "|---|-------|-------|--------|"]
        n = 0
        for e in self.events:
            if e["event"] == "tool_call":
                n += 1
                args = json.dumps(e["input"], ensure_ascii=False)
                status = "ERROR " if e["is_error"] else ""
                detail = f"`{e['name']}({args[:160]})` → {status}{e['result_chars']} chars, {e['duration_ms']} ms"
                if e.get("note"):
                    detail += f" — {e['note']}"
                lines.append(f"| {n} | {e['elapsed_s']} | tool | {detail.replace('|', '/')} |")
            elif e["event"] == "model_call":
                u = e.get("usage", {})
                lines.append(f"| | {e['elapsed_s']} | model turn {e['turn']} | stop={e.get('stop_reason')} "
                             f"in={u.get('input_tokens', 0)} out={u.get('output_tokens', 0)} "
                             f"cache_read={u.get('cache_read_input_tokens', 0)} ({e['latency_ms']} ms) |")
            elif e["event"] == "model_text" and e.get("text"):
                lines.append(f"| | {e['elapsed_s']} | model note | {e['text'][:300].replace('|', '/').replace(chr(10), ' ')} |")
            else:
                lines.append(f"| | {e['elapsed_s']} | {e['event']} | "
                             f"{json.dumps({k: v for k, v in e.items() if k not in ('t', 'elapsed_s', 'event')}, ensure_ascii=False)[:300].replace('|', '/')} |")
        return "\n".join(lines) + "\n"

"""Environment-driven configuration. No credentials are ever stored in code."""
from __future__ import annotations

import os
from dataclasses import dataclass

try:  # .env is optional
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Limits:
    """Hard execution bounds for one assessment run."""

    max_turns: int = 100
    max_tool_calls: int = 200
    max_wall_seconds: int = 2400
    max_total_tokens: int = 1_500_000
    tool_result_chars: int = 7000
    max_response_tokens: int = 4096

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(
            max_turns=_int("JRA_MAX_TURNS", 100),
            max_tool_calls=_int("JRA_MAX_TOOL_CALLS", 200),
            max_wall_seconds=_int("JRA_MAX_WALL_SECONDS", 2400),
            max_total_tokens=_int("JRA_MAX_TOTAL_TOKENS", 1_500_000),
            tool_result_chars=_int("JRA_TOOL_RESULT_CHARS", 7000),
        )


@dataclass(frozen=True)
class JiraSettings:
    base_url: str
    email: str
    api_token: str
    issue_type: str = "Task"
    link_type: str = "Relates"

    @classmethod
    def from_env(cls) -> "JiraSettings":
        missing = [k for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN") if not os.getenv(k)]
        if missing:
            raise SystemExit(f"Missing environment variables: {', '.join(missing)} (see .env.example)")
        return cls(
            base_url=os.environ["JIRA_BASE_URL"].rstrip("/"),
            email=os.environ["JIRA_EMAIL"],
            api_token=os.environ["JIRA_API_TOKEN"],
            issue_type=os.getenv("JIRA_ISSUE_TYPE", "Task"),
            link_type=os.getenv("JIRA_LINK_TYPE", "Relates"),
        )


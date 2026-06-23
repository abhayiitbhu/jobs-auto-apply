"""Track skipped jobs and unanswered questions during a run for end-of-run prompts."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunIssue:
    source: str
    title: str
    company: str
    url: str
    reason: str
    questions: list[str] = field(default_factory=list)


_issues: list[RunIssue] = []


def clear_run_issues() -> None:
    _issues.clear()


def record_skip(
    *,
    source: str,
    title: str,
    company: str = "",
    url: str = "",
    reason: str,
    questions: list[str] | None = None,
) -> None:
    _issues.append(
        RunIssue(
            source=source,
            title=title,
            company=company,
            url=url,
            reason=reason,
            questions=list(questions or []),
        )
    )


def run_issues() -> list[RunIssue]:
    return list(_issues)


def run_issue_count() -> int:
    return len(_issues)


def run_issues_summary() -> str:
    if not _issues:
        return ""
    lines = [f"{len(_issues)} job(s) were skipped this run:"]
    for issue in _issues[:12]:
        where = f"{issue.title}"
        if issue.company:
            where += f" @ {issue.company}"
        q_part = ""
        if issue.questions:
            q_part = f" — {issue.questions[0][:70]}"
        lines.append(f"  • [{issue.source}] {where} ({issue.reason}){q_part}")
    if len(_issues) > 12:
        lines.append(f"  … and {len(_issues) - 12} more")
    return "\n".join(lines)

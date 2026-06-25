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
_run_attempted_keys: set[str] = set()


def clear_run_issues() -> None:
    _issues.clear()
    _run_attempted_keys.clear()


def record_run_attempt(job_key: str) -> None:
    """Mark a job as attempted this run (applied, skipped, or failed)."""
    if job_key:
        _run_attempted_keys.add(job_key)


def run_attempted_job_keys() -> set[str]:
    return set(_run_attempted_keys)


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

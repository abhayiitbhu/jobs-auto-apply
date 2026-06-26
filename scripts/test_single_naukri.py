"""Apply to Naukri listings for targeted testing / diagnosis.

Usage:
  python scripts/test_single_naukri.py                # diagnose every Naukri
                                                      # entry in technical_failures.json
  python scripts/test_single_naukri.py URL [URL ...]  # diagnose specific listing(s)
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Running this file directly puts scripts/ on sys.path, not the repo root, so the
# package import below fails. Add the repo root explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contextlib

from jobs_auto_apply.browser import naukri_session
from jobs_auto_apply.config import load_config
from jobs_auto_apply.naukri.apply import apply_to_job
from jobs_auto_apply.technical_failures import (
    clear_technical_failures_for_job,
    matching_failures,
)
from jobs_auto_apply.utils import JobListing, setup_logging


def _job_id_from_url(url: str) -> str:
    """Naukri detail URLs end with a numeric id (optionally followed by ?query)."""
    m = re.search(r"(\d{6,})(?:\?|$)", url)
    return m.group(1) if m else url


def _load_failure_jobs(base_dir: Path) -> list[JobListing]:
    path = base_dir / "data" / "technical_failures.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    jobs: list[JobListing] = []
    seen: set[str] = set()
    for entry in (data.get("failures") or {}).values():
        if entry.get("source") != "naukri":
            continue
        url = (entry.get("url") or "").strip()
        if not url or "job-listings" not in url:
            continue
        jid = _job_id_from_url(url)
        if jid in seen:
            continue
        seen.add(jid)
        jobs.append(
            JobListing(
                job_id=jid,
                title=entry.get("title") or "Naukri job",
                company=entry.get("company") or "",
                url=url,
                source="naukri",
                easy_apply=True,
            )
        )
    return jobs


def _jobs_from_urls(urls: list[str]) -> list[JobListing]:
    return [
        JobListing(
            job_id=_job_id_from_url(u),
            title="Naukri job",
            company="",
            url=u,
            source="naukri",
            easy_apply=True,
        )
        for u in urls
    ]


async def main() -> None:
    config = load_config(Path("config.yaml"))
    setup_logging(config.base_dir / "data" / "test_single.log", verbose=True)

    urls = [a for a in sys.argv[1:] if a.startswith("http")]
    jobs = _jobs_from_urls(urls) if urls else _load_failure_jobs(config.base_dir)

    if not jobs:
        print("No Naukri jobs to diagnose (no URLs given and none in technical_failures.json).")
        return

    print(f"Diagnosing {len(jobs)} Naukri listing(s)...")

    async with naukri_session(config) as (_browser, context, _page):
        for idx, job in enumerate(jobs, 1):
            print("\n" + "=" * 70)
            print(f"==== [{idx}/{len(jobs)}] {job.job_id} — {job.title} @ {job.company} ====")
            print(f"==== {job.url} ====")
            print("=" * 70)
            page = await context.new_page()
            started = datetime.now(timezone.utc).isoformat()
            try:
                result = await apply_to_job(page, context, job, config)
                print(f"\n==== APPLY RESULT [{job.job_id}]: {result!r} ====")
                _resolve_failure(config.base_dir, job, result, started)
            except Exception as exc:
                print(f"\n==== ERROR [{job.job_id}]: {type(exc).__name__}: {exc} ====")
            finally:
                with contextlib.suppress(Exception):
                    await page.close()


def _resolve_failure(base_dir: Path, job: JobListing, result, started: str) -> None:
    """Drop the job from technical_failures.json once it no longer fails.

    ``apply_to_job`` returns ``True`` when applied, ``None`` when skipped /
    already-applied (but it also re-records a fresh failure for the "could not
    fill" skip), and ``False`` on a technical failure. Treat the job as resolved
    when it applied, or when it was skipped without a new failure being logged
    during this test.
    """
    matches = matching_failures(base_dir, source="naukri", url=job.url, job_id=job.job_id)
    if not matches:
        return
    fresh_failure = any(str(e.get("last_seen", "")) >= started for e in matches.values())
    resolved = result is True or (result is None and not fresh_failure)
    if not resolved:
        print(f"==== STILL FAILING [{job.job_id}]: kept in technical_failures.json ====")
        return
    removed = clear_technical_failures_for_job(base_dir, source="naukri", url=job.url, job_id=job.job_id)
    if removed:
        print(f"==== RESOLVED [{job.job_id}]: removed from technical_failures.json ({', '.join(removed)}) ====")


if __name__ == "__main__":
    asyncio.run(main())

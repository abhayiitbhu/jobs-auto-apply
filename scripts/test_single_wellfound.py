"""Apply to Wellfound listings for targeted testing / diagnosis.

Usage:
  python scripts/test_single_wellfound.py                # diagnose every Wellfound
                                                         # entry in technical_failures.json
  python scripts/test_single_wellfound.py URL [URL ...]  # diagnose specific listing(s)

Honors `application.dry_run` in config.yaml: set it to true to walk the whole
flow (open modal, fill cover note / questions) without actually submitting.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Running this file directly puts scripts/ on sys.path, not the repo root, so the
# package import below fails. Add the repo root explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contextlib

from jobs_auto_apply.browser import wellfound_session
from jobs_auto_apply.config import load_config
from jobs_auto_apply.technical_failures import (
    clear_technical_failures_for_job,
    matching_failures,
)
from jobs_auto_apply.utils import JobListing, setup_logging
from jobs_auto_apply.wellfound.apply import process_wellfound_job
from jobs_auto_apply.wellfound.guard import (
    WellfoundAccessRestrictedError,
    WellfoundApplicationLimitReached,
)
from jobs_auto_apply.wellfound.search import _job_id_from_url, is_wellfound_job_url


def _load_failure_jobs(base_dir: Path) -> list[JobListing]:
    path = base_dir / "data" / "technical_failures.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    jobs: list[JobListing] = []
    seen: set[str] = set()
    for entry in (data.get("failures") or {}).values():
        if entry.get("source") != "wellfound":
            continue
        url = (entry.get("url") or "").strip()
        if not url or not is_wellfound_job_url(url):
            continue
        jid = _job_id_from_url(url)
        if jid in seen:
            continue
        seen.add(jid)
        jobs.append(
            JobListing(
                job_id=jid,
                title=entry.get("title") or "Wellfound job",
                company=entry.get("company") or "",
                url=url,
                source="wellfound",
                easy_apply=True,
            )
        )
    return jobs


def _jobs_from_urls(urls: list[str]) -> list[JobListing]:
    return [
        JobListing(
            job_id=_job_id_from_url(u),
            title="Wellfound job",
            company="",
            url=u,
            source="wellfound",
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
        print("No Wellfound jobs to diagnose (no URLs given and none in technical_failures.json).")
        return

    print(f"Diagnosing {len(jobs)} Wellfound listing(s)...")
    if config.application.dry_run:
        print("(dry_run=true — will walk the flow but not submit.)")

    async with wellfound_session(config) as (_browser, context, _page):
        for idx, job in enumerate(jobs, 1):
            print("\n" + "=" * 70)
            print(f"==== [{idx}/{len(jobs)}] {job.job_id} — {job.title} @ {job.company} ====")
            print(f"==== {job.url} ====")
            print("=" * 70)
            page = await context.new_page()
            started = datetime.now(timezone.utc).isoformat()
            try:
                result = await process_wellfound_job(page, context, job, config)
                print(f"\n==== APPLY RESULT [{job.job_id}]: {result!r} ====")
                _resolve_failure(config.base_dir, job, result, started)
            except WellfoundApplicationLimitReached as exc:
                print(f"\n==== LIMIT REACHED [{job.job_id}]: {exc} ====")
            except WellfoundAccessRestrictedError as exc:
                print(f"\n==== ACCESS RESTRICTED [{job.job_id}]: {exc} ====")
            except Exception as exc:
                print(f"\n==== ERROR [{job.job_id}]: {type(exc).__name__}: {exc} ====")
            finally:
                with contextlib.suppress(Exception):
                    await page.close()


def _resolve_failure(base_dir: Path, job: JobListing, result, started: str) -> None:
    """Drop the job from technical_failures.json once it no longer fails.

    ``process_wellfound_job`` returns ``True`` when applied, ``None`` when
    skipped / already-applied, and ``False`` on a technical failure. Treat the
    job as resolved when it applied, or when it was skipped without a new failure
    being logged during this test.
    """
    matches = matching_failures(base_dir, source="wellfound", url=job.url, job_id=job.job_id)
    if not matches:
        return
    fresh_failure = any(str(e.get("last_seen", "")) >= started for e in matches.values())
    resolved = result is True or (result is None and not fresh_failure)
    if not resolved:
        print(f"==== STILL FAILING [{job.job_id}]: kept in technical_failures.json ====")
        return
    removed = clear_technical_failures_for_job(base_dir, source="wellfound", url=job.url, job_id=job.job_id)
    if removed:
        print(f"==== RESOLVED [{job.job_id}]: removed from technical_failures.json ({', '.join(removed)}) ====")


if __name__ == "__main__":
    asyncio.run(main())

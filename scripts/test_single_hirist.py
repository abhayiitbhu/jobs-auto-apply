"""Diagnose / test a single Hirist screening form.

Usage:
    python scripts/test_single_hirist.py                # diagnose every Hirist
                                                        # entry in technical_failures.json
    python scripts/test_single_hirist.py URL [URL ...]  # diagnose specific listing(s)

Safe by design: it discovers questions, prints per-question DOM diagnostics, runs
the real fill logic, and reports which fields could not be filled — but it never
clicks the final Next/Submit, so no application is actually sent.
"""

import asyncio
import json
import sys
from pathlib import Path

# Running this file directly puts scripts/ on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs_auto_apply.browser import hirist_session
from jobs_auto_apply.config import load_config
from jobs_auto_apply.hirist.apply import (
    _already_applied,
    _click_apply,
    _wait_for_apply_button,
    goto_hirist_job_detail,
)
from jobs_auto_apply.hirist.questions import (
    _FORM_STATE_JS,
    _locate_hirist_question_box,
    discover_hirist_questions,
    fill_hirist_questions,
)
from jobs_auto_apply.application_questions import resolve_question_answers
from jobs_auto_apply.page_load import prepare_interactive_page
from jobs_auto_apply.utils import JobListing, setup_logging

def _load_failure_urls(base_dir: Path) -> list[str]:
    """Hirist screening URLs from data/technical_failures.json (source=hirist)."""
    path = base_dir / "data" / "technical_failures.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    urls: list[str] = []
    seen: set[str] = set()
    for entry in (data.get("failures") or {}).values():
        if entry.get("source") != "hirist":
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


async def _diagnose_question(page, label: str) -> None:
    print(f"\n  Q: {label[:80]!r}")
    box = await _locate_hirist_question_box(page, label)
    if box is None:
        print("    box: NOT FOUND by _locate_hirist_question_box")
        try:
            txt = page.get_by_text(label[:35], exact=False)
            print(f"    label text occurrences on page: {await txt.count()}")
        except Exception as exc:
            print(f"    (text probe failed: {exc})")
        return
    try:
        cls = await box.evaluate("el => el.className")
        radios = await box.locator("input[type=radio]").count()
        checks = await box.locator("input[type=checkbox]").count()
        textareas = await box.locator("textarea").count()
        inputs = await box.locator(
            "input[type=text], input[type=number], input:not([type])"
        ).count()
        visible = await box.first.is_visible()
        html = await box.evaluate("el => el.outerHTML.slice(0, 500)")
        print(
            f"    box class={cls!r} visible={visible} "
            f"radios={radios} checks={checks} textareas={textareas} inputs={inputs}"
        )
        print(f"    html: {' '.join(html.split())[:480]}")
    except Exception as exc:
        print(f"    (box probe failed: {exc})")


def _job_id_from_url(url: str) -> str:
    parts = url.rstrip("/").split("/")
    return parts[-2] if url.rstrip("/").endswith("screening") else parts[-1]


async def _diagnose_url(config, context, url: str) -> None:
    print("\n" + "=" * 70)
    print(f"==== JOB: {url} ====")
    print("=" * 70)

    page = await context.new_page()
    try:
        await _diagnose_on_page(config, page, url)
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _diagnose_on_page(config, page, url: str) -> None:

    job = JobListing(
        job_id=_job_id_from_url(url),
        title="Hirist test job",
        company="",
        url=url,
        source="hirist",
        easy_apply=True,
    )

    await goto_hirist_job_detail(page, url)
    if await _already_applied(page):
        print("==== Already applied — nothing to test ====")
        return

    # Open the screening form if we're on the job detail page.
    try:
        await page.wait_for_selector(
            "text=/Mandatory Question|tell the recruiter more about yourself/i",
            timeout=4000,
        )
        on_form = True
    except Exception:
        on_form = False
    if not on_form:
        if await _wait_for_apply_button(page):
            await _click_apply(page)
        try:
            await page.wait_for_selector(
                "text=/Mandatory Question|tell the recruiter more about yourself/i",
                timeout=10_000,
            )
        except Exception:
            print("==== Could not reach the screening form ====")

    await prepare_interactive_page(page, fast=True)
    questions = await discover_hirist_questions(page, prepped=True)
    print(f"\n==== Discovered {len(questions)} question(s) ====")

    print("\n==== Per-question DOM diagnostics ====")
    for q in questions:
        await _diagnose_question(page, str(q.get("label", "")))

    answers = await resolve_question_answers(
        config, job, "", questions, interactive=False, defer_new=True
    )
    print("\n==== Resolved answers ====")
    for q in questions:
        label = str(q.get("label", ""))
        print(f"  {label[:60]!r} -> {str(answers.get(label, ''))[:60]!r}")

    unfilled = await fill_hirist_questions(
        page, questions, answers, prep=False, config=config
    )
    print(f"\n==== fill_hirist_questions unfilled: {unfilled} ====")

    state = await page.evaluate(_FORM_STATE_JS) or {}
    print("\n==== Form state ====")
    print(f"  nextDisabled={state.get('nextDisabled')}")
    print(f"  empty={state.get('empty')}")
    for f in (state.get("fields") or [])[:14]:
        print(
            f"  - {str(f.get('label',''))[:45]!r} "
            f"domValue={str(f.get('domValue',''))[:25]!r}"
        )

    print("\n(Not clicking Next/Submit — diagnostic only.)")
    await page.wait_for_timeout(1000)


async def main() -> None:
    config = load_config(Path("config.yaml"))
    setup_logging(config.base_dir / "data" / "test_hirist.log", verbose=True)

    cli_urls = [a for a in sys.argv[1:] if a.startswith("http")]
    urls = cli_urls or _load_failure_urls(config.base_dir)
    if not urls:
        print(
            "No Hirist jobs to diagnose "
            "(no URLs given and no source=hirist entries in technical_failures.json)."
        )
        return

    print(f"Diagnosing {len(urls)} Hirist screening form(s)...")

    async with hirist_session(config) as (_browser, context, _page):
        for url in urls:
            try:
                await _diagnose_url(config, context, url)
            except Exception as exc:
                print(f"\n==== ERROR while testing {url}: {type(exc).__name__}: {exc} ====")


if __name__ == "__main__":
    asyncio.run(main())

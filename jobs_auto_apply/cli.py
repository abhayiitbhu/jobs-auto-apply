from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .application_questions import clear_draft_answer_cache
from .apply_filters import filter_pending_jobs
from .browser import (
    PARALLEL_COOKIE_PLATFORMS,
    hirist_session,
    instahyre_session,
    naukri_session,
    uplers_session,
    wellfound_session,
)
from .chrome_profile import list_chrome_profiles, resolve_chrome_profile_dir
from .config import (
    AppConfig,
    HiristFiltersConfig,
    InstahyreFiltersConfig,
    NaukriFiltersConfig,
    UplersFiltersConfig,
    WellfoundFiltersConfig,
    load_config,
)
from .hirist.apply import apply_batch as hirist_apply_batch
from .hirist.search import apply_filters as hirist_apply_filters
from .hirist.search import collect_from_search_urls as hirist_collect_from_search_urls
from .hirist.search import collect_job_listings as hirist_collect_jobs
from .hirist.search import iter_paginated_feed_pages
from .instahyre.apply import apply_batch as instahyre_apply_batch
from .instahyre.apply import apply_from_feeds as instahyre_apply_from_feeds
from .instahyre.search import apply_filters as instahyre_apply_filters
from .instahyre.search import collect_from_search_urls as instahyre_collect_from_search_urls
from .instahyre.search import collect_job_listings as instahyre_collect_jobs
from .limits import apply_cap, is_unlimited, scrape_limit
from .memory import get_decision, load_memory, record_decision, save_preferences
from .naukri.apply import apply_batch as naukri_apply_batch
from .naukri.pipeline import run_naukri_pipeline
from .naukri.resume_sync import (
    run_naukri_resume_sync_scheduler,
    sync_naukri_resume_if_due,
)
from .naukri.search import apply_filters as naukri_apply_filters
from .naukri.search import collect_job_listings as naukri_collect_jobs
from .naukri.search import collect_naukri_srp_batch, scroll_naukri_srp_more
from .naukri.search import go_to_search_page as naukri_go_to_search_page
from .pending_questions import (
    answer_pending_groups_interactive,
    answer_pending_groups_via_messenger,
    pending_count,
    review_saved_answers_interactive,
    saved_answers_needing_review_count,
    summary_for_run,
)
from .pending_retry import retry_pending_jobs
from .review import (
    ReviewItem,
    approved_items,
    build_review_payload,
    enrich_jobs_parallel,
    items_from_payload,
    load_review_queue,
    needs_re_enrich,
    refresh_cover_letters,
    repair_review_queue_items,
    review_summary,
    save_review_queue,
)
from .role_filter import auto_reject_skipped_roles, filter_skipped_roles, should_skip_role
from .run_issues import (
    clear_run_issues,
    run_issue_count,
    run_issues_summary,
)
from .uplers.apply import apply_batch as uplers_apply_batch
from .uplers.search import apply_filters as uplers_apply_filters
from .uplers.search import collect_job_listings as uplers_collect_jobs
from .utils import (
    JobListing,
    clear_deferred_applies,
    company_key,
    filter_skipped_companies,
    job_key,
    load_applied_jobs,
    reconcile_applied_jobs,
    setup_logging,
    should_skip_company,
)
from .wellfound.apply import apply_batch as wellfound_apply_batch
from .wellfound.apply import ensure_resume_on_profile
from .wellfound.pipeline import run_wellfound_pipeline
from .wellfound.search import apply_filters as wellfound_apply_filters
from .wellfound.search import collect_job_listings as wellfound_collect_jobs

logger = logging.getLogger("job_apply")
console = Console()

PLATFORM_CHOICES = (
    "wellfound",
    "uplers",
    "naukri",
    "hirist",
    "instahyre",
    "all",
)

PLATFORM_APPLY = {
    "wellfound": wellfound_apply_batch,
    "uplers": uplers_apply_batch,
    "naukri": naukri_apply_batch,
    "hirist": hirist_apply_batch,
    "instahyre": instahyre_apply_batch,
}


@click.group()
def main() -> None:
    """Auto-apply to jobs on Wellfound, Uplers, Naukri, Hirist, and Instahyre."""


def _enabled_platforms(config: AppConfig, platform: str) -> list[str]:
    mapping = {
        "wellfound": config.wellfound.enabled,
        "uplers": config.uplers.enabled,
        "naukri": config.naukri.enabled,
        "hirist": config.hirist.enabled,
        "instahyre": config.instahyre.enabled,
    }
    if platform == "all":
        return [name for name, enabled in mapping.items() if enabled]
    if not mapping.get(platform):
        return []
    return [platform]


def _review_dir(config: AppConfig) -> str:
    return config.application.review_dir


async def _run_single_platform(name: str, runner, config: AppConfig) -> int:
    applied_ids = load_applied_jobs(config.applied_jobs_path)
    console.print(f"[bold]Starting {name.capitalize()}...[/bold]")
    try:
        return int(await runner(config, applied_ids))
    except Exception as exc:
        console.print(f"[red]{name.capitalize()} failed: {exc}[/red]")
        return 0


async def _run_platform_batch(config: AppConfig, targets: list[str]) -> int:
    runners = [
        ("wellfound", _run_wellfound),
        ("uplers", _run_uplers),
        ("naukri", _run_naukri),
        ("hirist", _run_hirist),
        ("instahyre", _run_instahyre),
    ]
    selected = [(name, runner) for name, runner in runners if name in targets]
    if not selected:
        return 0

    if not config.application.parallel_platforms:
        total = 0
        for name, runner in selected:
            total += await _run_single_platform(name, runner, config)
        return total

    parallel = [(name, runner) for name, runner in selected if name in PARALLEL_COOKIE_PLATFORMS]
    sequential = [(name, runner) for name, runner in selected if name not in PARALLEL_COOKIE_PLATFORMS]
    total = 0

    if len(parallel) > 1:
        names = ", ".join(name for name, _ in parallel)
        console.print(
            f"[cyan]Running {len(parallel)} platforms in parallel: {names}[/cyan]\n"
            "[dim]Each uses its own browser + cookies (naukri/hirist/instahyre apply workers still apply per site).[/dim]\n"
        )
        results = await asyncio.gather(*[_run_single_platform(name, runner, config) for name, runner in parallel])
        total += sum(results)
    elif parallel:
        total += await _run_single_platform(parallel[0][0], parallel[0][1], config)

    for name, runner in sequential:
        total += await _run_single_platform(name, runner, config)
    return total


@main.command("run")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
@click.option("--platform", type=click.Choice(PLATFORM_CHOICES), default="all")
@click.option("--verbose", is_flag=True)
def run_cmd(config_path: Path, platform: str, verbose: bool) -> None:
    """Search, filter, and apply automatically (or apply approved queue if require_review is true)."""
    asyncio.run(_run(config_path, platform, verbose))


@main.command("serve")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
@click.option(
    "--platform",
    type=click.Choice(PLATFORM_CHOICES),
    default="all",
    help="Platforms to apply to each cycle ('all' = every enabled platform).",
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--interval-minutes", default=30, show_default=True, type=int, help="Minutes between apply cycles.")
@click.option(
    "--run-on-start/--no-run-on-start", default=True, show_default=True, help="Run one cycle immediately on startup."
)
@click.option("--verbose", is_flag=True)
@click.option("--reload", is_flag=True, help="Automatically reload server on file changes (dev only).")
def serve_cmd(
    config_path: Path,
    platform: str,
    host: str,
    port: int,
    interval_minutes: int,
    run_on_start: bool,
    verbose: bool,
    reload: bool,
) -> None:
    """Run as an always-on server that re-applies every N minutes (default 30) via uvicorn."""
    import os

    import uvicorn

    from .server import create_app

    # Surface config/prereq problems immediately instead of only inside the loop.
    config = load_config(config_path)
    setup_logging(config.log_path, verbose=verbose)
    _check_prerequisites(config, require_resume=True)
    enabled = _enabled_platforms(config, platform)
    channel = _active_messenger(config)
    if channel and _messenger_mode(config, channel) == "listener":
        msg = (
            f"{_messenger_label(channel)} listener ON (in-process) — "
            "pending questions sent & replies applied automatically."
        )
    elif channel:
        msg = (
            f"{_messenger_label(channel)} mode={_messenger_mode(config, channel)} — "
            "questions handled inline at the end of each apply cycle."
        )
    else:
        msg = "No messenger enabled — use answer-questions for deferred questions."
    console.print(
        f"[bold green]Serving on http://{host}:{port}[/bold green] — applying to "
        f"[cyan]{', '.join(enabled) or 'none enabled'}[/cyan] every "
        f"[cyan]{interval_minutes} min[/cyan].\n"
        f"[dim]{msg}[/dim]\n"
        "[dim]GET /status, POST /run-now. Stop with Ctrl+C.[/dim]"
    )

    # Set environment variables for reload mode
    os.environ["JAA_CONFIG_PATH"] = str(config_path)
    os.environ["JAA_PLATFORM"] = platform
    os.environ["JAA_INTERVAL_MINUTES"] = str(interval_minutes)
    os.environ["JAA_VERBOSE"] = "1" if verbose else "0"
    os.environ["JAA_RUN_ON_START"] = "1" if run_on_start else "0"

    if reload:
        # For reload, use factory function via string import.
        # Only watch the Python package so runtime data writes (data/*.json,
        # caches, cookies, logs) don't trigger an endless restart loop.
        from pathlib import Path as _Path

        pkg_dir = str(_Path(__file__).resolve().parent)
        uvicorn.run(
            "jobs_auto_apply.server:create_app_from_env",
            host=host,
            port=port,
            log_level="warning",
            reload=True,
            factory=True,
            reload_dirs=[pkg_dir],
            reload_includes=["*.py"],
            reload_excludes=["*.json", "*.txt", "*.log", "data/*"],
        )
    else:
        # Without reload, pass app object directly
        app = create_app(
            config_path=config_path,
            platform=platform,
            interval_minutes=interval_minutes,
            verbose=verbose,
            run_on_start=run_on_start,
        )
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="warning",
        )


async def _start_naukri_resume_sync(config: AppConfig) -> tuple[asyncio.Event, asyncio.Task]:
    """Startup sync + 30-minute background scheduler (all platforms)."""
    stop = asyncio.Event()
    try:
        await sync_naukri_resume_if_due(config)
    except Exception as exc:
        logger.warning("Naukri resume sync at startup failed: %s", exc)
    task = asyncio.create_task(run_naukri_resume_sync_scheduler(config, stop))
    return stop, task


async def _stop_naukri_resume_sync(stop: asyncio.Event, task: asyncio.Task) -> None:
    stop.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _run(config_path: Path, platform: str, verbose: bool) -> None:
    from .chrome_profile import reset_chrome_lock_flag

    reset_chrome_lock_flag()
    config = load_config(config_path)
    setup_logging(config.log_path, verbose=verbose)
    _check_prerequisites(config, require_resume=True)

    resume_stop, resume_task = await _start_naukri_resume_sync(config)
    try:
        if config.application.require_review:
            console.print(
                "[cyan]require_review is enabled — applying only to jobs you approved.[/cyan]\n"
                "Collect & review first: [bold]python main.py review --platform all[/bold]"
            )
            await _apply_reviewed(config, platform)
            return

        targets = _enabled_platforms(config, platform)
        if not targets:
            console.print(f"[yellow]No enabled platforms match --platform {platform}.[/yellow]")
            return
        parallel_targets = [t for t in targets if t in PARALLEL_COOKIE_PLATFORMS]
        if config.application.parallel_platforms and len(parallel_targets) > 1:
            console.print(
                f"[cyan]Parallel mode: {', '.join(parallel_targets)} at the same time[/cyan]\n"
                "[dim]Wellfound/Uplers still run one at a time (Chrome profile). "
                "Set parallel_platforms: false to run everything sequentially.[/dim]\n"
            )
        elif platform == "all" and len(targets) > 1 and not config.application.parallel_platforms:
            console.print(
                f"[cyan]Running {len(targets)} platforms in order: {', '.join(targets)}[/cyan]\n"
                "[dim]Set application.parallel_platforms: true to run naukri/hirist/instahyre together.[/dim]\n"
            )

        clear_run_issues()
        clear_deferred_applies(config.applied_jobs_path)
        clear_draft_answer_cache()
        if config.llm.enabled and config.llm.use_faiss_memory:
            from .embeddings import get_embeddings

            await asyncio.to_thread(get_embeddings, config.llm.embeddings_model)
        if config.llm.enabled:
            from .llm_answers import ensure_verifier_unloaded

            await asyncio.to_thread(ensure_verifier_unloaded, config)
        total_applied = await _run_platform_batch(config, targets)
        console.print(f"[green]Successfully applied to {total_applied} jobs this run.[/green]")
        from .technical_failures import technical_failures_summary

        tech_msg = technical_failures_summary(config.base_dir)
        if tech_msg:
            console.print(f"\n[yellow]{tech_msg}[/yellow]")
        await _finish_pending_questions(config, platform, targets, applied_count=total_applied)
        clear_deferred_applies(config.applied_jobs_path)
    finally:
        await _stop_naukri_resume_sync(resume_stop, resume_task)


_server_listener_channel: str | None = None


def set_server_listener_channel(channel: str | None) -> None:
    """Set when ``serve`` runs a messenger listener in-process (cleared on shutdown)."""
    global _server_listener_channel
    _server_listener_channel = channel


def server_listener_channel() -> str | None:
    return _server_listener_channel


def _active_messenger(config: AppConfig) -> str | None:
    """Which messaging channel is enabled (Telegram preferred), or None."""
    if config.telegram.enabled:
        return "telegram"
    if config.whatsapp.enabled:
        return "whatsapp"
    return None


def _messenger_label(channel: str) -> str:
    return "Telegram" if channel == "telegram" else "WhatsApp"


def _messenger_mode(config: AppConfig, channel: str) -> str:
    return config.telegram.mode if channel == "telegram" else config.whatsapp.mode


def _messenger_client_cm(config: AppConfig, channel: str):
    if channel == "telegram":
        from .telegram import telegram_client

        return telegram_client(config)
    from .whatsapp import whatsapp_client

    return whatsapp_client(config)


async def _answer_pending_via_messenger(
    config: AppConfig, *, channel: str, applied_count: int | None = None
) -> tuple[int, list]:
    """Open the messaging channel, ask each pending question, save replies."""
    label = "Telegram" if channel == "telegram" else "WhatsApp"
    console.print(f"\n[bold]Sending unanswered questions to {label}…[/bold]")
    try:
        async with _messenger_client_cm(config, channel) as client:
            return await answer_pending_groups_via_messenger(
                config.base_dir, config, client, applied_count=applied_count
            )
    except Exception as exc:
        logger.exception("%s pending flow failed: %s", label, exc)
        console.print(f"[red]{label} unavailable: {exc}[/red]")
        console.print("[dim]Questions left pending — resolve with: [bold]python main.py answer-questions[/bold][/dim]")
        return 0, []


async def _finish_pending_questions(
    config: AppConfig, platform: str, targets: list[str], *, applied_count: int | None = None
) -> None:
    """After all search pages are done: auto-answer pending questions, optional prompts, retry skips."""
    if not set(targets) & {"naukri", "hirist"}:
        return

    issues_msg = run_issues_summary()
    if issues_msg:
        console.print(f"\n[yellow]{issues_msg}[/yellow]")

    msg = summary_for_run(config.base_dir, platform=platform, config=config)
    if msg:
        console.print(msg)

    has_work = pending_count(config.base_dir, config) > 0 or run_issue_count() > 0
    if not has_work:
        return

    console.print("\n[bold]All search pages complete — resolving unanswered questions…[/bold]")
    console.print(
        "[dim]Pending review runs only after every configured SRP page is processed. "
        "Use python3 main.py answer-questions for manual follow-up.[/dim]"
    )
    prompt_on_failure = config.llm.prompt_pending_questions
    if config.llm.auto_answer_pending and not config.llm.prompt_pending_questions:
        prompt_on_failure = False

    channel = _active_messenger(config)
    if channel and _messenger_mode(config, channel) == "listener":
        if server_listener_channel() == channel:
            console.print(
                f"[dim]Questions deferred to the {_messenger_label(channel)} listener (running with serve).[/dim]"
            )
        else:
            listen_cmd = "telegram-listen" if channel == "telegram" else "whatsapp-listen"
            console.print(
                f"[dim]Questions left pending for the {channel} listener "
                f"(run: python main.py {listen_cmd}, or use serve).[/dim]"
            )
        answered, jobs_to_retry = 0, []
    elif channel:
        answered, jobs_to_retry = await _answer_pending_via_messenger(
            config, channel=channel, applied_count=applied_count
        )
    else:
        answered, jobs_to_retry = answer_pending_groups_interactive(
            config.base_dir,
            config=config,
            prompt_on_failure=prompt_on_failure,
        )
    if answered and jobs_to_retry and config.llm.retry_pending_jobs and not config.application.dry_run:
        clear_deferred_applies(config.applied_jobs_path)
        console.print("[bold]Retrying skipped jobs with new answers…[/bold]")
        try:
            retried = await retry_pending_jobs(config, jobs_to_retry)
        except Exception as exc:
            logger.exception("Pending job retry failed: %s", exc)
            console.print(f"[red]Could not retry skipped jobs: {exc}[/red]")
            console.print(
                "[dim]Quit Chrome (Cmd+Q), wait a few seconds, then re-run: "
                "[bold]python main.py run --platform hirist[/bold][/dim]"
            )
            retried = 0
        if retried:
            console.print(f"[green]Applied to {retried} job(s) after answering pending questions.[/green]")
        else:
            console.print("[yellow]No jobs were applied on retry (may need more answers or manual re-run).[/yellow]")


@main.command("review")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
@click.option("--platform", type=click.Choice(PLATFORM_CHOICES), default="all")
@click.option("--no-prompt", is_flag=True, help="Only collect jobs to JSON; skip interactive approve/reject.")
@click.option("--re-enrich", is_flag=True, help="Re-scrape job page to refresh JD and cover letters for pending items.")
@click.option("--verbose", is_flag=True)
def review_cmd(config_path: Path, platform: str, no_prompt: bool, re_enrich: bool, verbose: bool) -> None:
    """Collect up to jobs_per_platform listings per platform, then review with you interactively."""
    asyncio.run(_review(config_path, platform, no_prompt, re_enrich, verbose))


@main.command("apply-reviewed")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
@click.option("--platform", type=click.Choice(PLATFORM_CHOICES), default="all")
@click.option("--verbose", is_flag=True)
def apply_reviewed_cmd(config_path: Path, platform: str, verbose: bool) -> None:
    """Submit applications only for jobs you approved in the review queue."""
    asyncio.run(_apply_reviewed_cmd(config_path, platform, verbose))


@main.command("review-status")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
@click.option("--platform", type=click.Choice(PLATFORM_CHOICES), default="all")
def review_status_cmd(config_path: Path, platform: str) -> None:
    """Show pending / approved / rejected counts per platform."""
    config = load_config(config_path)
    table = Table(title="Review queue status")
    table.add_column("Platform")
    table.add_column("Pending", justify="right")
    table.add_column("Approved", justify="right")
    table.add_column("Rejected", justify="right")
    table.add_column("Queue file")
    for name in _enabled_platforms(config, platform):
        counts = review_summary(config.base_dir, name, _review_dir(config))
        path = config.base_dir / _review_dir(config) / f"{name}.json"
        table.add_row(
            name,
            str(counts.get("pending", 0)),
            str(counts.get("approved", 0)),
            str(counts.get("rejected", 0)),
            str(path),
        )
    console.print(table)


async def _apply_reviewed_cmd(config_path: Path, platform: str, verbose: bool) -> None:
    config = load_config(config_path)
    setup_logging(config.log_path, verbose=verbose)
    _check_prerequisites(config, require_resume=True)
    resume_stop, resume_task = await _start_naukri_resume_sync(config)
    try:
        await _apply_reviewed(config, platform)
    finally:
        await _stop_naukri_resume_sync(resume_stop, resume_task)


async def _apply_reviewed(config: AppConfig, platform: str) -> None:
    from .job_selection import load_applied_companies, pick_best_per_company

    applied_ids = load_applied_jobs(config.applied_jobs_path)
    applied_companies = load_applied_companies(config.applied_jobs_path)
    total = 0
    review_dir = _review_dir(config)

    for name in _enabled_platforms(config, platform):
        items = approved_items(config.base_dir, name, review_dir)
        pending = [
            item.to_job_listing()
            for item in items
            if job_key(item.source, item.job_id) not in applied_ids
            and not should_skip_company(item.company, config.profile.skip_companies)
            and not should_skip_role(
                item.title,
                skip_frontend=config.profile.skip_frontend_roles,
                skip_qa_test=config.profile.skip_qa_test_roles,
                keywords=config.profile.skip_role_keywords,
                jd=item.jd_excerpt,
            )[0]
            and (not config.application.one_job_per_company or company_key(item.company) not in applied_companies)
        ]
        if config.application.skip_ineligible_salary:
            from .salary import is_job_salary_eligible

            kept = []
            for job in pending:
                if is_job_salary_eligible(
                    jd=job.description,
                    meta=job.meta,
                    min_inr_lpa=config.application.min_inr_salary_lpa,
                ):
                    kept.append(job)
                else:
                    reason = job.meta.get(
                        "salary_reason",
                        f"INR < {config.application.min_inr_salary_lpa:g}L",
                    )
                    console.print(
                        f"[yellow]Skipping salary-ineligible: {job.title} @ {job.company} ({reason})[/yellow]"
                    )
            pending = kept
        if not pending:
            console.print(f"[yellow]{name}: no approved jobs left to apply.[/yellow]")
            continue

        pending, skipped = pick_best_per_company(config, pending)
        for job, winner, reason in skipped:
            if job.job_id != winner.job_id:
                console.print(
                    f"[dim]Skipping duplicate @ {job.company}: {job.title} (selected: {winner.title} — {reason})[/dim]"
                )

        limit = apply_cap(config.application.jobs_per_platform)
        if limit is not None:
            pending = pending[:limit]
        _print_jobs_table(f"{name} (approved)", pending)
        if config.application.dry_run:
            console.print(f"[cyan]Dry run — {name} submissions disabled.[/cyan]")
            continue
        async with _platform_session(name, config) as (_, context, page):
            if name == "wellfound" and config.resume.sync_to_wellfound:
                await ensure_resume_on_profile(page, config.resume_path)
            apply_fn = PLATFORM_APPLY[name]
            total += await apply_fn(page, context, pending, config)

    console.print(f"[green]Applied to {total} approved jobs.[/green]")


async def _review(config_path: Path, platform: str, no_prompt: bool, re_enrich: bool, verbose: bool) -> None:
    config = load_config(config_path)
    setup_logging(config.log_path, verbose=verbose)
    _check_prerequisites(config, require_resume=False)
    review_dir = _review_dir(config)
    target = config.application.jobs_per_platform
    unlimited_review = is_unlimited(target)

    for name in _enabled_platforms(config, platform):
        payload = load_review_queue(config.base_dir, name, review_dir)
        items = items_from_payload(payload)
        by_key = {item.job_key: item for item in items}

        rejected_roles = auto_reject_skipped_roles(
            items,
            skip_frontend=config.profile.skip_frontend_roles,
            skip_qa_test=config.profile.skip_qa_test_roles,
            keywords=config.profile.skip_role_keywords,
        )
        if rejected_roles:
            save_review_queue(config.base_dir, name, build_review_payload(name, items), review_dir)
            console.print(f"[yellow]{name}: auto-rejected {rejected_roles} frontend/skipped roles.[/yellow]")

        if re_enrich:
            to_fix = [item for item in items if item.status == "pending" and needs_re_enrich(item)]
            if to_fix:
                workers = config.application.enrich_workers
                console.print(f"[bold]{name}:[/bold] re-enriching {len(to_fix)} items ({workers} parallel tabs)...")
                async with _platform_session(name, config) as (_, context, page):
                    jobs = [item.to_job_listing() for item in to_fix]
                    enriched = await enrich_jobs_parallel(context, config, jobs, workers=workers)
                    for item, enriched_item in zip(to_fix, enriched):
                        enriched_item.status = item.status
                        by_key[item.job_key] = enriched_item
                items = list(by_key.values())
                save_review_queue(config.base_dir, name, build_review_payload(name, items), review_dir)
                console.print(f"[green]{name}: refreshed JD and cover letters.[/green]")
            else:
                pending = [item for item in items if item.status == "pending" and item.jd_excerpt]
                if pending:
                    console.print(f"[bold]{name}:[/bold] refreshing {len(pending)} cover letters...")
                    count = await refresh_cover_letters(config, pending)
                    save_review_queue(config.base_dir, name, build_review_payload(name, items), review_dir)
                    console.print(f"[green]{name}: regenerated {count} cover letters.[/green]")
                else:
                    console.print(f"[cyan]{name}: no pending items need re-enrich.[/cyan]")

        items = list(by_key.values())
        existing_keys = {item.job_key for item in items}
        applied_ids = load_applied_jobs(config.applied_jobs_path)

        need = None if unlimited_review else max(0, target - sum(1 for item in items if item.status == "pending"))
        if need == 0:
            console.print(f"[cyan]{name}: review queue already has {len(items)} items (target {target}).[/cyan]")
        else:
            label = "all eligible" if need is None else str(need)
            console.print(f"[bold]{name}:[/bold] collecting up to {label} new jobs for review...")
            from .job_selection import score_job

            async with _platform_session(name, config) as (_, context, page):
                raw_jobs = filter_skipped_companies(
                    await _collect_jobs_for_platform(name, config, page),
                    config.profile.skip_companies,
                )
                raw_jobs = filter_skipped_roles(
                    raw_jobs,
                    skip_frontend=config.profile.skip_frontend_roles,
                    skip_qa_test=config.profile.skip_qa_test_roles,
                    keywords=config.profile.skip_role_keywords,
                )
                candidates: list[JobListing] = []
                for job in raw_jobs:
                    key = job_key(job.source, job.job_id)
                    if key in existing_keys or key in applied_ids:
                        continue
                    if get_decision(config.base_dir, key):
                        continue
                    ck = company_key(job.company)
                    if ck and config.application.one_job_per_company:
                        dup = next(
                            (i for i in items if company_key(i.company) == ck),
                            None,
                        )
                        if dup and dup.status == "approved":
                            continue
                    candidates.append(job)
                    if unlimited_review:
                        if len(candidates) >= scrape_limit(0):
                            break
                    elif len(candidates) >= max(need * 3, need + 5):
                        break

                workers = config.application.enrich_workers
                console.print(f"  Enriching {len(candidates)} jobs ({workers} parallel tabs)...")
                enriched_items = await enrich_jobs_parallel(context, config, candidates, workers=workers)

                new_items: list[ReviewItem] = []
                for item in enriched_items:
                    if should_skip_role(
                        item.title,
                        skip_frontend=config.profile.skip_frontend_roles,
                        skip_qa_test=config.profile.skip_qa_test_roles,
                        keywords=config.profile.skip_role_keywords,
                        jd=item.jd_excerpt,
                    )[0]:
                        console.print(f"  Skip frontend/skipped role: {item.title} @ {item.company}")
                        continue
                    if config.application.skip_ineligible_salary and not item.meta.get("salary_eligible", True):
                        console.print(
                            f"  Skip salary-ineligible: {item.title} @ {item.company} "
                            f"— {item.meta.get('salary_reason', '')}"
                        )
                        continue
                    ck = company_key(item.company)
                    if ck and config.application.one_job_per_company:
                        dup = next(
                            (i for i in items if company_key(i.company) == ck),
                            None,
                        )
                        if dup:
                            new_fit = score_job(config, item)
                            old_fit = score_job(config, dup)
                            if dup.status == "approved":
                                continue
                            if new_fit.total > old_fit.total:
                                console.print(
                                    f"  Replacing {dup.title} with better fit {item.title} "
                                    f"({new_fit.total:.1f} vs {old_fit.total:.1f})"
                                )
                                items = [i for i in items if i.job_key != dup.job_key]
                                by_key.pop(dup.job_key, None)
                                existing_keys.discard(dup.job_key)
                            else:
                                console.print(
                                    f"  Skip {item.title} — keeping {dup.title} "
                                    f"({old_fit.total:.1f} vs {new_fit.total:.1f})"
                                )
                                continue
                    new_items.append(item)
                    by_key[item.job_key] = item
                    existing_keys.add(item.job_key)
                    if need is not None and len(new_items) >= need:
                        break
                items.extend(new_items)
                payload = build_review_payload(name, items)
                path = save_review_queue(config.base_dir, name, payload, review_dir)
                console.print(f"[green]Saved {len(new_items)} new items → {path}[/green]")

        if no_prompt:
            continue

        payload = load_review_queue(config.base_dir, name, review_dir)
        items = items_from_payload(payload)
        if repair_review_queue_items(items):
            save_review_queue(config.base_dir, name, build_review_payload(name, items), review_dir)
            console.print(f"[green]{name}: fixed company names from job pages.[/green]")
        _interactive_review(config, name, items, review_dir)


def _interactive_review(config: AppConfig, platform: str, items: list[ReviewItem], review_dir: str) -> None:
    skip = config.profile.skip_companies
    pending = [
        item
        for item in items
        if item.status == "pending"
        and not should_skip_company(item.company, skip)
        and not should_skip_role(
            item.title,
            skip_frontend=config.profile.skip_frontend_roles,
            skip_qa_test=config.profile.skip_qa_test_roles,
            keywords=config.profile.skip_role_keywords,
            jd=item.jd_excerpt,
        )[0]
    ]
    if not pending:
        console.print(f"[green]{platform}: nothing pending to review.[/green]")
        return

    console.print(f"\n[bold]Review {len(pending)} {platform} jobs[/bold] (your choices are saved to memory)\n")
    by_key = {item.job_key: item for item in items}

    for idx, item in enumerate(pending, 1):
        meta = dict(item.meta or {})
        if "eligible_to_apply" not in meta and item.jd_excerpt:
            from .salary import job_eligibility

            meta.update(
                job_eligibility(
                    jd=item.jd_excerpt,
                    meta=meta,
                    min_inr_lpa=config.application.min_inr_salary_lpa,
                )
            )
            item.meta = meta

        warnings = []
        if meta.get("salary_display"):
            warnings.append(f"Salary: {meta['salary_display']}")
        if meta.get("location_blocked"):
            warnings.append("⛔ Location blocked for your profile")
        if meta.get("salary_eligible") is False:
            warnings.append(f"⛔ Salary ineligible: {meta.get('salary_reason', '')}")
        dup_count = sum(
            1
            for other in pending
            if other.job_key != item.job_key and company_key(other.company) == company_key(item.company)
        )
        if dup_count and config.application.one_job_per_company:
            warnings.append(f"ℹ️ {dup_count + 1} openings at this company — best fit selected on apply")
        warn_text = ("\n" + "\n".join(warnings)) if warnings else ""

        jd_preview = item.jd_excerpt[:2000]
        jd_suffix = "..." if len(item.jd_excerpt) > 2000 else ""
        body = (
            f"[bold]{item.title}[/bold] @ {item.company}\n"
            f"URL: {item.url}{warn_text}\n\n"
            f"[dim]JD ({len(item.jd_excerpt)} chars):[/dim]\n{jd_preview}{jd_suffix}\n\n"
            f"[dim]Cover letter preview:[/dim]\n{item.cover_letter}"
        )
        console.print(Panel(body, title=f"{platform} [{idx}/{len(pending)}]", border_style="blue"))

        choice = click.prompt(
            "Decision",
            type=click.Choice(["a", "r", "s", "q"], case_sensitive=False),
            default="s",
            show_choices=False,
            prompt_suffix=" [a=pprove, r=eject, s=kip, q=uit]: ",
        )
        if choice == "q":
            break
        if choice == "s":
            continue

        status = "approved" if choice == "a" else "rejected"
        note = ""
        if status == "rejected":
            note = click.prompt("Reason (optional)", default="", show_default=False)

        by_key[item.job_key].status = status
        record_decision(
            config.base_dir,
            job_key=item.job_key,
            status=status,
            platform=platform,
            meta={"title": item.title, "company": item.company, "note": note},
        )
        save_preferences(
            config.base_dir,
            {
                "skip_companies": config.profile.skip_companies,
                "last_review_platform": platform,
                f"last_{status}": {"title": item.title, "company": item.company},
            },
        )
        console.print(f"[{'green' if status == 'approved' else 'red'}]{status}[/]: {item.title}")

    payload = build_review_payload(platform, list(by_key.values()))
    save_review_queue(config.base_dir, platform, payload, review_dir)
    counts = review_summary(config.base_dir, platform, review_dir)
    console.print(
        f"\n{platform} queue: {counts.get('pending', 0)} pending, "
        f"{counts.get('approved', 0)} approved, {counts.get('rejected', 0)} rejected"
    )
    console.print("Run [bold]python main.py apply-reviewed --platform all[/bold] when ready.")


def _check_prerequisites(config: AppConfig, *, require_resume: bool = True) -> None:
    if require_resume and not config.resume_path.exists():
        console.print(f"[red]Resume not found:[/red] {config.resume_path}")
        raise SystemExit(1)
    if require_resume is False and not config.resume_path.exists():
        console.print(
            f"[yellow]Resume not found ({config.resume_path}) — review will continue; "
            "add resume.pdf before apply-reviewed.[/yellow]"
        )
    if not config.user.email:
        console.print("[red]Set user.email in config.yaml.[/red]")
        raise SystemExit(1)
    facts_path = config.base_dir / "profile" / "resume_facts.yaml"
    if not facts_path.exists():
        console.print(f"[red]Resume facts not found:[/red] {facts_path}")
        raise SystemExit(1)


class _platform_session:
    def __init__(self, platform: str, config: AppConfig) -> None:
        self.platform = platform
        self.config = config
        self._cm = None

    async def __aenter__(self):
        sessions = {
            "wellfound": wellfound_session,
            "uplers": uplers_session,
            "naukri": naukri_session,
            "hirist": hirist_session,
            "instahyre": instahyre_session,
        }
        self._cm = sessions[self.platform](self.config)
        return await self._cm.__aenter__()

    async def __aexit__(self, *args):
        if self._cm:
            return await self._cm.__aexit__(*args)
        return False


async def _collect_jobs_for_platform(platform: str, config: AppConfig, page) -> list[JobListing]:
    limit = scrape_limit(config.application.jobs_per_platform, multiplier=1)
    if platform == "wellfound":
        filters = config.wellfound.filters
        if isinstance(filters, WellfoundFiltersConfig):
            await wellfound_apply_filters(page, filters)
            return await wellfound_collect_jobs(page, limit)
    if platform == "uplers":
        filters = config.uplers.filters
        if isinstance(filters, UplersFiltersConfig):
            await uplers_apply_filters(page, filters)
            return await uplers_collect_jobs(page, limit)
    if platform == "naukri":
        filters = config.naukri.filters
        if isinstance(filters, NaukriFiltersConfig):
            await naukri_apply_filters(page, filters)
            return await naukri_collect_jobs(
                page,
                limit,
                quick_apply_only=filters.quick_apply_only,
                sort=filters.sort,
                max_job_age_days=filters.max_job_age_days,
            )
    if platform == "hirist":
        filters = config.hirist.filters
        if isinstance(filters, HiristFiltersConfig):
            if filters.search_urls:
                return await hirist_collect_from_search_urls(page, filters.search_urls, limit)
            await hirist_apply_filters(page, filters)
            return await hirist_collect_jobs(page, limit)
    if platform == "instahyre":
        filters = config.instahyre.filters
        if isinstance(filters, InstahyreFiltersConfig):
            await instahyre_apply_filters(page, filters)
            if filters.search_urls or filters.feeds:
                return await instahyre_collect_from_search_urls(
                    page,
                    filters.search_urls,
                    limit,
                    feed_dicts=filters.feeds or None,
                    default_job_functions=filters.job_functions,
                )
            return await instahyre_collect_jobs(page, limit)
    return []


async def _run_wellfound(config: AppConfig, applied_ids: set[str]) -> int:
    from .job_selection import pick_best_per_company
    from .salary import is_job_salary_eligible

    filters = config.wellfound.filters
    if not isinstance(filters, WellfoundFiltersConfig):
        return 0
    async with wellfound_session(config) as (_, context, page):
        if config.resume.sync_to_wellfound:
            await ensure_resume_on_profile(page, config.resume_path)
        await wellfound_apply_filters(page, filters)

        if config.application.pipeline_apply:
            workers = config.application.apply_workers
            console.print(f"[bold]Pipeline mode: {workers} workers scroll feed and apply in parallel[/bold]")
            if config.application.dry_run:
                console.print("[cyan]Dry run — Wellfound submissions disabled.[/cyan]")
            return await run_wellfound_pipeline(page, context, config, applied_ids)

        raw_jobs = filter_skipped_roles(
            filter_skipped_companies(
                await wellfound_collect_jobs(page, scrape_limit(config.application.max_jobs_per_run, multiplier=1)),
                config.profile.skip_companies,
            ),
            skip_frontend=config.profile.skip_frontend_roles,
            skip_qa_test=config.profile.skip_qa_test_roles,
            keywords=config.profile.skip_role_keywords,
        )
        console.print(f"[cyan]Collected {len(raw_jobs)} listings from Wellfound search[/cyan]")
        candidates: list[JobListing] = []
        skipped_applied = 0
        for job in raw_jobs:
            if job_key(job.source, job.job_id) in applied_ids:
                skipped_applied += 1
                continue
            candidates.append(job)
        if skipped_applied:
            console.print(f"[dim]Skipped {skipped_applied} already applied[/dim]")

        if not candidates:
            console.print("[yellow]No new Wellfound jobs to apply to.[/yellow]")
            return 0

        workers = config.application.enrich_workers
        console.print(f"[bold]Enriching {len(candidates)} jobs ({workers} tabs) before apply...[/bold]")
        enriched = await enrich_jobs_parallel(context, config, candidates, workers=workers)

        pending: list[JobListing] = []
        skipped_role = skipped_salary = 0
        for item in enriched:
            if should_skip_role(
                item.title,
                skip_frontend=config.profile.skip_frontend_roles,
                skip_qa_test=config.profile.skip_qa_test_roles,
                keywords=config.profile.skip_role_keywords,
                jd=item.jd_excerpt,
            )[0]:
                skipped_role += 1
                continue
            if config.application.skip_ineligible_salary and not is_job_salary_eligible(
                jd=item.jd_excerpt,
                meta=item.meta,
                min_inr_lpa=config.application.min_inr_salary_lpa,
            ):
                skipped_salary += 1
                console.print(
                    f"[yellow]Skip salary-ineligible: {item.title} @ {item.company} "
                    f"— {item.meta.get('salary_reason', '')}[/yellow]"
                )
                continue
            pending.append(item.to_job_listing())

        if skipped_role or skipped_salary:
            console.print(f"[dim]Filtered: {skipped_role} role, {skipped_salary} salary[/dim]")

        if not pending:
            console.print("[yellow]No eligible Wellfound jobs after filters.[/yellow]")
            return 0

        before_dedup = len(pending)
        pending, skipped = pick_best_per_company(config, pending)
        for job, winner, reason in skipped:
            if job.job_id != winner.job_id:
                console.print(
                    f"[dim]Skipping duplicate @ {job.company}: {job.title} (selected: {winner.title} — {reason})[/dim]"
                )
        if before_dedup != len(pending):
            console.print(f"[dim]One job per company: {before_dedup} → {len(pending)} openings[/dim]")

        cap = apply_cap(config.application.jobs_per_platform)
        if cap is None:
            cap = apply_cap(config.application.max_jobs_per_run)
        if cap is not None:
            pending = pending[:cap]

        _print_jobs_table("Wellfound (auto-apply)", pending)
        if config.application.dry_run:
            console.print("[cyan]Dry run — Wellfound submissions disabled.[/cyan]")
            return 0
        return await wellfound_apply_batch(page, context, pending, config)


async def _run_uplers(config: AppConfig, applied_ids: set[str]) -> int:
    filters = config.uplers.filters
    if not isinstance(filters, UplersFiltersConfig):
        return 0
    async with uplers_session(config) as (_, context, page):
        await uplers_apply_filters(page, filters)
        jobs = await uplers_collect_jobs(page, scrape_limit(config.application.max_jobs_per_run, multiplier=3))
        return await _apply_list("Uplers → company sites", jobs, applied_ids, config, uplers_apply_batch, page, context)


async def _run_naukri(config: AppConfig, applied_ids: set[str]) -> int:
    filters = config.naukri.filters
    if not isinstance(filters, NaukriFiltersConfig):
        return 0
    async with naukri_session(config) as (_, context, page):
        reconcile_applied_jobs(config.applied_jobs_path)
        await sync_naukri_resume_if_due(config, page=page)
        await naukri_go_to_search_page(page, filters)
        if config.application.pipeline_apply:
            return await run_naukri_pipeline(page, context, config, applied_ids)
        per_batch_limit = scrape_limit(config.application.max_jobs_per_run, multiplier=3)
        max_batches = max(1, filters.max_pages)
        total = 0
        last_batch_with_jobs = 0
        session_seen: set[str] = set()
        for batch_num in range(1, max_batches + 1):
            console.print(f"\n[bold cyan]Naukri scroll batch {batch_num}/{max_batches} — collect & apply[/bold cyan]")
            if batch_num > 1 and not await scroll_naukri_srp_more(page):
                console.print("[dim]Naukri: no new listings after scroll — stopping[/dim]")
                break
            jobs = await collect_naukri_srp_batch(
                page,
                per_batch_limit,
                seen_job_ids=session_seen,
                quick_apply_only=filters.quick_apply_only,
                sort=filters.sort,
                max_job_age_days=filters.max_job_age_days,
                initial_scroll=(batch_num == 1),
            )
            for job in jobs:
                session_seen.add(job.job_id)
            if not jobs:
                if batch_num > 1:
                    console.print(f"[dim]Naukri: no new jobs in batch {batch_num} — stopping[/dim]")
                break
            last_batch_with_jobs = batch_num
            n = await _apply_list(
                f"Naukri (batch {batch_num})",
                jobs,
                applied_ids,
                config,
                naukri_apply_batch,
                page,
                context,
            )
            total += n
            applied_ids = load_applied_jobs(config.applied_jobs_path)
            console.print(
                f"[dim]Naukri batch {batch_num}: applied {n} job(s) "
                f"({total} total this run, {len(session_seen)} collected)[/dim]"
            )
        if max_batches > 1:
            console.print(
                f"[green]Naukri scroll done: processed batches 1–{last_batch_with_jobs} "
                f"of {max_batches} configured.[/green]"
            )
        return total


async def _run_hirist(config: AppConfig, applied_ids: set[str]) -> int:
    filters = config.hirist.filters
    if not isinstance(filters, HiristFiltersConfig):
        return 0
    max_pages = max(1, filters.max_pages)
    async with hirist_session(config) as (_, context, page):
        if not filters.search_urls and max_pages <= 1:
            limit = scrape_limit(config.application.max_jobs_per_run, multiplier=1)
            await hirist_apply_filters(page, filters)
            jobs = await hirist_collect_jobs(page, limit)
            return await _apply_list("Hirist", jobs, applied_ids, config, hirist_apply_batch, page, context)

        # Collect every listing across all feed pages/URLs into one deduped list
        # first, then apply in a single batch. One large batch keeps the parallel
        # apply workers saturated (no per-page drain stalls) and lets dedup +
        # already-applied/skip filtering + the run cap apply globally.
        session_seen: set[str] = set()
        all_jobs: list[JobListing] = []
        async for _feed_url, page_num, jobs in iter_paginated_feed_pages(page, filters, max_pages=max_pages):
            new_jobs = [j for j in jobs if j.job_id not in session_seen]
            for job in new_jobs:
                session_seen.add(job.job_id)
            if not new_jobs:
                if page_num > 1:
                    console.print(f"[dim]Hirist: no new jobs on page {page_num} — next feed/page[/dim]")
                continue
            all_jobs.extend(new_jobs)
            console.print(
                f"[dim]Hirist: collected {len(new_jobs)} new on page {page_num} ({len(all_jobs)} total)[/dim]"
            )

        if not all_jobs:
            console.print("[yellow]No Hirist listings collected.[/yellow]")
            return 0

        console.print(f"[cyan]Hirist: collected {len(all_jobs)} unique listing(s) — applying in one batch[/cyan]")
        return await _apply_list(
            "Hirist",
            all_jobs,
            applied_ids,
            config,
            hirist_apply_batch,
            page,
            context,
        )


async def _run_instahyre(config: AppConfig, applied_ids: set[str]) -> int:
    filters = config.instahyre.filters
    if not isinstance(filters, InstahyreFiltersConfig):
        return 0
    async with instahyre_session(config) as (_, context, page):
        if filters.search_urls or filters.feeds:
            # Instahyre runs sequentially (single tab walking the feeds), even when
            # pipeline_apply / instahyre_apply_workers are set for naukri/hirist.
            return await instahyre_apply_from_feeds(
                page,
                config,
                applied_ids,
                search_urls=filters.search_urls or None,
                feed_dicts=filters.feeds or None,
                default_job_functions=filters.job_functions,
            )
        limit = scrape_limit(config.application.max_jobs_per_run, multiplier=1)
        await instahyre_apply_filters(page, filters)
        jobs = await instahyre_collect_jobs(page, limit)
        return await _apply_list("Instahyre", jobs, applied_ids, config, instahyre_apply_batch, page, context)


async def _apply_list(title, jobs, applied_ids, config, apply_fn, page, context) -> int:
    pending = _pending_jobs(jobs, applied_ids, config.application.max_jobs_per_run, config)
    if not pending:
        console.print(f"[yellow]No new {title} jobs to apply to.[/yellow]")
        return 0
    _print_jobs_table(title, pending)
    if config.application.dry_run:
        console.print(f"[cyan]Dry run — {title} submissions disabled.[/cyan]")
    return await apply_fn(page, context, pending, config)


def _pending_jobs(jobs: list[JobListing], applied_ids: set[str], limit: int, config: AppConfig) -> list[JobListing]:
    return filter_pending_jobs(jobs, applied_ids, limit, config)


def _print_jobs_table(title: str, jobs: list[JobListing]) -> None:
    table = Table(title=title)
    table.add_column("Source")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("URL")
    for job in jobs:
        table.add_row(job.source, job.title, job.company, job.url)
    console.print(table)


@main.command("chrome-profiles")
def chrome_profiles_cmd() -> None:
    profiles = list_chrome_profiles()
    if not profiles:
        console.print("[yellow]No Chrome profiles found.[/yellow]")
        return
    table = Table(title="Chrome profiles")
    table.add_column("Label")
    table.add_column("Path")
    for label, path in profiles:
        table.add_row(label, str(path))
    console.print(table)


@main.command("login")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
@click.option("--platform", type=click.Choice(PLATFORM_CHOICES), default="all")
def login_cmd(config_path: Path, platform: str) -> None:
    """Sign in once (Google/passkey) and save session."""
    asyncio.run(_login(config_path, platform))


async def _login(config_path: Path, platform: str) -> None:
    config = load_config(config_path)
    setup_logging(config.log_path)
    if config.browser.use_chrome_profile:
        console.print(f"[cyan]Chrome profile:[/cyan] {resolve_chrome_profile_dir(config)}")
        console.print("[bold]Quit Google Chrome completely (Cmd+Q) before continuing.[/bold]\n")

    sessions = [
        ("wellfound", config.wellfound.enabled, wellfound_session),
        ("uplers", config.uplers.enabled, uplers_session),
        ("naukri", config.naukri.enabled, naukri_session),
        ("hirist", config.hirist.enabled, hirist_session),
        ("instahyre", config.instahyre.enabled, instahyre_session),
    ]
    for name, enabled, session_fn in sessions:
        if platform in (name, "all") and enabled:
            async with session_fn(config):
                console.print(f"[green]{name.capitalize()} session ready.[/green]")


@main.command("verify")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
@click.option("--platform", type=click.Choice(PLATFORM_CHOICES), default="all")
def verify_cmd(config_path: Path, platform: str) -> None:
    asyncio.run(_login(config_path, platform))


@main.command("export-cookies-help")
@click.option("--platform", type=click.Choice(PLATFORM_CHOICES), default="all")
def export_cookies_help(platform: str) -> None:
    console.print(
        "[bold]Recommended:[/bold] Chrome profile reuse (browser.use_chrome_profile: true)\n"
        "  Log into sites in Chrome → quit Chrome → python main.py review --platform all\n"
    )


@main.command("answer-questions")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
@click.option("--review", is_flag=True, help="Fix bad auto-generated saved answers.")
@click.option("--all", "review_all", is_flag=True, help="Review/edit all saved answers.")
@click.option("--suggest", is_flag=True, help="Ask LLM for a suggested answer before each prompt (slower).")
@click.option("--no-retry", is_flag=True, help="Save answers only; do not re-apply to skipped jobs.")
def answer_questions_cmd(config_path: Path, review: bool, review_all: bool, suggest: bool, no_retry: bool) -> None:
    """Answer pending questions or review saved answers in user_memory.json."""
    config = load_config(config_path)
    if review or review_all:
        review_saved_answers_interactive(config.base_dir, all_answers=review_all, config=config)
    else:
        answered, jobs_to_retry = answer_pending_groups_interactive(
            config.base_dir,
            config=config,
            suggest_answers=suggest,
        )
        if (
            answered
            and jobs_to_retry
            and config.llm.retry_pending_jobs
            and not no_retry
            and not config.application.dry_run
        ):
            retried = asyncio.run(retry_pending_jobs(config, jobs_to_retry))
            console.print(f"[green]Applied to {retried} job(s) after answering pending questions.[/green]")


@main.command("whatsapp-login")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
def whatsapp_login_cmd(config_path: Path) -> None:
    """Link WhatsApp Web once by scanning the QR code (session is then reused)."""
    config = load_config(config_path)
    from .whatsapp import WhatsAppClient

    async def _link() -> None:
        client = WhatsAppClient(
            profile_dir=config.whatsapp_profile_path,
            phone=config.whatsapp.phone or "0",
            headless=False,
            login_timeout_seconds=max(config.whatsapp.login_timeout_seconds, 180),
        )
        await client.start()
        try:
            ok = await client.ensure_logged_in()
            if ok:
                console.print("[green]WhatsApp Web linked. You're all set.[/green]")
            else:
                console.print("[red]Did not detect a linked session before timeout.[/red]")
        finally:
            await client.close()

    asyncio.run(_link())


@main.command("whatsapp-answer")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
@click.option("--no-retry", is_flag=True, help="Save answers only; do not re-apply to skipped jobs.")
@click.option("--test", "test_send", is_flag=True, help="Send a single test message to verify the WhatsApp link works.")
def whatsapp_answer_cmd(config_path: Path, no_retry: bool, test_send: bool) -> None:
    """Send pending questions to WhatsApp now, save replies, then retry those jobs."""
    config = load_config(config_path)
    if not config.whatsapp.enabled:
        console.print("[yellow]whatsapp.enabled is false in config.yaml.[/yellow]")
        return

    if test_send:
        from .whatsapp import whatsapp_client

        async def _test() -> None:
            console.print(
                f"[bold]Sending a test question to {config.whatsapp.phone} and waiting for your reply…[/bold]"
            )
            console.print("[dim]Reply to the WhatsApp message; this keeps the window open until you do.[/dim]")
            try:
                async with whatsapp_client(config) as client:
                    reply = await client.ask(
                        "🧪 Test from jobs-auto-apply — reply to this message to confirm the app can read your replies."
                    )
                if reply is None:
                    console.print("[yellow]No reply detected before the timeout.[/yellow]")
                else:
                    console.print(f"[green]Got your reply:[/green] {reply!r}")
            except Exception as exc:
                logger.exception("WhatsApp test failed: %s", exc)
                console.print(f"[red]Test failed: {exc}[/red]")

        asyncio.run(_test())
        return

    async def _run() -> None:
        answered, jobs_to_retry = await _answer_pending_via_messenger(config, channel="whatsapp")
        if (
            answered
            and jobs_to_retry
            and config.llm.retry_pending_jobs
            and not no_retry
            and not config.application.dry_run
        ):
            console.print("[bold]Retrying skipped jobs with new answers…[/bold]")
            retried = await retry_pending_jobs(config, jobs_to_retry)
            console.print(f"[green]Applied to {retried} job(s) after WhatsApp answers.[/green]")

    asyncio.run(_run())


def _format_apply_result(applied: int, total: int, titles: str) -> str:
    """Build a clear per-batch outcome message for the messenger result."""
    if applied >= total and total > 0:
        return f"✅ Applied to {applied}/{total} job(s): {titles}"
    if applied == 0:
        return (
            f"❌ Couldn't apply to {total} job(s): {titles}. "
            "It may already be applied, need more answers, or be on a platform "
            "without retry support. I'll try again next cycle."
        )
    return f"⚠️ Applied to {applied}/{total} job(s): {titles}. The rest couldn't complete this time."


async def _messenger_listen(
    config: AppConfig,
    *,
    channel: str | None = None,
    apply_lock: asyncio.Lock | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Keep one messaging session open, asking pending questions and applying replies.

    Works for either transport (``channel`` = "telegram" or "whatsapp"; auto-detected
    when omitted). Loops forever: ask whatever questions are pending, apply the
    answered jobs, then loop again — so any NEW questions surfaced while retrying get
    asked and applied too. When ``apply_lock`` is provided (e.g. the server runs this
    alongside the scheduled apply cycle), the re-apply step takes the lock so two
    browser apply-sessions never run at once. ``stop_event`` lets a host request a
    clean shutdown between iterations.
    """
    channel = channel or _active_messenger(config)
    if not channel:
        logger.warning("No messaging channel enabled; listener not started.")
        return
    label = "Telegram" if channel == "telegram" else "WhatsApp"
    relink_cmd = "telegram-login" if channel == "telegram" else "whatsapp-login"
    msg_cfg = config.telegram if channel == "telegram" else config.whatsapp

    # Wait effectively indefinitely per question so the listener never gives up
    # on a reply while it's running.
    per_question_timeout = max(msg_cfg.reply_timeout_seconds, 86400)
    idle = max(5, msg_cfg.listen_idle_seconds)

    # WhatsApp's QR link can genuinely expire (needs re-linking); a Telegram bot
    # token never does, so for Telegram a failing login check is just a transient
    # network blip — keep the session open indefinitely and retry instead of exiting.
    async with _messenger_client_cm(config, channel) as client:
        session_can_expire = getattr(client, "session_can_expire", True)
        console.print(f"[green]{label} listener running. Press Ctrl+C to stop.[/green]")
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                if not await client.is_logged_in():
                    if session_can_expire:
                        console.print(f"[red]{label} session ended — re-link with: python main.py {relink_cmd}[/red]")
                        break
                    logger.warning(
                        "%s login check failed (likely a transient network issue) — "
                        "retrying in %ds; the session stays open.",
                        label,
                        idle,
                    )
                    await asyncio.sleep(idle)
                    continue
                answered, jobs = await answer_pending_groups_via_messenger(
                    config.base_dir,
                    config,
                    client,
                    send_heads_up=False,
                    per_question_timeout=per_question_timeout,
                )
                if answered and jobs and config.llm.retry_pending_jobs and not config.application.dry_run:
                    clear_deferred_applies(config.applied_jobs_path)
                    total = len(jobs)
                    titles = ", ".join(
                        (ref.title or ref.url) + (f" @ {ref.company}" if ref.company else "") for ref in jobs
                    )
                    with contextlib.suppress(Exception):
                        await client.send(f"⏳ Applying to {total} job(s) with your answer(s): {titles}")
                    try:
                        if apply_lock is not None:
                            async with apply_lock:
                                retried = await retry_pending_jobs(config, jobs)
                        else:
                            retried = await retry_pending_jobs(config, jobs)
                        await client.send(_format_apply_result(retried, total, titles))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception("Listener retry failed: %s", exc)
                        with contextlib.suppress(Exception):
                            await client.send(f"⚠️ Could not apply ({titles}): {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("%s listener iteration failed: %s", label, exc)
            await asyncio.sleep(idle)


@main.command("whatsapp-listen")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
def whatsapp_listen_cmd(config_path: Path) -> None:
    """Stay running: send pending questions to WhatsApp and apply as replies arrive.

    Keeps one WhatsApp session open so it never re-asks for the QR, and picks up
    your replies asynchronously — even ones you send long after a run finishes.
    Set whatsapp.mode: listener so `run` defers questions to this process.
    """
    config = load_config(config_path)
    if not config.whatsapp.enabled:
        console.print("[yellow]whatsapp.enabled is false in config.yaml.[/yellow]")
        return
    try:
        asyncio.run(_messenger_listen(config, channel="whatsapp"))
    except KeyboardInterrupt:
        console.print("\n[dim]Listener stopped.[/dim]")


@main.command("telegram-login")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
def telegram_login_cmd(config_path: Path) -> None:
    """Verify the bot token and capture your chat_id (send /start to the bot)."""
    config = load_config(config_path)
    from .telegram import TelegramClient, TelegramError

    async def _link() -> None:
        client = TelegramClient(
            token=config.telegram.bot_token,
            chat_id=config.telegram.chat_id,
            chat_id_path=config.telegram_chat_path,
            offset_path=config.telegram_offset_path,
        )
        try:
            await client.start()
        except TelegramError as exc:
            console.print(f"[red]{exc}[/red]")
            return
        username = await client.bot_username()
        if client.chat_id:
            console.print(f"[green]Telegram ready.[/green] Bot @{username}, chat_id={client.chat_id}.")
            with contextlib.suppress(Exception):
                await client.send("✅ jobs-auto-apply is linked to this chat.")
            return
        console.print(
            f"[bold]Open Telegram, find your bot @{username}, and send it /start "
            f"(or any message).[/bold] Waiting up to 120s…"
        )
        chat_id = await client.capture_chat_id(timeout=120)
        if chat_id:
            console.print(
                f"[green]Captured chat_id={chat_id}.[/green] Saved to {config.telegram_chat_path.name}; you're all set."
            )
            with contextlib.suppress(Exception):
                await client.send("✅ jobs-auto-apply is linked to this chat.")
        else:
            console.print("[red]No message received. Re-run and send /start to the bot.[/red]")

    asyncio.run(_link())


@main.command("telegram-answer")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
@click.option("--no-retry", is_flag=True, help="Save answers only; do not re-apply to skipped jobs.")
@click.option("--test", "test_send", is_flag=True, help="Send a test message and wait for your reply.")
def telegram_answer_cmd(config_path: Path, no_retry: bool, test_send: bool) -> None:
    """Send pending questions to Telegram now, save replies, then retry those jobs."""
    config = load_config(config_path)
    if not config.telegram.enabled:
        console.print("[yellow]telegram.enabled is false in config.yaml.[/yellow]")
        return

    if test_send:
        from .telegram import telegram_client

        async def _test() -> None:
            console.print("[bold]Sending a Telegram test and waiting for your reply…[/bold]")
            try:
                async with telegram_client(config) as client:
                    reply = await client.ask(
                        "🧪 Test from jobs-auto-apply — reply to confirm the app reads your replies."
                    )
                if reply is None:
                    console.print("[yellow]No reply detected before the timeout.[/yellow]")
                else:
                    console.print(f"[green]Got your reply:[/green] {reply!r}")
            except Exception as exc:
                logger.exception("Telegram test failed: %s", exc)
                console.print(f"[red]Test failed: {exc}[/red]")

        asyncio.run(_test())
        return

    async def _run() -> None:
        answered, jobs_to_retry = await _answer_pending_via_messenger(config, channel="telegram")
        if (
            answered
            and jobs_to_retry
            and config.llm.retry_pending_jobs
            and not no_retry
            and not config.application.dry_run
        ):
            console.print("[bold]Retrying skipped jobs with new answers…[/bold]")
            retried = await retry_pending_jobs(config, jobs_to_retry)
            console.print(f"[green]Applied to {retried} job(s) after Telegram answers.[/green]")

    asyncio.run(_run())


@main.command("telegram-listen")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
def telegram_listen_cmd(config_path: Path) -> None:
    """Stay running: send pending questions to Telegram and apply as replies arrive.

    Picks up your replies asynchronously — even ones sent long after a run
    finishes. Prefer ``python main.py serve`` (same listener, plus scheduled
    applies). Use this only if you want Telegram Q&A without the scheduler.
    Set telegram.mode: listener so `run` defers questions to the listener.
    """
    config = load_config(config_path)
    if not config.telegram.enabled:
        console.print("[yellow]telegram.enabled is false in config.yaml.[/yellow]")
        return
    try:
        asyncio.run(_messenger_listen(config, channel="telegram"))
    except KeyboardInterrupt:
        console.print("\n[dim]Listener stopped.[/dim]")


@main.command("memory")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default="config.yaml"
)
def memory_cmd(config_path: Path) -> None:
    """Show saved review decisions, preferences, and question answers."""
    config = load_config(config_path)
    data = load_memory(config.base_dir, config)
    decisions = data.get("decisions", {})
    console.print(f"[bold]Decisions recorded:[/bold] {len(decisions)}")
    for key, meta in list(decisions.items())[-10:]:
        console.print(f"  {meta.get('status', '?')}: {meta.get('title', key)} @ {meta.get('company', '')}")
    prefs = data.get("preferences", {})
    if prefs:
        console.print(f"\n[bold]Preferences:[/bold] {prefs}")
    qa = data.get("question_answers", {})
    console.print(f"\n[bold]Saved question answers:[/bold] {len(qa)}")
    for entry in list(qa.values())[-5:]:
        if isinstance(entry, dict):
            q = str(entry.get("question", ""))
            suffix = "…" if len(q) > 70 else ""
            console.print(f"  Q: {q[:70]}{suffix}")
    pending = pending_count(config.base_dir, config)
    review_n = saved_answers_needing_review_count(config.base_dir)
    if pending:
        console.print(f"\n[yellow]Pending questions (need answers):[/yellow] {pending}")
        console.print("Run: [bold]python3 main.py answer-questions[/bold]")
    if review_n:
        console.print(f"\n[yellow]Saved answers needing review:[/yellow] {review_n}")
        console.print("Run: [bold]python3 main.py answer-questions --review[/bold]")


if __name__ == "__main__":
    main()

"""Long-running HTTP server that re-runs the apply flow on a fixed schedule.

Turns the laptop into a small always-on service: a FastAPI app served by
uvicorn that, on a background timer, runs the same apply flow as
``python main.py run`` every N minutes (default 30) for the enabled platforms
(currently naukri, hirist, instahyre).

When Telegram or WhatsApp is enabled in listener mode (``telegram.mode: listener``
or ``whatsapp.mode: listener``), the server also runs that messenger listener as
a second background task — no separate ``telegram-listen`` / ``whatsapp-listen``
process needed. The listener asks pending questions, applies as you reply, and
loops until nothing is left. The scheduled apply cycle and the listener's
re-apply share a single lock, so two browser apply-sessions never run at once.

Endpoints:
  GET  /            → scheduler status (JSON)
  GET  /status      → scheduler status (JSON)
  GET  /health      → liveness probe
  POST /run-now     → trigger an apply cycle immediately (skipped if one is running)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

logger = logging.getLogger("job_apply")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SchedulerState:
    config_path: Path
    platform: str
    interval_minutes: int
    verbose: bool
    run_on_start: bool
    is_running: bool = False
    run_count: int = 0
    error_count: int = 0
    last_started_at: Optional[str] = None
    last_finished_at: Optional[str] = None
    last_duration_seconds: Optional[float] = None
    last_error: Optional[str] = None
    next_run_at: Optional[str] = None
    listener_active: bool = False
    # created once the event loop is running (avoids binding to no loop on 3.9)
    apply_lock: Optional[asyncio.Lock] = None
    stop_event: Optional[asyncio.Event] = None
    loop_task: Optional[asyncio.Task] = None
    listener_task: Optional[asyncio.Task] = None

    def snapshot(self) -> dict:
        from .cli import server_listener_channel

        channel = server_listener_channel()
        return {
            "platform": self.platform,
            "interval_minutes": self.interval_minutes,
            "is_running": self.is_running,
            "run_count": self.run_count,
            "error_count": self.error_count,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_duration_seconds": self.last_duration_seconds,
            "last_error": self.last_error,
            "next_run_at": None if self.is_running else self.next_run_at,
            "messenger_listener_active": self.listener_active,
            "messenger_channel": channel,
            # backward compatibility
            "whatsapp_listener_active": self.listener_active,
        }


async def _run_one_cycle(state: SchedulerState) -> None:
    """Run a single apply cycle. Cycles never overlap, and the apply work is
    serialized against the WhatsApp listener's re-apply via ``apply_lock``."""
    from .cli import _run

    if state.is_running:
        logger.info("Scheduler: an apply cycle is already running — skipping this trigger.")
        return

    assert state.apply_lock is not None
    state.is_running = True
    state.last_started_at = _now_iso()
    state.last_error = None
    started = asyncio.get_event_loop().time()
    logger.info("Scheduler: starting apply cycle (platform=%s)", state.platform)
    try:
        async with state.apply_lock:
            await _run(state.config_path, state.platform, state.verbose)
    except asyncio.CancelledError:
        raise
    except SystemExit as exc:
        state.error_count += 1
        state.last_error = f"aborted (prerequisite/exit): {exc}"
        logger.error("Scheduler: apply cycle aborted: %s", exc)
    except Exception as exc:  # noqa: BLE001 - keep the scheduler alive across failures
        state.error_count += 1
        state.last_error = f"{type(exc).__name__}: {exc}"
        logger.exception("Scheduler: apply cycle failed")
    finally:
        state.run_count += 1
        state.last_finished_at = _now_iso()
        state.last_duration_seconds = round(asyncio.get_event_loop().time() - started, 1)
        state.is_running = False
        logger.info(
            "Scheduler: apply cycle finished in %ss (run #%d, errors=%d)",
            state.last_duration_seconds,
            state.run_count,
            state.error_count,
        )


async def _scheduler_loop(state: SchedulerState) -> None:
    interval_seconds = max(60, state.interval_minutes * 60)
    if state.run_on_start:
        await _run_one_cycle(state)
    while True:
        state.next_run_at = (
            datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)
        ).isoformat()
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break
        await _run_one_cycle(state)


def create_app(
    *,
    config_path: Path,
    platform: str = "all",
    interval_minutes: int = 30,
    verbose: bool = False,
    run_on_start: bool = True,
) -> FastAPI:
    state = SchedulerState(
        config_path=Path(config_path),
        platform=platform,
        interval_minutes=interval_minutes,
        verbose=verbose,
        run_on_start=run_on_start,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .config import load_config

        state.apply_lock = asyncio.Lock()
        state.stop_event = asyncio.Event()
        state.loop_task = asyncio.create_task(_scheduler_loop(state))

        config = load_config(state.config_path)
        from .cli import (
            _active_messenger,
            _messenger_listen,
            _messenger_mode,
            set_server_listener_channel,
        )

        channel = _active_messenger(config)
        if channel and _messenger_mode(config, channel) == "listener":
            state.listener_active = True
            set_server_listener_channel(channel)
            state.listener_task = asyncio.create_task(
                _messenger_listen(
                    config,
                    channel=channel,
                    apply_lock=state.apply_lock,
                    stop_event=state.stop_event,
                )
            )
            logger.info("%s listener started: will ask + apply replies until done.", channel)
        elif channel:
            logger.info(
                "%s mode=%s (not 'listener') — questions handled inline per cycle; "
                "no continuous listener.",
                channel,
                _messenger_mode(config, channel),
            )

        logger.info(
            "Jobs auto-apply server up: platform=%s, every %d min (run_on_start=%s)",
            platform,
            interval_minutes,
            run_on_start,
        )
        try:
            yield
        finally:
            set_server_listener_channel(None)
            if state.stop_event:
                state.stop_event.set()
            tasks = [t for t in (state.loop_task, state.listener_task) if t]
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            logger.info("Jobs auto-apply server stopped.")

    app = FastAPI(title="Jobs Auto-Apply Scheduler", lifespan=lifespan)
    app.state.scheduler = state

    @app.get("/")
    @app.get("/status")
    def status() -> dict:
        return state.snapshot()

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/run-now")
    async def run_now() -> JSONResponse:
        if state.is_running:
            return JSONResponse(
                status_code=409,
                content={"started": False, "reason": "an apply cycle is already running"},
            )
        asyncio.create_task(_run_one_cycle(state))
        return JSONResponse(
            content={"started": True, "platform": state.platform, "at": _now_iso()}
        )

    return app

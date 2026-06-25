"""Unofficial WhatsApp Web bridge for answering pending questions.

Drives web.whatsapp.com with Playwright (the same engine the apply flows use),
so there is no server, webhook, or tunnel to run. Link the device once by
scanning a QR code (``python main.py whatsapp-login``); the session is stored in
a dedicated persistent profile and reused on later runs.

WARNING: Automating WhatsApp Web violates WhatsApp's Terms of Service and can get
the linked number banned. This is opt-in (``whatsapp.enabled``) and intended for a
personal number you accept that risk on.
"""

from __future__ import annotations

from .pending_questions import (
    PENDING_REPLY_DROP,
    PENDING_REPLY_IGNORE,
    PENDING_REPLY_SKIP,
    parse_pending_reply,
)

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

logger = logging.getLogger("job_apply")

WHATSAPP_URL = "https://web.whatsapp.com"

# WhatsApp Web markup changes often; keep several fallbacks per element.
_LOGGED_IN_SELECTORS = (
    "div[aria-label='Chat list']",
    "#pane-side",
    "div[data-testid='chat-list']",
)
_QR_SELECTORS = (
    "canvas[aria-label*='scan' i]",
    "div[data-ref] canvas",
    "div[data-testid='qrcode']",
)
_COMPOSER_SELECTORS = (
    "footer div[contenteditable='true'][data-tab='10']",
    "footer div[contenteditable='true']",
    "div[contenteditable='true'][data-tab='10']",
)
# Message bubbles are read via _read_messages() (DOM evaluate with fallbacks),
# counting ANY new bubble after we send — not just incoming — so this also works
# in the "Message yourself" chat, where every message (including the reply you
# type on your phone) renders as an outgoing bubble.


class WhatsAppError(RuntimeError):
    """Raised when WhatsApp Web cannot be reached or driven."""


class WhatsAppClient:
    """A thin Playwright wrapper around a single WhatsApp Web conversation."""

    def __init__(
        self,
        *,
        profile_dir: Path,
        phone: str,
        headless: bool = False,
        reply_timeout_seconds: int = 900,
        poll_interval_seconds: int = 5,
        login_timeout_seconds: int = 180,
        skip_keyword: str = "skip",
        drop_keyword: str = "drop",
        ignore_keyword: str = "ignore",
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.phone = "".join(ch for ch in str(phone) if ch.isdigit())
        self.headless = headless
        self.reply_timeout_seconds = max(30, int(reply_timeout_seconds))
        self.poll_interval_seconds = max(2, int(poll_interval_seconds))
        self.login_timeout_seconds = max(30, int(login_timeout_seconds))
        self.skip_keyword = (skip_keyword or "skip").strip().lower()
        self.drop_keyword = (drop_keyword or "drop").strip().lower()
        self.ignore_keyword = (ignore_keyword or "ignore").strip().lower()
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._pw = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._context is not None:
            return
        if not self.phone:
            raise WhatsAppError(
                "whatsapp.phone is not set — add your number (with country code) to config.yaml"
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            ignore_default_args=["--enable-automation"],
        )
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

    async def close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:
            pass
        finally:
            self._context = None
            self._page = None
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise WhatsAppError("WhatsApp client not started")
        return self._page

    # ── login ────────────────────────────────────────────────────────────────
    async def _is_logged_in(self) -> bool:
        for sel in _LOGGED_IN_SELECTORS:
            try:
                if await self.page.query_selector(sel):
                    return True
            except Exception:
                continue
        return False

    async def is_logged_in(self) -> bool:
        """Public check: is WhatsApp Web currently showing a linked session?"""
        return await self._is_logged_in()

    async def ensure_logged_in(self, *, interactive: bool = True) -> bool:
        """Open WhatsApp Web; if not linked, prompt to scan the QR and wait."""
        page = self.page
        try:
            await page.goto(WHATSAPP_URL, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeoutError:
            pass

        deadline = asyncio.get_event_loop().time() + self.login_timeout_seconds
        announced_qr = False
        while True:
            if await self._is_logged_in():
                logger.info("WhatsApp Web session is linked.")
                # Give WhatsApp a moment to flush multi-device keys to the profile
                # so the link survives the next launch (avoids QR re-scan).
                await asyncio.sleep(3)
                return True
            qr_present = False
            for sel in _QR_SELECTORS:
                try:
                    if await page.query_selector(sel):
                        qr_present = True
                        break
                except Exception:
                    continue
            if qr_present and not announced_qr:
                announced_qr = True
                if not interactive:
                    return False
                print(
                    "\nScan the QR code in the WhatsApp Web window with your phone:\n"
                    "  WhatsApp → Settings → Linked Devices → Link a Device\n"
                )
            if asyncio.get_event_loop().time() > deadline:
                return False
            await asyncio.sleep(2)

    # ── conversation ─────────────────────────────────────────────────────────
    async def open_chat(self) -> None:
        """Open the conversation with the configured phone number."""
        page = self.page
        url = f"{WHATSAPP_URL}/send?phone={self.phone}&type=phone_number&app_absent=0"
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for _ in range(30):
            for sel in _COMPOSER_SELECTORS:
                try:
                    box = await page.query_selector(sel)
                    if box:
                        return
                except Exception:
                    continue
            # "Phone number shared via url is invalid" / "Starting chat" overlays
            await asyncio.sleep(1)
        raise WhatsAppError(
            f"Could not open WhatsApp chat for {self.phone} (composer not found)."
        )

    async def _composer(self):
        for sel in _COMPOSER_SELECTORS:
            try:
                box = await self.page.query_selector(sel)
                if box:
                    return box
            except Exception:
                continue
        return None

    async def send(self, text: str) -> None:
        page = self.page
        box = await self._composer()
        if box is None:
            await self.open_chat()
            box = await self._composer()
        if box is None:
            raise WhatsAppError("WhatsApp message composer not found")
        await box.click()
        # Multi-line messages: Shift+Enter keeps the line, Enter sends.
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if i:
                await page.keyboard.down("Shift")
                await page.keyboard.press("Enter")
                await page.keyboard.up("Shift")
            await page.keyboard.type(line)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.6)

    async def _read_messages(self) -> tuple[int, str]:
        """Return (message_count, latest_text) from the open conversation.

        Uses several selector fallbacks because WhatsApp Web's class names are
        obfuscated and change often; `div[role='row']` and `.copyable-text` are
        the most stable anchors.
        """
        try:
            result = await self.page.evaluate(
                """() => {
                    const main = document.querySelector('#main');
                    if (!main) return {count: 0, last: ''};
                    let rows = main.querySelectorAll('div.message-in, div.message-out');
                    if (!rows.length) rows = main.querySelectorAll('div.copyable-text');
                    if (!rows.length) rows = main.querySelectorAll('div[role="row"]');
                    let last = '';
                    for (let i = rows.length - 1; i >= 0; i--) {
                        const span = rows[i].querySelector('span.selectable-text');
                        const t = ((span ? span.innerText : rows[i].innerText) || '').trim();
                        if (t) { last = t; break; }
                    }
                    return {count: rows.length, last};
                }"""
            )
            return int(result.get("count", 0)), str(result.get("last", "")).strip()
        except Exception as exc:
            logger.debug("WhatsApp message read failed: %s", exc)
            return 0, ""

    async def _message_count(self) -> int:
        count, _ = await self._read_messages()
        return count

    async def _latest_message_text(self) -> str:
        _, last = await self._read_messages()
        return last

    async def wait_for_reply(self, *, baseline: int, timeout: int | None = None) -> str | None:
        """Poll until a new message bubble appears after ours; return its text."""
        timeout = timeout or self.reply_timeout_seconds
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(self.poll_interval_seconds)
            count, last = await self._read_messages()
            if count > baseline:
                return last
        return None

    async def ask(self, text: str, *, timeout: int | None = None) -> str | None:
        """Send a question and block until the user replies. Returns the reply.

        Returns None on timeout, and "" (empty) when the user replies the skip
        keyword. The baseline is captured AFTER our message renders, so the next
        new bubble (your reply, even in a self-chat) is detected.
        """
        await self.send(text)
        await asyncio.sleep(1.0)
        baseline = await self._message_count()
        reply = await self.wait_for_reply(baseline=baseline, timeout=timeout)
        if reply is None:
            return None
        parsed = parse_pending_reply(
            reply,
            skip_keyword=self.skip_keyword,
            drop_keyword=self.drop_keyword,
            ignore_keyword=self.ignore_keyword,
        )
        if parsed == PENDING_REPLY_SKIP:
            return PENDING_REPLY_SKIP
        if parsed == PENDING_REPLY_DROP:
            return PENDING_REPLY_DROP
        if parsed == PENDING_REPLY_IGNORE:
            return PENDING_REPLY_IGNORE
        return parsed


@asynccontextmanager
async def whatsapp_client(config) -> AsyncIterator[WhatsAppClient]:
    """Context manager that starts a linked WhatsApp client or raises."""
    client = WhatsAppClient(
        profile_dir=config.whatsapp_profile_path,
        phone=config.whatsapp.phone,
        headless=config.whatsapp.headless,
        reply_timeout_seconds=config.whatsapp.reply_timeout_seconds,
        poll_interval_seconds=config.whatsapp.poll_interval_seconds,
        login_timeout_seconds=config.whatsapp.login_timeout_seconds,
        skip_keyword=config.whatsapp.skip_keyword,
        drop_keyword=config.whatsapp.drop_keyword,
        ignore_keyword=config.whatsapp.ignore_keyword,
    )
    await client.start()
    try:
        if not await client.ensure_logged_in():
            raise WhatsAppError(
                "WhatsApp Web is not linked. Run: python main.py whatsapp-login"
            )
        await client.open_chat()
        yield client
    finally:
        await client.close()

"""Telegram Bot API bridge for answering pending questions.

Official, free, and safe: sends questions to your Telegram bot and reads your
replies via long polling (``getUpdates``). No server, webhook, or tunnel — the
app just calls Telegram's HTTPS API — and no ToS/ban risk.

A dedicated background task runs ``getUpdates`` continuously and enqueues
incoming messages. Apply / listener logic consumes replies from that queue, so
polling never blocks on waiting for a specific answer and replies sent while a
browser apply is running are not lost.

Setup:
  1. Create a bot with @BotFather and copy the token into ``telegram.bot_token``.
  2. Run ``python main.py telegram-login`` and send /start to your bot.

This exposes the same ``send`` / ``ask`` / ``is_logged_in`` interface as the
WhatsApp client so the pending-question flow can use either transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .pending_questions import (
    PENDING_REPLY_DROP,
    PENDING_REPLY_IGNORE,
    PENDING_REPLY_SKIP,
    parse_pending_reply,
)

logger = logging.getLogger("job_apply")

_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    """Raised when the Telegram Bot API cannot be reached or is misconfigured."""


def _post(token: str, method: str, params: dict[str, Any] | None, timeout: int) -> dict[str, Any]:
    url = _API.format(token=token, method=method)
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class TelegramClient:
    # Telegram exposes which message a reply quotes (``reply_to_message``), so the
    # pending-question flow can route out-of-order replies to the right question
    # instead of matching them by send order.
    supports_reply_routing = True

    # A Telegram bot token never expires, so the session can't "end" the way a
    # WhatsApp QR link can. A failing ``is_logged_in`` here is a transient network
    # blip, not a dead session — the listener should retry instead of giving up.
    session_can_expire = False

    def __init__(
        self,
        *,
        token: str,
        chat_id: str = "",
        chat_id_path: Path | None = None,
        offset_path: Path | None = None,
        reply_timeout_seconds: int = 900,
        skip_keyword: str = "skip",
        drop_keyword: str = "drop",
        ignore_keyword: str = "ignore",
        pending_keyword: str = "pending",
    ) -> None:
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.chat_id_path = chat_id_path
        self.offset_path = offset_path
        self.reply_timeout_seconds = max(30, int(reply_timeout_seconds))
        self.skip_keyword = (skip_keyword or "skip").strip().lower()
        self.drop_keyword = (drop_keyword or "drop").strip().lower()
        self.ignore_keyword = (ignore_keyword or "ignore").strip().lower()
        self.pending_keyword = (pending_keyword or "pending").strip().lower()
        self._offset: int | None = None
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._poll_task: asyncio.Task[None] | None = None

    # ── low-level ────────────────────────────────────────────────────────────
    async def _api(self, method: str, params: dict[str, Any] | None = None, *, timeout: int = 30) -> dict[str, Any]:
        return await asyncio.to_thread(_post, self.token, method, params, timeout)

    def _load_saved_chat_id(self) -> str:
        if self.chat_id_path and self.chat_id_path.exists():
            try:
                data = json.loads(self.chat_id_path.read_text(encoding="utf-8"))
                return str(data.get("chat_id", "")).strip()
            except Exception:
                return ""
        return ""

    def _save_chat_id(self, chat_id: str) -> None:
        if not self.chat_id_path:
            return
        try:
            self.chat_id_path.parent.mkdir(parents=True, exist_ok=True)
            self.chat_id_path.write_text(json.dumps({"chat_id": chat_id}), encoding="utf-8")
        except Exception as exc:
            logger.debug("Could not save telegram chat_id: %s", exc)

    def _load_saved_offset(self) -> int | None:
        if self.offset_path and self.offset_path.exists():
            try:
                data = json.loads(self.offset_path.read_text(encoding="utf-8"))
                value = data.get("offset")
                return int(value) if value is not None else None
            except Exception:
                return None
        return None

    def _save_offset(self, offset: int) -> None:
        if not self.offset_path:
            return
        try:
            self.offset_path.parent.mkdir(parents=True, exist_ok=True)
            self.offset_path.write_text(json.dumps({"offset": int(offset)}), encoding="utf-8")
        except Exception as exc:
            logger.debug("Could not save telegram offset: %s", exc)

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self.token:
            raise TelegramError("telegram.bot_token is not set — create a bot with @BotFather and add the token")
        me = await self._api("getMe")
        if not me.get("ok"):
            raise TelegramError(f"Invalid Telegram bot token: {me}")
        if not self.chat_id:
            self.chat_id = self._load_saved_chat_id()
        # Resume from where a previous run left off so a `serve --reload` restart
        # doesn't skip replies that arrived while the worker was reloading. Only
        # drain pre-existing backlog on the very first run (no saved offset),
        # so an old message is never mistaken for a fresh answer.
        saved = self._load_saved_offset()
        if saved is not None:
            self._offset = saved
        else:
            await self._drain_updates()
            if self._offset is not None:
                self._save_offset(self._offset)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="telegram-poll")

    async def close(self) -> None:  # symmetry with WhatsAppClient
        task = self._poll_task
        self._poll_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def is_logged_in(self) -> bool:
        try:
            return bool((await self._api("getMe")).get("ok"))
        except Exception:
            return False

    async def open_chat(self) -> None:
        if not self.chat_id:
            raise TelegramError("No Telegram chat_id yet. Run: python main.py telegram-login (then /start your bot)")

    async def bot_username(self) -> str:
        try:
            me = await self._api("getMe")
            return str((me.get("result") or {}).get("username", ""))
        except Exception:
            return ""

    # ── updates / replies ────────────────────────────────────────────────────
    async def _drain_updates(self) -> None:
        """Skip past any messages already waiting so they aren't treated as replies."""
        try:
            res = await self._api("getUpdates", {"timeout": 0}, timeout=15)
        except Exception as exc:
            logger.debug("telegram getUpdates(drain) failed: %s", exc)
            return
        updates = res.get("result", []) if res.get("ok") else []
        if updates:
            self._offset = int(updates[-1]["update_id"]) + 1

    async def capture_chat_id(self, *, timeout: int = 120) -> str | None:
        """Wait for the next incoming message and record its chat_id."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            _text, chat = await self._next_message(long_poll=20)
            if chat:
                self.chat_id = chat
                self._save_chat_id(chat)
                return chat
        return None

    @staticmethod
    def _parse_text_update(upd: dict[str, Any]) -> dict[str, Any] | None:
        """Parse a Telegram update into ``{text, chat, reply_to}`` or None."""
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = msg.get("text")
        if not text:
            return None
        chat = str((msg.get("chat") or {}).get("id", "")).strip()
        reply_to_raw = (msg.get("reply_to_message") or {}).get("message_id")
        return {
            "text": str(text).strip(),
            "chat": chat,
            "reply_to": int(reply_to_raw) if reply_to_raw else None,
        }

    async def _poll_loop(self) -> None:
        """Dedicated ``getUpdates`` loop — runs until ``close()`` cancels it."""
        long_poll = 45
        try:
            while True:
                params: dict[str, Any] = {"timeout": long_poll}
                if self._offset is not None:
                    params["offset"] = self._offset
                try:
                    res = await self._api("getUpdates", params, timeout=long_poll + 15)
                except Exception as exc:
                    logger.debug("telegram poll loop getUpdates failed: %s", exc)
                    await asyncio.sleep(2)
                    continue
                if not res.get("ok"):
                    await asyncio.sleep(2)
                    continue
                for upd in res.get("result", []):
                    self._offset = int(upd["update_id"]) + 1
                    self._save_offset(self._offset)
                    parsed = self._parse_text_update(upd)
                    if parsed is not None:
                        await self._incoming.put(parsed)
        except asyncio.CancelledError:
            raise

    async def _next_update(self, *, long_poll: int) -> dict[str, Any] | None:
        """Dequeue the next text message from the background poll task."""
        try:
            return await asyncio.wait_for(self._incoming.get(), timeout=long_poll)
        except asyncio.TimeoutError:
            return None

    async def _next_message(self, *, long_poll: int) -> tuple[str | None, str]:
        upd = await self._next_update(long_poll=long_poll)
        if upd is None:
            return None, ""
        return upd["text"], upd["chat"]

    async def wait_for_reply(self, *, timeout: int | None = None) -> str | None:
        upd = await self.wait_for_reply_routed(timeout=timeout)
        return upd["text"] if upd else None

    async def wait_for_reply_routed(self, *, timeout: int | None = None) -> dict[str, Any] | None:
        """Like ``wait_for_reply`` but also reports which message was replied to.

        Returns ``{text, reply_to}`` (``reply_to`` may be None) or None on timeout.
        Reads from the background poll queue so apply work and polling stay concurrent.
        """
        timeout = timeout or self.reply_timeout_seconds
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                upd = await asyncio.wait_for(self._incoming.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if self.chat_id and upd["chat"] and upd["chat"] != self.chat_id:
                continue  # message from a different chat — ignore
            return {"text": upd["text"], "reply_to": upd["reply_to"]}
        return None

    async def requeue_routed_reply(self, upd: dict[str, Any]) -> None:
        """Put a routed reply back on the poll queue for the active listener session."""
        await self._incoming.put(
            {
                "text": upd.get("text", ""),
                "chat": self.chat_id or "",
                "reply_to": upd.get("reply_to"),
            }
        )

    # ── messaging ────────────────────────────────────────────────────────────
    async def send(self, text: str, *, reply_to_message_id: int | None = None) -> int | None:
        """Send a message and return its Telegram ``message_id`` (None if missing).

        Pass ``reply_to_message_id`` to quote a previous message, so the user sees
        the reply threaded under the question it answers.
        """
        if not self.chat_id:
            raise TelegramError("No Telegram chat_id to send to.")
        params: dict[str, Any] = {"chat_id": self.chat_id, "text": text}
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = reply_to_message_id
        res = await self._api("sendMessage", params)
        if not res.get("ok"):
            raise TelegramError(f"Telegram sendMessage failed: {res}")
        message_id = (res.get("result") or {}).get("message_id")
        return int(message_id) if message_id else None

    async def ask(self, text: str, *, timeout: int | None = None) -> str | None:
        """Send a question and wait for the reply. None on timeout, "" on skip."""
        await self.send(text)
        reply = await self.wait_for_reply(timeout=timeout)
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
async def telegram_client(config) -> AsyncIterator[TelegramClient]:
    client = TelegramClient(
        token=config.telegram.bot_token,
        chat_id=config.telegram.chat_id,
        chat_id_path=config.telegram_chat_path,
        offset_path=config.telegram_offset_path,
        reply_timeout_seconds=config.telegram.reply_timeout_seconds,
        skip_keyword=config.telegram.skip_keyword,
        drop_keyword=config.telegram.drop_keyword,
        ignore_keyword=config.telegram.ignore_keyword,
        pending_keyword=config.telegram.pending_keyword,
    )
    await client.start()
    try:
        await client.open_chat()
        yield client
    finally:
        await client.close()

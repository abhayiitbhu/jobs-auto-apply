from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import BrowserContext


def load_cookies(path: Path, *, default_domain: str, required_names: list[str] | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Cookie file not found: {path}\nExport cookies from your browser while logged in. See README."
        )

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "cookies" in raw:
        cookies = raw["cookies"]
    elif isinstance(raw, list):
        cookies = raw
    else:
        raise ValueError(f"{path.name} must be a list of cookie objects or {{cookies: [...]}}")

    normalized: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict) or "name" not in cookie or "value" not in cookie:
            continue
        entry: dict[str, Any] = {
            "name": cookie["name"],
            "value": cookie["value"],
            "domain": cookie.get("domain", default_domain),
            "path": cookie.get("path", "/"),
        }
        if "secure" in cookie:
            entry["secure"] = bool(cookie["secure"])
        if "httpOnly" in cookie:
            entry["httpOnly"] = bool(cookie["httpOnly"])
        if cookie.get("sameSite"):
            entry["sameSite"] = cookie["sameSite"]
        if cookie.get("expires"):
            entry["expires"] = cookie["expires"]
        normalized.append(entry)

    if required_names:
        present = {c["name"] for c in normalized}
        missing = [name for name in required_names if name not in present]
        if missing and not any(name in present for name in required_names):
            raise ValueError(f"Missing session cookies in {path.name}. Expected one of: {', '.join(required_names)}")
    return normalized


async def inject_cookies(context: BrowserContext, cookies_path: Path, **kwargs: Any) -> None:
    cookies = load_cookies(cookies_path, **kwargs)
    await context.add_cookies(cookies)


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], " ".join(parts[1:])


def is_external_career_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    internal = ("wellfound.com", "uplers.com", "platform.uplers.com")
    return not any(domain in host for domain in internal)


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")

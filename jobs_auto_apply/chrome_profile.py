from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from .config import AppConfig


def default_chrome_user_data_root() -> Path:
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return home / "Library/Application Support/Google/Chrome"
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        return Path(local) / "Google" / "Chrome" / "User Data"
    return home / ".config" / "google-chrome"


def _is_profile_directory(path: Path) -> bool:
    return path.is_dir() and (path / "Preferences").exists()


def resolve_chrome_profile_dir(config: AppConfig) -> Path:
    """Resolve the Chrome profile directory to pass to launch_persistent_context."""
    custom = config.browser.chrome_user_data_dir.strip()
    profile_name = config.browser.chrome_profile_name.strip() or "Default"

    if custom:
        base = Path(custom).expanduser().resolve()
        if _is_profile_directory(base):
            return base
        candidate = base / profile_name
        if _is_profile_directory(candidate):
            return candidate
        raise FileNotFoundError(
            f"Chrome profile not found at {base} or {candidate}. "
            "Set browser.chrome_user_data_dir to your Chrome 'User Data' folder "
            "or directly to a profile folder (Default, Profile 1, ...)."
        )

    root = default_chrome_user_data_root()
    candidate = root / profile_name
    if not _is_profile_directory(candidate):
        raise FileNotFoundError(
            f"Chrome profile not found: {candidate}\n"
            f"Set browser.chrome_profile_name (e.g. Default, 'Profile 1') or "
            f"browser.chrome_user_data_dir in config.yaml."
        )
    return candidate


class ChromeProfileLockedError(RuntimeError):
    """Raised when Chrome is still running and has the profile locked.

    A subclass of ``RuntimeError`` so existing ``except Exception`` handlers keep
    working, while callers (e.g. the scheduler) can detect this specific case and
    retry quickly once Chrome is closed instead of waiting a full interval.
    """


# Module-level flag so the scheduler can tell whether a just-finished apply cycle
# was wasted purely because Chrome's profile was locked. Reset at the start of a
# run, set whenever ``assert_chrome_profile_available`` raises.
_chrome_lock_detected = False


def reset_chrome_lock_flag() -> None:
    global _chrome_lock_detected
    _chrome_lock_detected = False


def chrome_lock_was_detected() -> bool:
    return _chrome_lock_detected


def chrome_profile_lock_path(profile_dir: Path) -> Path:
    return profile_dir.parent / "SingletonLock"


def is_chrome_profile_locked(profile_dir: Path) -> bool:
    lock = chrome_profile_lock_path(profile_dir)
    return lock.exists() or lock.is_symlink()


def is_chrome_process_running() -> bool:
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ["pgrep", "-x", "Google Chrome"],
                capture_output=True,
                check=False,
            )
            return result.returncode == 0
        if system == "Windows":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                capture_output=True,
                text=True,
                check=False,
            )
            return "chrome.exe" in result.stdout.lower()
        result = subprocess.run(
            ["pgrep", "-x", "chrome"],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def assert_chrome_profile_available(profile_dir: Path) -> None:
    if not profile_dir.exists():
        raise FileNotFoundError(f"Chrome profile directory does not exist: {profile_dir}")

    if is_chrome_process_running() or is_chrome_profile_locked(profile_dir):
        global _chrome_lock_detected
        _chrome_lock_detected = True
        raise ChromeProfileLockedError(
            "Google Chrome is still running. Quit Chrome completely before running the script "
            "(Chrome locks its profile while open).\n"
            "  macOS: Cmd+Q Chrome, or run: osascript -e 'quit app \"Google Chrome\"'"
        )


def list_chrome_profiles() -> list[tuple[str, Path]]:
    root = default_chrome_user_data_root()
    if not root.exists():
        return []
    profiles: list[tuple[str, Path]] = []
    local_state = root / "Local State"
    if local_state.exists():
        import json

        try:
            data = json.loads(local_state.read_text(encoding="utf-8"))
            info_cache = data.get("profile", {}).get("info_cache", {})
            for folder, meta in info_cache.items():
                path = root / folder
                if _is_profile_directory(path):
                    name = str(meta.get("name", folder))
                    profiles.append((f"{name} ({folder})", path))
        except Exception:
            pass
    if not profiles:
        for path in sorted(root.iterdir()):
            if _is_profile_directory(path):
                profiles.append((path.name, path))
    return profiles

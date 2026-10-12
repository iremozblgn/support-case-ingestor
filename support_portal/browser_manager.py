from __future__ import annotations

import atexit
import errno
import logging
import threading
import time
from pathlib import Path
from typing import Any

from config import resolve_repo_path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX environments
    fcntl = None

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency in some environments
    psutil = None


class SupportPortalBrowserManagerError(Exception):
    """Browser manager failed to provide a usable persistent context."""

    def __init__(self, message: str, *, category: str = "browser_manager_error") -> None:
        super().__init__(message)
        self.category = category


_MANAGER_LOCK = threading.Lock()
_SCRAPE_LOCK = threading.Lock()
_PLAYWRIGHT: Any | None = None
_CONTEXT: Any | None = None
_PROFILE_DIR: str | None = None
_LAUNCHED_AT: float | None = None
_LAST_USED_AT: float | None = None
_BROWSER_PID: int | None = None
_LAST_LOCK_RECOVERED = False
_PROFILE_LOCK_HANDLE: Any | None = None
_LAST_CONTEXT_SETTINGS: dict[str, Any] = {}


def _logger(logger: logging.Logger | None = None) -> logging.Logger:
    return logger or logging.getLogger(__name__)


def _profile_lock_paths(profile_dir: Path) -> tuple[Path, ...]:
    return (
        profile_dir / "SingletonLock",
        profile_dir / "SingletonSocket",
        profile_dir / "SingletonCookie",
    )


def _profile_in_use_pids(profile_dir: Path) -> list[int]:
    if psutil is None:
        return []
    profile_text = str(profile_dir.resolve())
    matching_pids: list[int] = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = process.info.get("cmdline") or []
            if not cmdline:
                continue
            flattened = " ".join(str(part) for part in cmdline)
            if profile_text in flattened or f"--user-data-dir={profile_text}" in flattened:
                matching_pids.append(int(process.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, KeyError):
            continue
    return matching_pids


def _remove_stale_profile_locks(profile_dir: Path, logger: logging.Logger) -> tuple[bool, bool]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    pids = _profile_in_use_pids(profile_dir)
    if pids:
        logger.warning("profile_currently_in_use=true browser_pids=%s", pids[:10])
        return False, True

    removed_any = False
    for lock_path in _profile_lock_paths(profile_dir):
        try:
            if lock_path.exists() or lock_path.is_symlink():
                lock_path.unlink()
                removed_any = True
        except FileNotFoundError:
            continue
        except OSError:
            logger.debug("Failed to remove stale lock path %s", lock_path, exc_info=True)
    if removed_any:
        logger.info("removed_stale_profile_lock=true")
    return removed_any, False


def _find_browser_pid_for_profile(profile_dir: Path) -> int | None:
    pids = _profile_in_use_pids(profile_dir)
    return pids[0] if pids else None


def _clear_state() -> None:
    global _PLAYWRIGHT, _CONTEXT, _PROFILE_DIR, _LAUNCHED_AT, _LAST_USED_AT, _BROWSER_PID, _LAST_LOCK_RECOVERED, _LAST_CONTEXT_SETTINGS
    _PLAYWRIGHT = None
    _CONTEXT = None
    _PROFILE_DIR = None
    _LAUNCHED_AT = None
    _LAST_USED_AT = None
    _BROWSER_PID = None
    _LAST_LOCK_RECOVERED = False
    _LAST_CONTEXT_SETTINGS = {}


def _profile_lock_path(profile_dir: Path) -> Path:
    return profile_dir / ".support_portal_browser_manager.lock"


def _acquire_profile_lease(profile_dir: Path, logger: logging.Logger) -> None:
    global _PROFILE_LOCK_HANDLE
    if fcntl is None:
        return
    if _PROFILE_LOCK_HANDLE is not None:
        return
    lock_path = _profile_lock_path(profile_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            logger.warning("profile_currently_in_use=true lease_file=%s", lock_path)
            raise SupportPortalBrowserManagerError(
                f"Persistent profile lease is already held by another process: {profile_dir}",
                category="profile_busy",
            ) from exc
        raise
    _PROFILE_LOCK_HANDLE = handle


def _release_profile_lease(logger: logging.Logger) -> None:
    global _PROFILE_LOCK_HANDLE
    if _PROFILE_LOCK_HANDLE is None or fcntl is None:
        return
    try:
        fcntl.flock(_PROFILE_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
    except Exception:
        logger.debug("Ignoring profile lease unlock failure", exc_info=True)
    try:
        _PROFILE_LOCK_HANDLE.close()
    except Exception:
        logger.debug("Ignoring profile lease close failure", exc_info=True)
    _PROFILE_LOCK_HANDLE = None


def _context_profile_files(profile_dir: Path) -> tuple[Path, ...]:
    return (
        profile_dir / "Cookies",
        profile_dir / "Local State",
        profile_dir / "Default" / "Cookies",
        profile_dir / "Default" / "Local Storage",
        profile_dir / "Default" / "Network" / "Cookies",
    )


def _profile_state_mtimes(profile_dir: Path) -> dict[str, float]:
    mtimes: dict[str, float] = {}
    for path in _context_profile_files(profile_dir):
        try:
            if path.exists():
                mtimes[str(path.relative_to(profile_dir))] = path.stat().st_mtime
        except Exception:
            continue
    return mtimes


def _profile_owner_pid_if_any(profile_dir: Path) -> int | None:
    pids = _profile_in_use_pids(profile_dir)
    return pids[0] if pids else None


def _launch_args(settings: Any) -> list[str]:
    base_args = ["--start-maximized", "--no-sandbox"]
    extra_args = list(getattr(settings, "support_portal_browser_launch_args", []) or [])
    return base_args + [arg for arg in extra_args if arg not in base_args]


def get_browser_entrypoint_diagnostics(settings: Any) -> dict[str, Any]:
    profile_dir = resolve_repo_path(getattr(settings, "support_portal_browser_profile_dir", None))
    lease_path = _profile_lock_path(profile_dir)
    return {
        "resolved_browser_profile_dir": str(profile_dir),
        "persistent_context_used": True,
        "browser_executable_name": "chromium",
        "browser_headless": bool(getattr(settings, "support_portal_browser_headless", False)),
        "profile_exists_before_launch": profile_dir.exists(),
        "profile_owner_pid_if_any": _profile_owner_pid_if_any(profile_dir),
        "profile_lease_path": str(lease_path),
        "profile_lease_acquired": _PROFILE_LOCK_HANDLE is not None,
        "browser_launch_arguments": _launch_args(settings),
    }


def _close_context_locked(logger: logging.Logger, *, reason: str | None = None) -> None:
    global _PLAYWRIGHT, _CONTEXT
    profile_dir = Path(_PROFILE_DIR) if _PROFILE_DIR else None
    pre_close_mtimes = _profile_state_mtimes(profile_dir) if profile_dir else {}
    if reason:
        logger.info("browser_context_close_reason=%s", reason)
    if profile_dir is not None:
        logger.info("auth_state_flush_started=true resolved_browser_profile_dir=%s", profile_dir)
        try:
            if _CONTEXT is not None:
                _CONTEXT.pages[0].wait_for_timeout(750) if _CONTEXT.pages else None
        except Exception:
            logger.debug("Ignoring auth-state settle wait failure", exc_info=True)
        logger.info("auth_state_flush_completed=true tracked_profile_state_files=%s", sorted(pre_close_mtimes))
    try:
        if _CONTEXT is not None:
            logger.info("persistent_context_close_started=true")
            _CONTEXT.close()
            logger.info("persistent_context_close_completed=true")
    except Exception:
        logger.debug("Ignoring persistent context close failure", exc_info=True)
    try:
        if _PLAYWRIGHT is not None:
            _PLAYWRIGHT.stop()
            logger.info("playwright_stop_completed=true")
    except Exception:
        logger.debug("Ignoring Playwright stop failure", exc_info=True)
    if profile_dir is not None:
        post_close_mtimes = _profile_state_mtimes(profile_dir)
        changed_files = sorted(
            path
            for path, mtime in post_close_mtimes.items()
            if path not in pre_close_mtimes or pre_close_mtimes[path] != mtime
        )
        logger.info(
            "auth_state_files_touched=%s auth_state_files_known=%s",
            changed_files,
            sorted(post_close_mtimes),
        )
    _release_profile_lease(logger)
    logger.info("profile_lease_released=true")
    _clear_state()


def _is_context_alive_locked(logger: logging.Logger, *, touch: bool) -> bool:
    global _LAST_USED_AT
    if _CONTEXT is None:
        return False
    try:
        pages = list(_CONTEXT.pages)
        page = pages[0] if pages else _CONTEXT.new_page()
        result = page.evaluate("1+1")
        if result != 2:
            return False
        if touch:
            _LAST_USED_AT = time.time()
        return True
    except Exception:
        logger.debug("Persistent browser context liveness check failed", exc_info=True)
        return False


def is_context_alive(logger: logging.Logger | None = None) -> bool:
    local_logger = _logger(logger)
    with _MANAGER_LOCK:
        alive = _is_context_alive_locked(local_logger, touch=False)
        return alive


def _context_health_locked(logger: logging.Logger) -> dict[str, Any]:
    alive = _is_context_alive_locked(logger, touch=False)
    now = time.time()
    uptime = int(now - _LAUNCHED_AT) if _LAUNCHED_AT else 0
    return {
        "browser_pid": _BROWSER_PID,
        "browser_uptime_seconds": uptime,
        "browser_lock_recovered": _LAST_LOCK_RECOVERED,
        "browser_context_alive": alive,
        "active_pages_count": len(_CONTEXT.pages) if _CONTEXT is not None else 0,
        **_LAST_CONTEXT_SETTINGS,
    }


def get_context_health(logger: logging.Logger | None = None) -> dict[str, Any]:
    local_logger = _logger(logger)
    with _MANAGER_LOCK:
        return _context_health_locked(local_logger)


def get_or_create_context(settings: Any, logger: logging.Logger | None = None) -> tuple[Any, dict[str, Any]]:
    local_logger = _logger(logger)
    global _PLAYWRIGHT, _CONTEXT, _PROFILE_DIR, _LAUNCHED_AT, _LAST_USED_AT, _BROWSER_PID, _LAST_LOCK_RECOVERED, _LAST_CONTEXT_SETTINGS

    profile_dir = resolve_repo_path(settings.support_portal_browser_profile_dir)
    now = time.time()
    browser_context_reused = False
    browser_context_created = False
    browser_context_relaunch_required = False
    _LAST_CONTEXT_SETTINGS = get_browser_entrypoint_diagnostics(settings)

    with _MANAGER_LOCK:
        if _CONTEXT is not None:
            idle_timeout = max(0, int(getattr(settings, "support_portal_browser_idle_timeout_seconds", 600) or 0))
            if idle_timeout and _LAST_USED_AT and now - _LAST_USED_AT > idle_timeout:
                browser_context_relaunch_required = True
                _close_context_locked(local_logger, reason="idle_timeout")
            elif _PROFILE_DIR != str(profile_dir):
                browser_context_relaunch_required = True
                _close_context_locked(local_logger, reason="profile_changed")
            elif _is_context_alive_locked(local_logger, touch=True):
                browser_context_reused = True
                local_logger.info("browser_context_reused=true")
                health = _context_health_locked(local_logger)
                health.update(
                    {
                        "browser_context_reused": True,
                        "browser_context_created": False,
                        "browser_context_relaunch_required": browser_context_relaunch_required,
                    }
                )
                return _CONTEXT, health
            else:
                browser_context_relaunch_required = True
                _close_context_locked(local_logger, reason="stale_context")

        _LAST_LOCK_RECOVERED, profile_in_use = _remove_stale_profile_locks(profile_dir, local_logger)
        if profile_in_use:
            raise SupportPortalBrowserManagerError(
                f"Persistent profile already in use by another Chromium process: {profile_dir}",
                category="profile_busy",
            )
        _acquire_profile_lease(profile_dir, local_logger)
        _LAST_CONTEXT_SETTINGS["profile_lease_acquired"] = True

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - dependency issue
            raise SupportPortalBrowserManagerError("Playwright is not installed") from exc

        _PLAYWRIGHT = sync_playwright().start()
        try:
            _CONTEXT = _PLAYWRIGHT.chromium.launch_persistent_context(
                str(profile_dir),
                headless=settings.support_portal_browser_headless,
                args=_launch_args(settings),
                viewport=None,
            )
        except Exception as exc:
            try:
                _PLAYWRIGHT.stop()
            except Exception:
                local_logger.debug("Ignoring Playwright stop failure after launch error", exc_info=True)
            _release_profile_lease(local_logger)
            _clear_state()
            raise SupportPortalBrowserManagerError(
                f"BrowserType.launch_persistent_context failed for {profile_dir}: {exc}",
                category="browser_launch_failed",
            ) from exc

        _PROFILE_DIR = str(profile_dir)
        _LAUNCHED_AT = time.time()
        _LAST_USED_AT = _LAUNCHED_AT
        _BROWSER_PID = _find_browser_pid_for_profile(profile_dir)
        browser_context_created = True
        local_logger.info(
            "browser_context_created=true browser_context_relaunch_required=%s browser_pid=%s resolved_browser_profile_dir=%s browser_headless=%s persistent_context_used=true",
            browser_context_relaunch_required,
            _BROWSER_PID,
            profile_dir,
            settings.support_portal_browser_headless,
        )
        health = _context_health_locked(local_logger)
        health.update(
            {
                "browser_context_reused": browser_context_reused,
                "browser_context_created": browser_context_created,
                "browser_context_relaunch_required": browser_context_relaunch_required,
            }
        )
        return _CONTEXT, health


def close_context(logger: logging.Logger | None = None, *, reason: str | None = None) -> None:
    local_logger = _logger(logger)
    with _MANAGER_LOCK:
        _close_context_locked(local_logger, reason=reason)


def acquire_scrape_lease() -> bool:
    return _SCRAPE_LOCK.acquire(blocking=False)


def release_scrape_lease() -> None:
    if _SCRAPE_LOCK.locked():
        _SCRAPE_LOCK.release()


atexit.register(close_context)

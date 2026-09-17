"""Browser lifecycle management for ChatGPT automation."""

from __future__ import annotations

import os

from playwright.sync_api import BrowserContext, Page, sync_playwright

from config.selectors import PROMPT_BOX
from src.self_update import profile_dir


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionExpiredError(Exception):
    """Raised when the browser session is not authenticated."""


class GenerationTimeoutError(Exception):
    """Raised when image generation does not complete within the expected time."""


class RateLimitError(Exception):
    """Raised when ChatGPT signals a rate-limit or cooldown."""


class ProfileLockedError(Exception):
    """Raised when the browser profile is already in use by another Chromium instance."""


# ---------------------------------------------------------------------------
# Context management
# ---------------------------------------------------------------------------


def launch_context(account: str) -> BrowserContext:
    """Launch a persistent browser context for the given account.

    Prefers the REAL installed Google Chrome (Playwright ``channel="chrome"``)
    so the automation uses the same browser — and therefore the same network
    stack, proxy, VPN and certificate settings — that loads chatgpt.com fine in
    the operator's normal browser. The bundled Chromium is only a fallback for a
    machine that has no Chrome installed.

    Why this matters: on some PCs the bundled Chromium could not reach
    chatgpt.com (it stayed on about:blank) even though the user's normal Chrome
    opened it fine — a proxy/cert difference. Using the installed Chrome removes
    that difference.

    Override with the STUDIO_BROWSER_CHANNEL env var:
        "chrome"  (default) — use installed Google Chrome, fall back to Chromium
        "msedge"            — use installed Microsoft Edge, fall back to Chromium
        "chromium"          — force the bundled Chromium (old behaviour)

    Uses a per-account profile directory so cookies and session state are
    preserved across runs.

    Raises
    ------
    ProfileLockedError
        If the profile directory is already in use by another browser instance.
    """
    # Absolute, fixed profile path derived from the app directory (never
    # relative to the CWD, never inside the self-update-swapped code/ folder),
    # so the saved ChatGPT session persists across restarts AND updates.
    profile_path = profile_dir(account)
    print(f"[agent] using browser profile {profile_path}")

    channel = (os.environ.get("STUDIO_BROWSER_CHANNEL", "chrome") or "chrome").strip().lower()
    common_kwargs = dict(
        user_data_dir=str(profile_path),
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )

    def _grant(context: "BrowserContext") -> "BrowserContext":
        try:
            context.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin="https://chatgpt.com",
            )
        except Exception:
            pass
        return context

    try:
        pw = sync_playwright().start()
    except Exception:
        raise

    # Build the ordered list of things to try. A real channel first (if asked),
    # then the bundled Chromium as a safety net.
    attempts: list[tuple[str, dict]] = []
    if channel in ("chrome", "msedge"):
        attempts.append((channel, {**common_kwargs, "channel": channel}))
    attempts.append(("chromium", dict(common_kwargs)))  # bundled fallback

    last_exc: Exception | None = None
    for label, kwargs in attempts:
        try:
            print(f"[agent] launching browser channel={label!r}")
            context: BrowserContext = pw.chromium.launch_persistent_context(**kwargs)
            print(f"[agent] browser launched via {label!r}")
            return _grant(context)
        except Exception as exc:
            msg = str(exc).lower()
            # A profile lock is fatal for every channel (same profile dir), so
            # surface it clearly rather than falling through to the next attempt.
            if "already in use" in msg or "existing browser session" in msg or "lock" in msg:
                raise ProfileLockedError(
                    f"Profile '{profile_path}' is already in use by another browser instance. "
                    "Close any open automation browser windows and try again."
                ) from exc
            last_exc = exc
            print(f"[agent] channel {label!r} unavailable ({exc}); trying next option")

    # Every attempt failed for a non-lock reason (e.g. neither Chrome nor the
    # bundled Chromium could launch).
    raise last_exc if last_exc else RuntimeError("Could not launch any browser.")


# ---------------------------------------------------------------------------
# Session validation
# ---------------------------------------------------------------------------


def is_logged_in(page: Page) -> bool:
    """Check whether the page has an active ChatGPT session.

    Returns True if the prompt box is present and visible, indicating
    the user is authenticated and the editor is ready.
    """
    try:
        prompt = page.wait_for_selector(PROMPT_BOX, state="visible", timeout=10_000)
        return prompt is not None
    except Exception:
        return False


def require_login(page: Page, run_id: str) -> None:
    """Assert that the session is active; raise SessionExpiredError if not.

    Takes a screenshot before raising so the caller can inspect the
    login state visually.

    Parameters
    ----------
    page:
        The active page to check.
    run_id:
        Used to name the diagnostic screenshot.

    Raises
    ------
    SessionExpiredError
        Always raised when is_logged_in returns False.
    """
    if not is_logged_in(page):
        from pathlib import Path

        path = Path("logs") / f"{run_id}_session_expired.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(path))
        except Exception:
            pass
        raise SessionExpiredError(
            f"Session not authenticated for this profile. Screenshot saved to {path}"
        )

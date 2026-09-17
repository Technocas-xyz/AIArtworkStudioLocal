"""Browser lifecycle management for ChatGPT automation."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from config.selectors import PROMPT_BOX
from src.self_update import profile_dir, profiles_root


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
# Finding the real installed Chrome / Edge, and attaching over CDP
# ---------------------------------------------------------------------------


def _find_browser_exe(channel: str) -> str | None:
    """Locate the installed Chrome (or Edge) executable, or None if not found."""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    if channel == "msedge":
        candidates = [
            rf"{pf}\Microsoft\Edge\Application\msedge.exe",
            rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
        ]
    else:  # chrome
        candidates = [
            rf"{pf}\Google\Chrome\Application\chrome.exe",
            rf"{pf86}\Google\Chrome\Application\chrome.exe",
            rf"{local}\Google\Chrome\Application\chrome.exe" if local else "",
        ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cdp_ready(port: int, timeout: float = 25.0) -> bool:
    """Wait until Chrome's DevTools endpoint accepts connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def _attach_real_chrome(pw, account: str, channel: str) -> BrowserContext | None:
    """Start the REAL installed Chrome as a normal (non-Playwright-launched)
    process with remote debugging, then connect Playwright to it over CDP.

    This is the most anti-bot-resistant path: because Playwright does NOT launch
    the browser, none of the automation switches Cloudflare keys on are present —
    it is an ordinary Chrome the user is sitting in front of. We use a dedicated
    real-Chrome user-data-dir (under profiles/) so the saved ChatGPT session
    persists AND we never disturb the operator's day-to-day Chrome profile.

    Returns a BrowserContext on success, or None if Chrome could not be found /
    attached (so the caller can fall back to a launched context)."""
    exe = _find_browser_exe(channel)
    if not exe:
        print(f"[agent] attach: no installed {channel} found")
        return None

    # A dedicated, persistent, REAL-Chrome profile dir (distinct from the
    # Playwright automation profile). Kept under profiles/ so it survives
    # updates and is namespaced per account.
    udd = (profiles_root() / f"{account}_chrome").resolve()
    udd.mkdir(parents=True, exist_ok=True)
    port = _free_port()

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={udd}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        "https://chatgpt.com",
    ]
    print(f"[agent] attach: starting real {channel} on CDP port {port} (profile {udd})")
    try:
        # Detached so closing the app doesn't kill it mid-thought, and vice
        # versa; we connect over the network port, not via a child pipe.
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(args, creationflags=creationflags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        print(f"[agent] attach: could not start {channel}: {exc}")
        return None

    if not _cdp_ready(port):
        print("[agent] attach: Chrome DevTools endpoint did not come up in time")
        return None

    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception as exc:
        print(f"[agent] attach: connect_over_cdp failed: {exc}")
        return None

    # The real Chrome already has a default context; use it (it holds the
    # persistent profile's cookies, i.e. the saved ChatGPT session).
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    print("[agent] attach: connected to real Chrome over CDP")
    return context


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

    # Default to "attach": start the REAL installed Chrome and connect over CDP.
    # This is the most reliable against ChatGPT's Cloudflare anti-bot check,
    # which hard-blocks Playwright-launched browsers on some machines. Override
    # with STUDIO_BROWSER_CHANNEL:
    #   "attach"  (default) — start real Chrome + connect over CDP (least detectable)
    #   "chrome"            — Playwright launches installed Chrome (persistent context)
    #   "msedge"            — Playwright launches installed Edge
    #   "chromium"          — bundled Chromium
    channel = (os.environ.get("STUDIO_BROWSER_CHANNEL", "attach") or "attach").strip().lower()
    # Reduce the automation fingerprint so ChatGPT's Cloudflare anti-bot check
    # (the "Performing security verification" screen) is far less likely to
    # challenge or loop. We:
    #   * drop the "--enable-automation" switch (removes the "Chrome is being
    #     controlled by automated test software" infobar and the navigator
    #     .webdriver=true signal that Cloudflare keys on), and
    #   * disable the automation extension,
    #   * keep AutomationControlled off.
    # The operator still completes any human checkbox themselves; this just makes
    # a real, human-driven sign-in look like the normal browser it actually is.
    common_kwargs = dict(
        user_data_dir=str(profile_path),
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
            "--no-first-run",
            "--start-maximized",
        ],
        ignore_default_args=["--enable-automation"],
    )

    def _grant(context: "BrowserContext") -> "BrowserContext":
        try:
            context.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin="https://chatgpt.com",
            )
        except Exception:
            pass
        # Stealth: hide the residual automation signals Cloudflare inspects, on
        # every page/frame. navigator.webdriver=true is the strongest tell; we
        # also normalise a couple of properties headless/automation can leave odd.
        try:
            context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                try { window.chrome = window.chrome || { runtime: {} }; } catch (e) {}
                try {
                  const orig = navigator.permissions && navigator.permissions.query;
                  if (orig) {
                    navigator.permissions.query = (p) => (
                      p && p.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : orig(p)
                    );
                  }
                } catch (e) {}
                """
            )
        except Exception:
            pass
        return context

    try:
        pw = sync_playwright().start()
    except Exception:
        raise

    # 1) Preferred: attach to the REAL Chrome over CDP (least bot-detectable).
    #    Falls through to a launched context if Chrome isn't found / can't attach.
    if channel == "attach":
        ctx = _attach_real_chrome(pw, account, "chrome")
        if ctx is not None:
            return _grant(ctx)
        print("[agent] attach unavailable; falling back to a launched Chrome/Chromium")

    # 2) Launched contexts. Order: a real channel first (if asked), then the
    #    bundled Chromium as a safety net.
    attempts: list[tuple[str, dict]] = []
    launch_channel = channel if channel in ("chrome", "msedge") else "chrome"
    attempts.append((launch_channel, {**common_kwargs, "channel": launch_channel}))
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

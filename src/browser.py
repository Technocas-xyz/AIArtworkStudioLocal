"""Browser lifecycle management for ChatGPT automation."""

from __future__ import annotations

import os
import shutil
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


# Stealth init-script run on every page/frame to hide the residual automation
# signals Cloudflare Turnstile inspects. This is best-effort — Turnstile can
# still detect a CDP-driven browser by other means — but it removes the common
# JS tells (navigator.webdriver, missing window.chrome, permissions quirk,
# empty plugins/languages).
_STEALTH_JS = """
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
try {
  if (!navigator.languages || navigator.languages.length === 0) {
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
  }
} catch (e) {}
try {
  if (!navigator.plugins || navigator.plugins.length === 0) {
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
  }
} catch (e) {}
"""


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


def _cdp_ready(port: int, timeout: float = 30.0) -> bool:
    """Wait until Chrome's DevTools HTTP endpoint actually responds.

    We hit /json/version (not just a TCP connect) because a bare TCP accept can
    succeed before the DevTools HTTP server is really serving, and because if
    Chrome routed the launch into an ALREADY-RUNNING Chrome (the classic attach
    failure) this port never serves at all — which we want to detect clearly.
    """
    import urllib.request
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.0) as r:
                if r.status == 200:
                    return True
        except Exception as exc:
            last = str(exc)
        time.sleep(0.4)
    print(f"[agent] attach: DevTools endpoint on port {port} never responded ({last})")
    return False


def _default_chrome_user_data_dir(channel: str) -> "Path | None":
    """The user's REAL everyday Chrome/Edge user-data-dir (where their logged-in
    ChatGPT session already lives), or None if not found."""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return None
    if channel == "msedge":
        p = Path(local) / "Microsoft" / "Edge" / "User Data"
    else:
        p = Path(local) / "Google" / "Chrome" / "User Data"
    return p if p.is_dir() else None


def _seed_login_from_default(channel: str, dest_udd: "Path") -> bool:
    """Copy the login-carrying files from the operator's REAL Chrome profile
    into our dedicated user-data-dir `dest_udd`, so the dedicated profile starts
    already signed in to ChatGPT.

    WHY THIS EXISTS: since Chrome 136, --remote-debugging-port is IGNORED on the
    default Chrome data directory (a security change to stop apps puppeting your
    real profile). So we cannot attach to the real profile directly. Chrome DOES
    honour the debug port on a NON-default user-data-dir — and a non-default dir
    seeded with the real profile's cookies + encryption key carries the same
    logged-in session. That is exactly what we do here.

    We copy the minimum needed for the session:
      * <UserData>/Local State            — holds the OS-encrypted key cookies use
      * <UserData>/Default/Network/Cookies — the encrypted cookies themselves
      * <UserData>/Default/Network/Cookies-journal (if present)
    Returns True if it copied a cookies DB, else False. Never raises."""
    src = _default_chrome_user_data_dir(channel)
    if not src:
        print("[agent] seed: no real profile found to seed from")
        return False
    try:
        # Only seed ONCE — if the dedicated profile already has cookies, keep
        # them (the operator may have signed in there / it may be newer).
        dest_cookies = dest_udd / "Default" / "Network" / "Cookies"
        if dest_cookies.is_file() and dest_cookies.stat().st_size > 0:
            print("[agent] seed: dedicated profile already has cookies; not re-seeding")
            return True

        src_local_state = src / "Local State"
        src_cookies = src / "Default" / "Network" / "Cookies"
        if not src_cookies.is_file():
            print(f"[agent] seed: real profile has no cookies DB at {src_cookies}")
            return False

        (dest_udd / "Default" / "Network").mkdir(parents=True, exist_ok=True)
        if src_local_state.is_file():
            shutil.copy2(src_local_state, dest_udd / "Local State")
        shutil.copy2(src_cookies, dest_cookies)
        journal = src / "Default" / "Network" / "Cookies-journal"
        if journal.is_file():
            shutil.copy2(journal, dest_udd / "Default" / "Network" / "Cookies-journal")
        print(f"[agent] seed: copied login (Local State + Cookies) from real profile into {dest_udd}")
        return True
    except Exception as exc:
        print(f"[agent] seed: could not copy login files ({exc})")
        return False


def _chrome_exe_name(channel: str) -> str:
    return "msedge.exe" if channel == "msedge" else "chrome.exe"


def _chrome_running(channel: str) -> bool:
    """True if a Chrome/Edge process is running (including a background process
    left after all windows close) — which blocks a debug-port launch against the
    same user-data-dir, because the new launch just hands off to the existing
    instance and its --remote-debugging-port is ignored."""
    exe_name = _chrome_exe_name(channel)
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
            capture_output=True, text=True, timeout=8,
        ).stdout.lower()
        return exe_name.lower() in out
    except Exception:
        return False


def _kill_chrome(channel: str) -> None:
    """Terminate all Chrome/Edge processes so the profile is free for a
    debug-port launch. Only used when the operator has already closed their
    windows and we need to clear a lingering BACKGROUND process (Chrome's
    'keep running in background' behaviour), which otherwise silently steals the
    launch and leaves the debug port dead."""
    exe_name = _chrome_exe_name(channel)
    try:
        subprocess.run(["taskkill", "/F", "/IM", exe_name, "/T"],
                       capture_output=True, text=True, timeout=15)
        print(f"[agent] attach: terminated lingering {exe_name} processes")
    except Exception as exc:
        print(f"[agent] attach: could not terminate {exe_name}: {exc}")
    # Give the OS a moment to release the profile lock files.
    time.sleep(2.0)


def automation_profile_dir(account: str) -> "Path":
    """The dedicated NON-default profile the automation drives (and that the
    plain sign-in Chrome writes cookies into). Both use this SAME dir so the
    Cloudflare clearance + login cookies established during sign-in are already
    present when the automated browser attaches."""
    d = (profiles_root() / f"{account}_chrome").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


# Handle to the plain (un-automated) sign-in Chrome, so the app can close it
# before attaching the automated browser to the same profile.
_signin_proc: "subprocess.Popen | None" = None


def open_plain_signin_browser(account: str = "acct1", channel: str = "chrome") -> bool:
    """Open a PLAIN Chrome (NO remote-debugging, NO Playwright) on the automation
    profile dir, so the operator can clear Cloudflare and sign in to ChatGPT
    exactly as they would in their normal browser — Cloudflare does not block a
    browser that nothing is driving.

    The cookies (including cf_clearance + the ChatGPT session) are written into
    the automation profile. Later, close_plain_signin_browser() closes it and the
    automated browser attaches to the SAME profile, inheriting those cookies.

    Returns True if Chrome was launched."""
    global _signin_proc
    exe = _find_browser_exe(channel)
    if not exe:
        print(f"[signin] no installed {channel} found")
        return False
    udd = automation_profile_dir(account)
    # Seed login from the real profile too, as a convenience (best-effort). Even
    # if it carries a stale login, the operator will refresh it here by hand.
    try:
        _seed_login_from_default(channel, udd)
    except Exception:
        pass
    args = [
        exe,
        f"--user-data-dir={udd}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        "https://chatgpt.com",
    ]
    print(f"[signin] opening PLAIN {channel} (no automation) on profile {udd}")
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        _signin_proc = subprocess.Popen(args, creationflags=creationflags,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as exc:
        print(f"[signin] could not launch plain browser: {exc}")
        _signin_proc = None
        return False


def close_plain_signin_browser(channel: str = "chrome", timeout: float = 15.0) -> None:
    """Close the plain sign-in Chrome and WAIT for the profile lock to release,
    so the automated browser can then attach to the same profile. Chrome spawns
    child processes, so we terminate them all by image name and confirm none
    remain."""
    global _signin_proc
    print("[signin] closing plain sign-in browser and freeing the profile…")
    # Terminate the launched process and any Chrome children.
    try:
        if _signin_proc is not None:
            _signin_proc.terminate()
    except Exception:
        pass
    _signin_proc = None
    # Chrome keeps sibling/child processes; clear them so the profile unlocks.
    _kill_chrome(channel)
    # Wait until no chrome.exe remains (profile lock released).
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _chrome_running(channel):
            break
        time.sleep(0.5)
    # Extra settle time for the OS to release the lock files.
    time.sleep(1.5)


def _attach_profile(pw, exe: str, user_data_dir: "Path", label: str) -> BrowserContext | None:
    """Start the REAL Chrome/Edge with remote debugging against a specific
    user-data-dir, then connect Playwright to it over CDP.

    Because Playwright does NOT launch the browser (we start it as an ordinary
    process and merely connect), none of the automation switches Cloudflare keys
    on are present — it behaves like the browser the operator uses by hand.

    Returns a connected BrowserContext, or None if the browser couldn't be
    started / attached to (so the caller can try the next option)."""
    port = _free_port()
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        "https://chatgpt.com",
    ]
    print(f"[agent] attach[{label}]: starting real browser on CDP port {port} (profile {user_data_dir})")
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(args, creationflags=creationflags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        print(f"[agent] attach[{label}]: could not start browser: {exc}")
        return None

    if not _cdp_ready(port):
        print(f"[agent] attach[{label}]: DevTools endpoint did not come up "
              "(is this profile already open in another Chrome window?)")
        return None

    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception as exc:
        print(f"[agent] attach[{label}]: connect_over_cdp failed: {exc}")
        return None

    context = browser.contexts[0] if browser.contexts else browser.new_context()
    # Stealth on the ATTACHED context too — this was previously only applied to
    # Playwright-LAUNCHED contexts, so CDP-attached pages still exposed the
    # automation tells Cloudflare's Turnstile inspects. Apply to all future
    # pages/frames in this context.
    try:
        context.add_init_script(_STEALTH_JS)
    except Exception:
        pass
    print(f"[agent] attach[{label}]: connected over CDP")
    return context


def _context_logged_in(context: BrowserContext) -> bool:
    """Best-effort: does this context already have a signed-in ChatGPT session?

    Navigates the first page to chatgpt.com (bounded) and checks for the prompt
    box. Never raises."""
    try:
        page = context.pages[0] if context.pages else context.new_page()
    except Exception:
        return False
    try:
        page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=45_000)
    except Exception as exc:
        print(f"[agent] login-check: navigation slow/failed ({exc})")
    try:
        return is_logged_in(page)
    except Exception:
        return False


def _attach_real_chrome(pw, account: str, channel: str) -> BrowserContext | None:
    """Attach Playwright to a REAL Chrome the app starts, over the DevTools
    protocol — the least bot-detectable way to drive ChatGPT.

    IMPORTANT — Chrome 136+ behaviour: `--remote-debugging-port` is IGNORED when
    it points at Chrome's DEFAULT user-data-dir (a security change so apps can't
    puppet your real profile). It only works on a NON-default user-data-dir.
    So we cannot attach to the operator's real profile directly.

    What we do instead, to still reuse the operator's existing ChatGPT login:
      * Use a dedicated NON-default profile (profiles/<account>_chrome) — the
        debug port works there.
      * SEED that profile once with the login-carrying files (Local State +
        cookies) copied from the operator's real Chrome profile, so it starts
        ALREADY signed in to ChatGPT.
      * Attach and check login. If seeding carried the session, no sign-in is
        needed; otherwise the operator signs in once here and it persists.

    This leaves the operator's everyday Chrome untouched (we only read-copy its
    cookie files), so their normal Chrome can even stay open.

    Env overrides (STUDIO_USE_DEFAULT_PROFILE):
      "never" -> do NOT seed from the real profile; use a clean dedicated one
      other   -> seed from the real profile (default behaviour)

    Returns a BrowserContext, or None if no browser could be attached (caller
    then falls back to a Playwright-launched context)."""
    exe = _find_browser_exe(channel)
    if not exe:
        print(f"[agent] attach: no installed {channel} found")
        return None

    pref = os.environ.get("STUDIO_USE_DEFAULT_PROFILE", "").strip().lower()
    dedicated = (profiles_root() / f"{account}_chrome").resolve()
    dedicated.mkdir(parents=True, exist_ok=True)

    # Seed the dedicated profile from the real one so it starts logged in.
    # (Skipped if the operator asked not to, or if it already has a session.)
    if pref != "never":
        _seed_login_from_default(channel, dedicated)

    ctx = _attach_profile(pw, exe, dedicated, "dedicated")
    if ctx is None:
        return None
    if _context_logged_in(ctx):
        print("[agent] attach: dedicated profile is logged in (seeded from your real Chrome) — using it")
    else:
        print("[agent] attach: dedicated profile is NOT logged in — sign in once on this window")
    return ctx


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
        # every page/frame. Shared with the CDP-attach path via _STEALTH_JS.
        try:
            context.add_init_script(_STEALTH_JS)
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


# Signals that ChatGPT is loaded AND signed in. ChatGPT's markup shifts between
# UI variants, so we accept ANY of several composer/shell selectors rather than
# relying on the single #prompt-textarea id (which the newer landing screen may
# not expose immediately).
READY_SELECTORS = (
    "#prompt-textarea",                                   # classic composer
    "div[contenteditable='true']#prompt-textarea",        # contenteditable variant
    "textarea[data-testid='prompt-textarea']",            # testid variant
    "[data-testid='composer-text-input']",                # newer composer
    "div.ProseMirror[contenteditable='true']",            # ProseMirror composer
    "[data-testid='send-button']",                        # send button = composer present
    "[data-testid='create-new-chat-button']",             # sidebar 'New chat' = logged-in shell
    "nav a[href='/']",                                    # logged-in nav
)


def _ready_selector_present(page: Page) -> bool:
    for sel in READY_SELECTORS:
        try:
            if page.query_selector(sel):
                return True
        except Exception:
            continue
    return False


def is_logged_in(page: Page) -> bool:
    """Check whether the page has an active ChatGPT session.

    True if any composer/logged-in-shell signal is present. We wait briefly for
    the classic prompt box, then fall back to the broader signal set so a UI
    variant (or a slightly slow render) doesn't read as 'not logged in'.
    """
    try:
        page.wait_for_selector(PROMPT_BOX, state="visible", timeout=8_000)
        return True
    except Exception:
        pass
    # Broader check for other UI variants / a composer that isn't the classic id.
    return _ready_selector_present(page)


def on_verification_screen(page: Page) -> bool:
    """True if the page is currently on a Cloudflare/OpenAI verification screen
    ("Just a moment...", "Verifying...", the Turnstile challenge). We use this to
    WAIT for a human to clear it rather than failing — Cloudflare is fine with a
    real person passing the check; it only objects to automation racing it."""
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    if "just a moment" in title or "attention required" in title:
        return True
    try:
        # Cloudflare Turnstile challenge iframe / widget.
        if page.query_selector("iframe[src*='challenges.cloudflare.com']"):
            return True
        # Visible "Verifying..." / "checking your browser" text.
        body = (page.inner_text("body") or "").lower()
        if ("verifying" in body or "checking your browser" in body
                or "performing security verification" in body):
            return True
    except Exception:
        pass
    return False


def wait_for_login(page, timeout: float = 300.0, poll: float = 2.0,
                   on_status=None) -> bool:
    """Patiently wait until the ChatGPT session is READY (prompt box visible),
    giving a human time to clear any Cloudflare/verification screen and/or sign
    in by hand. Returns True once ready, False if it never became ready within
    `timeout`.

    This is the reliable way to drive ChatGPT-web behind Cloudflare: rather than
    fighting the bot check with stealth (which Cloudflare can defeat), we let the
    real person clear it once. The resulting session persists in the profile, so
    subsequent runs usually skip the check entirely.

    `on_status(msg)` is an optional callback so the UI can show what's happening
    ("waiting for you to complete the verification / sign in")."""
    import time as _t
    deadline = _t.time() + max(0.0, timeout)
    said_verify = said_login = False

    def _say(msg):
        print(f"[agent] {msg}")
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    while _t.time() < deadline:
        # Ready? Accept any composer/logged-in-shell signal, not just the
        # classic #prompt-textarea id (which the newer UI may not expose).
        try:
            if _ready_selector_present(page):
                _say("ChatGPT is ready (signed in).")
                return True
        except Exception:
            pass
        # On a verification wall — ask the human to complete it.
        if on_verification_screen(page):
            if not said_verify:
                _say("Cloudflare is verifying — please complete the check in the "
                     "browser window if it asks. Waiting…")
                said_verify = True
        else:
            if not said_login:
                _say("Waiting for ChatGPT to load / for you to sign in in the "
                     "browser window…")
                said_login = True
        try:
            page.wait_for_timeout(int(poll * 1000))
        except Exception:
            _t.sleep(poll)
    _say("Timed out waiting for ChatGPT to become ready.")
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

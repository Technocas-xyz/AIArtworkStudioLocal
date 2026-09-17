"""Artwork Studio — single-process desktop entry point (console/headless).

Everything runs in ONE process on the designer's PC:
  * the FastAPI web UI (app.py) on 127.0.0.1,
  * the in-process worker (local_worker.py) that claims jobs and runs the
    workflows against the local browser,
  * a real, logged-in Chromium/ChatGPT session (opened once).

There is no remote server and no network agent. Nextcloud and the Decoinks/
PrintShop backend are still reached directly over the network for the vault and
live prompts; only the middle "artwork server" is gone.

Thread model (important — Playwright's sync API is thread-affine):
  * uvicorn runs on a BACKGROUND thread (it owns its own asyncio loop; the FastAPI
    request handlers only mutate the shared in-memory `jobs` dict).
  * The browser context/page is created on the MAIN thread, and the worker claim
    loop also runs on the MAIN thread — so every Playwright call happens on the
    thread that created the page.

Use studio_gui.py for the tray/window experience with a Sign-in button. This
console entry point is handy for development and for a headless-ish run where the
operator has already signed in (via login.py or a previous GUI session).
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path


def _appbase() -> Path:
    """Stable, writable folder for input/output/logs/downloads + .env.

    Frozen build: the folder that holds the exe. Source run: the project root.
    We chdir here BEFORE importing app so its ./input, ./output, ./logs and
    ./downloads (created at import time, relative to CWD) land in one place."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_BASE = _appbase()
os.chdir(_BASE)

from dotenv import load_dotenv

# Load .env from the app base before importing app (auth/nextcloud/prompt config
# read it at import).
load_dotenv(_BASE / ".env")

import uvicorn

import app as app_module
import agent
import local_worker
from src.browser import is_logged_in


HOST = "127.0.0.1"
DEFAULT_PORT = int(os.getenv("STUDIO_PORT", "8000") or 8000)


def _pick_port(preferred: int) -> int:
    """Return `preferred` if free, else an OS-assigned free port on 127.0.0.1."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, candidate))
                return s.getsockname()[1]
            except OSError:
                continue
    # Fallback: let the OS choose.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _wait_until_up(host: str, port: int, timeout: float = 20.0) -> bool:
    """Block until the server accepts a TCP connection, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def _start_server(host: str, port: int) -> threading.Thread:
    """Run uvicorn on a daemon thread so the main thread can own the browser."""
    config = uvicorn.Config(app_module.app, host=host, port=port,
                            log_level="info", access_log=False)
    server = uvicorn.Server(config)

    def _run():
        try:
            server.run()
        except Exception:
            traceback.print_exc()

    t = threading.Thread(target=_run, name="uvicorn", daemon=True)
    t.start()
    return t


def main() -> None:
    port = _pick_port(DEFAULT_PORT)
    url = f"http://{HOST}:{port}/"
    name = os.getenv("AGENT_NAME") or os.getenv("COMPUTERNAME") or "This PC"

    print("=" * 64)
    print("  Artwork Studio — starting local server + worker")
    print(f"  URL:  {url}")
    print(f"  Name: {name}")
    print("=" * 64)

    # 1) Tell app.py it is running as the single local process.
    app_module.enable_local_mode()

    # 2) Start the web server first so the UI is reachable while the browser
    #    warms up (the operator can watch the sign-in state).
    _start_server(HOST, port)
    if _wait_until_up(HOST, port):
        try:
            webbrowser.open(url)
        except Exception:
            pass
        print(f"[studio] UI ready at {url}")
    else:
        print("[studio] WARNING: server did not report ready in time; open the URL manually.")

    # 3) Open the ONE browser context on THIS (main) thread.
    try:
        context, page = agent.open_browser_context()
    except Exception as exc:
        print(f"[studio] FATAL: could not launch Chrome: {exc}")
        traceback.print_exc()
        return

    try:
        logged_in = is_logged_in(page)
    except Exception:
        logged_in = False
    local_worker.set_logged_in(logged_in)
    if logged_in:
        print("[studio] ChatGPT session OK — logged in.")
    else:
        print("=" * 64)
        print("  NOT LOGGED IN to ChatGPT.")
        print("  Sign in in the Chrome window that just opened, then leave it open.")
        print("  (Or run:  python login.py)")
        print("=" * 64)

    # 4) Run the worker claim loop on THIS thread (same thread as the page).
    #    This blocks until stopped (Ctrl+C).
    stop_event = threading.Event()
    try:
        local_worker.start(
            page=page,
            jobs=app_module.jobs,
            jobs_lock=app_module._jobs_lock,
            input_dir=app_module.INPUT_DIR,
            output_dir=app_module.OUTPUT_DIR,
            on_terminal=app_module.on_job_terminal,
            stop_event=stop_event,
            name=name,
            background=False,   # run inline on the browser (main) thread
        )
    except KeyboardInterrupt:
        print("\n[studio] stopping…")
    finally:
        stop_event.set()
        try:
            context.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

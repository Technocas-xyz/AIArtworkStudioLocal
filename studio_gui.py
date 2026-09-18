"""Artwork Studio — desktop tray + window (single-process, all-local).

A thin tkinter + tray wrapper around the local studio. It owns the ONE Playwright
browser context for its whole lifetime (Playwright's sync API is thread-affine),
drives the one-time "Sign in to ChatGPT", starts the local FastAPI server and the
in-process worker, and opens the web UI in the default browser.

There is no remote server and no network agent here. Nextcloud + the Decoinks/
PrintShop backend are still reached over the network directly.

Config (server port + designer name) is saved to
%APPDATA%\\ArtworkStudio\\config.json so it is entered once.
"""
from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox


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


# --- Diagnostic log ---------------------------------------------------------
# The packaged app is windowed (no console), so every print() from the browser
# launch/attach code and any traceback would otherwise be lost. Tee stdout and
# stderr to studio_debug.log next to the exe so a failing sign-in can be
# diagnosed from the file. Best-effort — never breaks startup.
class _Tee:
    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]
        # The primary (real) stream is the first one; delegate terminal queries
        # like isatty()/fileno() to it so libraries (e.g. uvicorn's logging,
        # which calls sys.stdout.isatty()) behave exactly as they would without
        # the tee. Missing this raised AttributeError and stopped the server.
        self._primary = self._streams[0] if self._streams else None

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
                st.flush()
            except Exception:
                pass
        return len(s) if isinstance(s, str) else 0

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return bool(self._primary and self._primary.isatty())
        except Exception:
            return False

    def fileno(self):
        if self._primary is not None and hasattr(self._primary, "fileno"):
            return self._primary.fileno()
        raise OSError("no fileno")

    @property
    def encoding(self):
        return getattr(self._primary, "encoding", "utf-8")

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    def __getattr__(self, name):
        # Any other stream attribute libraries poke at → delegate to the primary.
        return getattr(self._primary, name)


try:
    _log_fh = open(_BASE / "studio_debug.log", "a", encoding="utf-8", buffering=1)
    import datetime as _dt
    _log_fh.write(f"\n===== Artwork Studio start {_dt.datetime.now().isoformat()} =====\n")
    sys.stdout = _Tee(sys.__stdout__, _log_fh)
    sys.stderr = _Tee(sys.__stderr__, _log_fh)
except Exception:
    pass

# Load .env before importing app (auth/nextcloud/prompt config read it at import).
try:
    from dotenv import load_dotenv
    load_dotenv(_BASE / ".env")
except Exception:
    pass

import uvicorn

import app as app_module
import agent
import local_worker
from src.browser import is_logged_in, wait_for_login
import src.browser as agent_browser


HOST = "127.0.0.1"

# --- Config file in AppData ---
_APPDATA = Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming"))
_CONFIG_DIR = _APPDATA / "ArtworkStudio"
try:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
_CONFIG_FILE = _CONFIG_DIR / "config.json"


def load_config() -> dict:
    if _CONFIG_FILE.exists():
        try:
            return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    try:
        _CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[gui] could not save config: {exc}")


# --- Tray icon drawing (coloured dot) ---
def _make_icon(color: str):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=color)
    return img


_TRAY_COLORS = {
    "connected": "#16a34a",      # green — running + signed in
    "running": "#16a34a",
    "not_signed_in": "#d97706",  # amber
    "error": "#dc2626",          # red
    "stopped": "#9ca3af",        # grey
}


def _pick_port(preferred: int) -> int:
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, candidate))
                return s.getsockname()[1]
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _server_up(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


class StudioGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Artwork Studio")
        self.root.geometry("460x300")
        self.root.resizable(False, False)

        cfg = load_config()
        self.name_var = tk.StringVar(value=cfg.get("name", os.getenv("COMPUTERNAME", "This PC")))
        self.port_var = tk.StringVar(value=str(cfg.get("port", os.getenv("STUDIO_PORT", "8000"))))

        self._events: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._tray = None
        self._state = "stopped"
        self._port = None
        self._server = None
        self._server_thread = None
        self._worker_started = False

        # --- Single browser owner thread -------------------------------------
        # Playwright's sync API is thread-affine: the context/page can only be
        # used from the thread that created them. ONE dedicated browser thread
        # owns the context AND runs the worker claim loop, so the profile is
        # locked exactly once and every Playwright call is on that thread.
        self._cmd_queue: queue.Queue = queue.Queue()
        self._context = None
        self._page = None
        self._signed_in = False
        self._signin_opened = False   # a plain sign-in browser was opened
        self._browser_thread = threading.Thread(target=self._browser_loop, daemon=True)
        self._browser_thread.start()

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(200, self._drain_events)

        # Enable local mode in app.py immediately so /api/status reads the worker.
        app_module.enable_local_mode()
        local_worker.set_name(self.name_var.get().strip() or "This PC")

    # -------------------------------------------------- UI
    def _build_ui(self):
        pad = {"padx": 12, "pady": 4}
        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True, padx=8, pady=8)

        ttk.Label(frm, text="Your name").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.name_var, width=36).grid(row=0, column=1, columnspan=2, sticky="we", **pad)

        ttk.Label(frm, text="Local port").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.port_var, width=10).grid(row=1, column=1, sticky="w", **pad)

        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=3, sticky="we", pady=(10, 4))
        self.signin_btn = ttk.Button(btns, text="Sign in to ChatGPT", command=self._on_signin)
        self.signin_btn.pack(side="left", padx=4)
        self.start_btn = ttk.Button(btns, text="Start", command=self._on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.open_btn = ttk.Button(btns, text="Open Studio", command=self._open_ui, state="disabled")
        self.open_btn.pack(side="left", padx=4)

        self.status_dot = tk.Canvas(frm, width=14, height=14, highlightthickness=0)
        self.status_dot.grid(row=3, column=0, sticky="e", pady=(12, 4))
        self._dot = self.status_dot.create_oval(2, 2, 12, 12, fill="#9ca3af", outline="")
        self.status_var = tk.StringVar(value="Stopped")
        ttk.Label(frm, textvariable=self.status_var).grid(row=3, column=1, columnspan=2, sticky="w", pady=(12, 4))

        self.url_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.url_var, foreground="#2563eb").grid(row=4, column=0, columnspan=3, sticky="w", padx=12)

        self.hint = ttk.Label(frm, text="Sign in to ChatGPT once, then Start. The Studio opens in your browser; this window minimises to the tray.",
                              foreground="#6b7280", wraplength=420)
        self.hint.grid(row=5, column=0, columnspan=3, sticky="w", padx=12, pady=(8, 0))

        frm.columnconfigure(1, weight=1)

    def _set_state(self, state: str, detail: str = ""):
        self._state = state
        colors = _TRAY_COLORS
        self.status_dot.itemconfig(self._dot, fill=colors.get(state, "#9ca3af"))
        labels = {"connected": "Running — signed in", "running": "Running a job",
                  "not_signed_in": "Not signed in to ChatGPT", "error": "Error", "stopped": "Stopped"}
        self.status_var.set(detail or labels.get(state, state))
        self._update_tray(state)

    # -------------------------------------------------- config
    def _persist(self):
        try:
            port = int(self.port_var.get().strip() or "8000")
        except ValueError:
            port = 8000
        save_config({"name": self.name_var.get().strip(), "port": port})

    # -------------------------------------------------- button handlers
    def _on_signin(self):
        # Two-phase login: open a PLAIN Chrome (no automation) so Cloudflare lets
        # you through and you can sign in exactly like your normal browser. The
        # cookies (cf_clearance + ChatGPT session) land in the automation
        # profile; when you later click Start we close this browser and attach
        # the automation to that same profile, inheriting the clearance.
        self.signin_btn.config(state="disabled")
        self._set_state("not_signed_in", "Opening a normal browser to sign in…")
        try:
            ok = agent_browser.open_plain_signin_browser("acct1")
        except Exception as exc:
            traceback.print_exc()
            ok = False
        if ok:
            self._signin_opened = True
            self._set_state("not_signed_in",
                            "Sign in / clear the check in the browser, then click Start.")
            messagebox.showinfo(
                "Sign in to ChatGPT",
                "A normal Chrome window has opened.\n\n"
                "1) Complete any Cloudflare check and sign in to ChatGPT there.\n"
                "2) Leave it on the ChatGPT page (you should see the chat box).\n"
                "3) Come back here and click Start.\n\n"
                "The app will close that window and take over your signed-in session.")
        else:
            self.signin_btn.config(state="normal")
            self._set_state("error", "Could not open the sign-in browser.")

    def _on_start(self):
        self._persist()
        local_worker.set_name(self.name_var.get().strip() or "This PC")
        self._stop_event.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._cmd_queue.put(("start", None))

    def _on_stop(self):
        self._stop_event.set()
        self.stop_btn.config(state="disabled")
        self.start_btn.config(state="normal")
        self._set_state("stopped", "Stopped")

    def _open_ui(self):
        if self._port:
            try:
                webbrowser.open(f"http://{HOST}:{self._port}/")
            except Exception:
                pass

    # -------------------------------------------------- server
    def _ensure_server(self):
        """Start uvicorn on a background thread once."""
        if self._server_thread is not None:
            return
        self._port = _pick_port(int(self.port_var.get().strip() or "8000"))
        config = uvicorn.Config(app_module.app, host=HOST, port=self._port,
                                log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)

        def _run():
            try:
                self._server.run()
            except Exception:
                traceback.print_exc()

        self._server_thread = threading.Thread(target=_run, name="uvicorn", daemon=True)
        self._server_thread.start()
        # Wait briefly for it to accept connections.
        import time as _t
        for _ in range(60):
            if _server_up(HOST, self._port):
                break
            _t.sleep(0.1)
        self._events.put(("server_up", self._port))

    # -------------------------------------------------- single browser thread
    def _browser_loop(self):
        """Owns the ONE Playwright context/page AND runs the worker loop."""
        while True:
            try:
                cmd, _ = self._cmd_queue.get()
            except Exception:
                continue
            if cmd == "quit":
                self._close_context()
                return
            elif cmd == "signin":
                self._do_signin()
            elif cmd == "start":
                self._do_start()

    def _ensure_context(self):
        if self._context is None:
            self._context, self._page = agent.open_browser_context()
        return self._page

    def _do_signin(self):
        try:
            page = self._ensure_context()
            # ALWAYS (re)navigate to chatgpt.com on a sign-in click, bounded so a
            # slow/unreachable network can't leave the browser stuck on
            # about:blank forever.
            nav_ok = False
            nav_err = ""
            try:
                page.bring_to_front()
            except Exception:
                pass
            try:
                page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=60_000)
                nav_ok = True
            except Exception as nav_exc:
                nav_err = str(nav_exc)
                self._events.put(("log", f"sign-in navigation to chatgpt.com failed: {nav_exc}"))

            if not nav_ok:
                # Navigation itself failed — almost always the PC being unable to
                # reach chatgpt.com (network/DNS/proxy/firewall).
                self._events.put(("signin_error",
                                  "Could not load chatgpt.com. Check this PC can open "
                                  "https://chatgpt.com in a normal browser (network/proxy/firewall). "
                                  + (nav_err[:200] if nav_err else "")))
                return

            # PATIENT WAIT: give the human time to clear any Cloudflare
            # "Verifying…" screen and/or sign in by hand. This is the reliable
            # way to get past Cloudflare on a CDP-driven browser — a real person
            # completes the check once, and the session then persists in the
            # profile so later runs skip it. We surface progress to the UI.
            self._events.put(("status", ("not_signed_in",
                                          "Complete the ChatGPT/Cloudflare check in the browser window…")))
            logged_in = wait_for_login(
                page, timeout=300.0,
                on_status=lambda m: self._events.put(("status", ("not_signed_in", m))),
            )
            self._signed_in = bool(logged_in)
            local_worker.set_logged_in(self._signed_in)
            self._events.put(("signin_done", logged_in))
        except Exception as exc:
            traceback.print_exc()
            self._events.put(("signin_error", str(exc)))

    def _do_start(self):
        try:
            # 0) If a PLAIN sign-in browser was opened, close it and free the
            #    profile so the automated browser can attach to the SAME profile
            #    and inherit the Cloudflare clearance + login cookies.
            if self._signin_opened:
                self._events.put(("status", ("not_signed_in", "Taking over your signed-in session…")))
                try:
                    agent_browser.close_plain_signin_browser("chrome")
                except Exception:
                    traceback.print_exc()
                self._signin_opened = False

            # 1) Local mode + web server (idempotent).
            app_module.enable_local_mode()
            self._ensure_server()

            # 2) Open (or reuse) the automated browser on THIS thread. It attaches
            #    to the profile the sign-in browser just populated.
            page = self._ensure_context()
            # Patiently confirm readiness (cookies should carry the session, so
            # this usually returns immediately).
            logged_in = wait_for_login(
                page, timeout=180.0,
                on_status=lambda m: self._events.put(("status", ("not_signed_in", m))),
            )
            self._signed_in = bool(logged_in)
            local_worker.set_logged_in(logged_in)

            self._events.put(("started", None))

            # 3) Run the worker claim loop on THIS (browser) thread. Blocks until
            #    Stop/Quit sets the stop event.
            local_worker.start(
                page=page,
                jobs=app_module.jobs,
                jobs_lock=app_module._jobs_lock,
                input_dir=app_module.INPUT_DIR,
                output_dir=app_module.OUTPUT_DIR,
                on_terminal=app_module.on_job_terminal,
                stop_event=self._stop_event,
                name=self.name_var.get().strip() or "This PC",
                background=False,
            )
            self._events.put(("status", ("stopped", "Stopped")))
        except Exception as exc:
            traceback.print_exc()
            self._events.put(("status", ("error", str(exc))))

    def _close_context(self):
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
            self._page = None
            self._signed_in = False

    # -------------------------------------------------- event pump
    def _drain_events(self):
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "status":
                    s, d = payload
                    self._set_state(s, d)
                elif kind == "server_up":
                    self._port = payload
                    self.url_var.set(f"http://{HOST}:{payload}/")
                    self.open_btn.config(state="normal")
                elif kind == "started":
                    # Server + worker are up; open the UI and reflect state.
                    self._open_ui()
                    self._set_state("connected" if self._signed_in else "not_signed_in",
                                    "Running — signed in" if self._signed_in else "Running — sign in to ChatGPT")
                elif kind == "signin_done":
                    self.signin_btn.config(state="normal")
                    if payload:
                        self._set_state("connected", "Signed in to ChatGPT")
                    else:
                        self._set_state("not_signed_in", "Not signed in — try again")
                elif kind == "signin_error":
                    self.signin_btn.config(state="normal")
                    self._set_state("error", f"Sign-in failed: {payload}")
        except queue.Empty:
            pass
        # Reflect live worker state (running a job vs idle) into the dot.
        try:
            if self._worker_state_running():
                st = local_worker.get_state()
                if st.get("current_job_id"):
                    self._set_state("running", "Running a job")
                elif st.get("logged_in"):
                    if self._state != "connected":
                        self._set_state("connected", "Running — waiting for jobs")
                elif self._state not in ("stopped",):
                    self._set_state("not_signed_in", "Running — sign in to ChatGPT")
        except Exception:
            pass
        self.root.after(500, self._drain_events)

    def _worker_state_running(self) -> bool:
        try:
            return local_worker.is_worker_running()
        except Exception:
            return False

    # -------------------------------------------------- tray
    def _ensure_tray(self):
        if self._tray is not None:
            return
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("Show window", self._tray_show, default=True),
                pystray.MenuItem("Open Studio", lambda: self._open_ui()),
                pystray.MenuItem("Stop", lambda: self._on_stop()),
                pystray.MenuItem("Quit", self._tray_quit),
            )
            self._tray = pystray.Icon("artwork_studio", _make_icon("#9ca3af"), "Artwork Studio", menu)
            threading.Thread(target=self._tray.run, daemon=True).start()
        except Exception as exc:
            print(f"[gui] tray unavailable: {exc}")

    def _update_tray(self, state: str):
        if self._tray is None:
            return
        try:
            self._tray.icon = _make_icon(_TRAY_COLORS.get(state, "#9ca3af"))
            self._tray.title = f"Artwork Studio — {state}"
        except Exception:
            pass

    def _tray_show(self, *_):
        self.root.after(0, self._restore_window)

    def _restore_window(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()

    def _tray_quit(self, *_):
        self._stop_event.set()
        self._cmd_queue.put(("quit", None))
        if self._tray:
            self._tray.stop()
        self.root.after(0, self.root.destroy)

    def _on_close(self):
        # Minimise to tray instead of quitting.
        self._ensure_tray()
        self.root.withdraw()


def main():
    root = tk.Tk()
    gui = StudioGUI(root)
    gui._set_state("stopped", "Stopped")
    root.mainloop()


if __name__ == "__main__":
    main()

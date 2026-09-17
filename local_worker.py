"""In-process worker for the single-process (local) Artwork Studio.

This replaces the client-side network agent. Instead of the designer's PC
polling a remote server over HTTP, everything runs in ONE process on the PC:
the FastAPI UI (app.py), this worker, and the browser all share the same
`jobs` dict in memory.

Design:
  * The heavy workflow logic in agent.py is REUSED UNCHANGED. Those functions
    read/write the module-global agent.INPUT_DIR / agent.OUTPUT_DIR and call
    the module-global agent._wait_for_resume(job_id) at every operator pause.
  * We only swap the *transport seams* in the agent module so the same code
    runs locally:
      - agent.INPUT_DIR / agent.OUTPUT_DIR  -> the app's ./input and ./output,
        so the workflow writes exactly where the server serves files from.
      - agent._wait_for_resume(job_id)      -> an IN-PROCESS wait on a
        threading.Event, released the instant an operator pause-answer route
        flips the job's status back to "running" (or the job is cancelled).
        No HTTP polling, no heartbeat, no merge reconciliation — those are the
        pieces that caused the session/timeout failures.
      - agent._upload_files / _download_inputs -> no-ops: files are already
        local, so there is nothing to ship. The audit still passes because the
        files exist in ./output.

  * There is exactly ONE worker and ONE browser page. A job is claimed off the
    local queue, run to completion (or a pause), and on any terminal status we
    run the same server-side post-processing the old /result endpoint did
    (similarity recompute) plus the Prompt Management run log.

Thread model: Playwright's sync API is thread-affine, so the worker loop runs
on the SAME thread that created the browser page. The FastAPI request handlers
(operator answers, status) run on their own threads and only mutate the shared
`jobs` dict — which is safe here because each job is owned by the one worker and
the answer routes only ever flip a paused job forward.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

import agent  # the existing workflow library — reused unchanged
from src.browser import is_logged_in


# ---------------------------------------------------------------------------
# In-process resume registry
# ---------------------------------------------------------------------------
# One Event per job that is (or will be) waiting at an operator pause. The
# pause-answer routes in app.py call resume(job_id) to release the wait; cancel
# calls cancel(job_id). Because everything is one process, the operator's answer
# is already written onto the shared job dict before the event is set, so the
# workflow reads it directly with no field-copy step.

_resume_events: dict[str, threading.Event] = {}
_resume_lock = threading.Lock()

# The single worker's state, surfaced to /api/status and /api/session.
_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,        # worker loop alive
    "logged_in": False,      # ChatGPT session present on the local browser
    "current_job_id": None,  # the job the worker is on right now, if any
    "name": "This PC",       # label shown in the UI header
    "last_error": "",
}


def _event_for(job_id: str) -> threading.Event:
    with _resume_lock:
        ev = _resume_events.get(job_id)
        if ev is None:
            ev = threading.Event()
            _resume_events[job_id] = ev
        return ev


def resume(job_id: str) -> None:
    """Release a job blocked at a pause. Called by the operator answer routes."""
    _event_for(job_id).set()


def cancel(job_id: str) -> None:
    """Wake a paused job so it observes the cancellation immediately."""
    _event_for(job_id).set()


def _clear_event(job_id: str) -> None:
    with _resume_lock:
        _resume_events.pop(job_id, None)


# ---------------------------------------------------------------------------
# State accessors for the API layer
# ---------------------------------------------------------------------------

def get_state() -> dict[str, Any]:
    with _state_lock:
        return dict(_state)


def set_logged_in(value: bool) -> None:
    with _state_lock:
        _state["logged_in"] = bool(value)


def set_name(name: str) -> None:
    with _state_lock:
        _state["name"] = name or "This PC"


def is_worker_running() -> bool:
    with _state_lock:
        return bool(_state["running"])


def current_job_id() -> str | None:
    with _state_lock:
        return _state["current_job_id"]


# ---------------------------------------------------------------------------
# Seam patches: make agent.py run in-process instead of over HTTP
# ---------------------------------------------------------------------------

_patched = False


def install_patches(input_dir: Path, output_dir: Path) -> None:
    """Point the agent workflow library at local dirs and in-process pause/resume.

    Idempotent — safe to call more than once."""
    global _patched
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1) The workflow code writes to agent.INPUT_DIR / agent.OUTPUT_DIR. Point
    #    them at the app's own ./input and ./output so the server serves exactly
    #    the files the worker produced.
    agent.INPUT_DIR = input_dir
    agent.OUTPUT_DIR = output_dir

    # 2) In-process pause/resume. The workflow sets job["status"]="awaiting_*"
    #    and then calls agent._wait_for_resume(job_id). We block on the job's
    #    event until an operator answer flips the status forward, then return —
    #    the answer fields are already on the shared job dict.
    def _local_wait_for_resume(job_id: str) -> None:
        job = _jobs_ref.get(job_id) if _jobs_ref is not None else None
        ev = _event_for(job_id)
        ev.clear()
        while True:
            # Already resolved (answered) or cancelled? Return / abort at once.
            status = (job or {}).get("status", "") if job else ""
            if status == "cancelled":
                _clear_event(job_id)
                raise agent._CancelledError()
            if status and not status.startswith("awaiting_"):
                # Operator answered: status moved back to running (or terminal).
                _clear_event(job_id)
                return
            # Wait for a signal, but wake periodically so a status set without a
            # signal (belt-and-braces) is still observed.
            ev.wait(timeout=1.0)
            ev.clear()

    agent._wait_for_resume = _local_wait_for_resume

    # 3) Uploads/downloads are no-ops: the files are already local. Returning the
    #    requested names as "uploaded" keeps agent._upload_outputs' audit happy.
    def _local_upload_files(job_id: str, names) -> set:
        names = [n for n in (names or []) if n]
        present = {n for n in names if (output_dir / n).exists()}
        return present

    def _local_download_inputs(input_files) -> None:
        # Inputs already live in ./input (uploaded via /api/upload or imported
        # from Nextcloud), so there is nothing to fetch.
        return None

    agent._upload_files = _local_upload_files
    agent._download_inputs = _local_download_inputs

    # The agent's console-capture Tee keys off agent._current_job_id; keep it
    # working so per-job logs still populate for the UI.
    _patched = True


# The worker needs a handle to the app's shared jobs dict and lock, plus the
# terminal-completion hooks (similarity + prompt run log). These are injected by
# start() to avoid an import cycle with app.py.
_jobs_ref: dict[str, dict] | None = None
_jobs_lock_ref: threading.Lock | None = None
_on_terminal: Callable[[dict], None] | None = None


# ---------------------------------------------------------------------------
# The claim loop
# ---------------------------------------------------------------------------

def _claim_next_job() -> dict | None:
    """Claim the oldest queued job, mirroring the old agent_next_job logic but
    for the single local worker. Returns the job dict or None."""
    assert _jobs_ref is not None and _jobs_lock_ref is not None
    with _jobs_lock_ref:
        queued = sorted(
            [j for j in _jobs_ref.values() if j.get("status") == "queued"],
            key=lambda j: j.get("created_at", 0),
        )
        if not queued:
            return None
        job = queued[0]
        job["status"] = "running"
        job["started_at"] = time.time()
        job["claimed_by"] = "local"
        job["claimed_by_name"] = get_state().get("name") or "This PC"
        job["claimed_at"] = time.time()
        job["last_progress_at"] = time.time()
        return job


def _run_one_job(page: Any, job: dict) -> None:
    """Run a single claimed job to completion using the reused agent workflows,
    then fire the terminal-completion hooks."""
    job_id = job["id"]
    # Let the agent's console Tee attribute prints to this job.
    agent._current_job_id = job_id
    agent._current_job = job
    agent._reset_job_buffers()
    print(f"[worker] running job {job_id} ({job.get('workflow')})")
    try:
        agent._run_job(page, job)
    except agent._CancelledError:
        print(f"[worker] job {job_id} cancelled by operator")
        job["status"] = "cancelled"
        job.setdefault("finished_at", time.time())
    except Exception as exc:  # noqa: BLE001 — record any failure on the job
        import traceback
        tb = traceback.format_exc()
        print(tb)
        job["status"] = "failed"
        job["error"] = str(exc)
        job["finished_at"] = time.time()
        job["awaiting_input"] = False
        # Rich diagnostics so the web UI shows the failure without a terminal.
        job.setdefault("error_details", []).append({
            "message": str(exc),
            "exc_type": type(exc).__name__,
            "traceback": tb,
            "step_name": job.get("stage_label") or f"stage {job.get('stage', '?')}",
            "agent_name": get_state().get("name") or "This PC",
            "timestamp": time.time(),
            "warning": False,
            "screenshot": None,
        })
        # If the session expired, reflect it so the UI prompts a re-login.
        try:
            from src.browser import SessionExpiredError
            if isinstance(exc, SessionExpiredError):
                set_logged_in(False)
        except Exception:
            pass
    finally:
        agent._current_job_id = None
        agent._current_job = None
        _clear_event(job_id)
        # Terminal-state bookkeeping the old /result + merge path used to do:
        # similarity recompute (cv2 lives in-process now) and the prompt run log.
        if _on_terminal is not None:
            try:
                _on_terminal(job)
            except Exception as exc:  # never let reporting break the loop
                print(f"[worker] terminal hook failed for {job_id}: {exc}")


def _loop(page: Any, stop_event: threading.Event,
          poll_interval: float = 2.0) -> None:
    with _state_lock:
        _state["running"] = True
    print("[worker] claim loop started")
    try:
        while not stop_event.is_set():
            # Keep the session flag fresh so the UI header is accurate.
            try:
                set_logged_in(is_logged_in(page))
            except Exception:
                set_logged_in(False)

            job = _claim_next_job()
            if job is None:
                stop_event.wait(poll_interval)
                continue

            with _state_lock:
                _state["current_job_id"] = job["id"]
            try:
                _run_one_job(page, job)
            finally:
                with _state_lock:
                    _state["current_job_id"] = None
    finally:
        with _state_lock:
            _state["running"] = False
        print("[worker] claim loop stopped")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def start(page: Any,
          jobs: dict[str, dict],
          jobs_lock: threading.Lock,
          input_dir: Path,
          output_dir: Path,
          on_terminal: Callable[[dict], None] | None = None,
          stop_event: threading.Event | None = None,
          name: str | None = None,
          background: bool = True) -> threading.Thread | None:
    """Wire up and start the in-process worker.

    Parameters
    ----------
    page:        the single Playwright page (created on the worker's thread).
    jobs:        app.py's shared jobs dict.
    jobs_lock:   app.py's _jobs_lock.
    input_dir:   ./input
    output_dir:  ./output
    on_terminal: callback(job) run when a job reaches a terminal status; app.py
                 passes a function that recomputes similarity and reports prompt
                 runs.
    stop_event:  optional stop signal for a clean shutdown.
    name:        label for the UI header.
    background:  run the loop on a new daemon thread (True) or inline (False).

    NOTE: When background=True the loop runs on a NEW thread. Playwright is
    thread-affine, so `page` MUST have been created on that same thread. In the
    GUI/entry point the browser thread owns both, so it calls start(background=
    False) from that thread. background=True is provided for headless setups
    where the caller created the page on the worker thread.
    """
    global _jobs_ref, _jobs_lock_ref, _on_terminal
    _jobs_ref = jobs
    _jobs_lock_ref = jobs_lock
    _on_terminal = on_terminal
    if name:
        set_name(name)

    install_patches(input_dir, output_dir)

    ev = stop_event or threading.Event()
    if background:
        t = threading.Thread(target=_loop, args=(page, ev), name="local-worker", daemon=True)
        t.start()
        return t
    _loop(page, ev)
    return None

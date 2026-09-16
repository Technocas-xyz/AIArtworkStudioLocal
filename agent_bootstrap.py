"""Frozen entry point for the packaged agent.

This tiny bootstrap is the ONLY code baked permanently into ArtworkAgent.exe.
Everything that changes day to day — agent.py, agent_gui.py, src/, config/ —
lives in a WRITABLE `code/` folder next to the exe so it can be self-updated
without shipping the 250 MB Chromium runtime again.

Responsibilities (kept deliberately minimal so this file never needs updating):
  1. Seed `<app>/code` from the read-only baseline shipped inside `_internal/`
     on first run (fresh install has code before it ever contacts the server).
  2. Put `<app>/code` FIRST on sys.path so the loose, updatable modules win
     over anything frozen.
  3. Hand off to agent_gui.main().

If anything here fails, we fall back to importing agent_gui directly from the
frozen bundle, so a first launch can never be bricked by a code/ problem.
"""
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

# These names MUST match src/self_update.py. The bootstrap owns the actual swap
# because it is the only moment code/ is not yet imported and therefore not
# locked — the running agent can only STAGE an update, never swap its own live
# code/ on Windows (open .py files block the rename).
_STAGING_NAME = "code_staging"
_BACKUP_NAME = "code_backup"
_VERSION_FILE = "code_version.txt"
_MANAGED = ("agent.py", "agent_gui.py", "src", "config")


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _blog(msg: str) -> None:
    """Log to console AND <app>/update.log so a failed swap is visible without a terminal."""
    line = f"[bootstrap] {msg}"
    try:
        print(line)
    except Exception:
        pass
    try:
        with open(_app_dir() / "update.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:
        pass


def _force_rmtree(path: Path) -> None:
    def _onerror(func, p, _exc):
        try:
            os.chmod(p, 0o777); func(p)
        except Exception:
            pass
    if path.exists():
        shutil.rmtree(path, onerror=_onerror)


def _move_onto(src: Path, dst: Path) -> None:
    if dst.exists():
        _force_rmtree(dst) if dst.is_dir() else dst.unlink()
    try:
        os.replace(str(src), str(dst))
        return
    except OSError:
        pass
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        _force_rmtree(src)
    else:
        shutil.copy2(src, dst)
        try:
            src.unlink()
        except Exception:
            pass


def _apply_staged_if_present() -> None:
    """Swap a staged update into code/ BEFORE code/ is imported (so nothing is
    locked). Self-contained — must not import anything from code/. On failure,
    roll back and leave the old code in place; never brick the launch."""
    app = _app_dir()
    staging = app / _STAGING_NAME
    if not staging.is_dir():
        return
    code = app / "code"
    # Validate the bundle before touching live code.
    ok = ((staging / "agent.py").is_file() and (staging / "agent_gui.py").is_file()
          and (staging / "src").is_dir() and (staging / "config").is_dir())
    if not ok:
        present = sorted(p.name for p in staging.iterdir())
        _blog(f"staged bundle incomplete, refusing to apply. staging has: {present}")
        return
    ver = ""
    vf = staging / _VERSION_FILE
    if vf.is_file():
        try:
            ver = vf.read_text(encoding="utf-8").strip()
        except Exception:
            ver = ""
    _blog(f"applying staged update {ver or '(no version)'} from {staging}")
    code.mkdir(parents=True, exist_ok=True)
    backup = app / _BACKUP_NAME
    _force_rmtree(backup); backup.mkdir(parents=True, exist_ok=True)

    moved, placed = [], []
    try:
        for name in _MANAGED:
            src = staging / name
            if not src.exists():
                continue
            dst = code / name
            if dst.exists():
                _move_onto(dst, backup / name)
                moved.append(name)
                _blog(f"moved existing code/{name} -> backup")
            _move_onto(src, dst)
            placed.append(name)
            _blog(f"placed new code/{name}")
        # Version LAST — a present code/code_version.txt means fully applied.
        if ver:
            (code / _VERSION_FILE).write_text(ver, encoding="utf-8")
            _blog(f"wrote code/{_VERSION_FILE} = {ver}")
        _blog("staged update applied cleanly")
    except Exception as exc:
        _blog(f"apply FAILED: {exc}\n{traceback.format_exc()}")
        for name in placed:
            d = code / name
            _force_rmtree(d) if d.is_dir() else (d.unlink() if d.exists() else None)
        for name in moved:
            b = backup / name
            if b.exists():
                try:
                    _move_onto(b, code / name)
                except Exception as rexc:
                    _blog(f"rollback of {name} failed: {rexc}")
        _blog("rolled back to previous code")
    finally:
        _force_rmtree(staging)


def _baseline_dir() -> Path | None:
    """Read-only code baseline shipped in the build (under _internal/)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        b = Path(meipass) / "code_baseline"
        if b.is_dir():
            return b
    # Source/dev run: the project root itself is the code.
    return None


def _seed_code_dir() -> Path:
    """Ensure <app>/code exists and holds code. Returns the code dir path."""
    code = _app_dir() / "code"
    baseline = _baseline_dir()
    try:
        code.mkdir(parents=True, exist_ok=True)
        # Seed only when the code dir has no agent_gui yet (fresh install or a
        # wiped code dir). We never overwrite an already-updated code dir.
        if baseline and not (code / "agent_gui.py").is_file():
            for entry in baseline.iterdir():
                dst = code / entry.name
                if entry.is_dir():
                    shutil.copytree(entry, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(entry, dst)
    except Exception as exc:
        print(f"[bootstrap] could not seed code dir: {exc}")
    return code


def main() -> None:
    # 1) Apply any staged update FIRST, before code/ is imported/locked. This is
    #    the only safe moment on Windows to swap the agent's own live code.
    try:
        _apply_staged_if_present()
    except Exception as exc:
        _blog(f"apply step errored (continuing on current code): {exc}")

    # 2) Seed code/ from the shipped baseline on a fresh install.
    try:
        code = _seed_code_dir()
        p = str(code)
        # Loose, updatable code must win over any frozen copy.
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    except Exception as exc:
        print(f"[bootstrap] path setup failed, using frozen code: {exc}")

    # 3) Import the (now up-to-date) GUI and run it.
    import agent_gui
    agent_gui.main()


if __name__ == "__main__":
    main()

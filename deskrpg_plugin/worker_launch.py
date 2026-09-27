"""Can this gateway start kanban workers? — `/deskrpg/info` `kanban.worker_launch`.

Hermes starts a kanban worker with `$HERMES_BIN` when it is set, and otherwise as `<gateway python> -m hermes_cli.main`
(`hermes_cli/kanban_db_dispatch.py`, `_resolve_hermes_argv`). It removes its own `PYTHONPATH` from the worker's
environment (`tools/environments/local.py`). On the upstream PM runtime the gateway imports Hermes only through that
`PYTHONPATH`, so without `HERMES_BIN` every worker exits with `No module named 'hermes_cli'` and its card is given up.

The check mirrors that: with `HERMES_BIN` set it only checks that the file can be run; without it, it starts the
gateway's interpreter once without `PYTHONPATH` and tries the import. The answer cannot change while the process
lives, so it is computed once. It never raises — an inconclusive probe reports `ok: null`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

PROBE_TIMEOUT_SECONDS = 20


def _looks_like_path(value: str) -> bool:
    return os.sep in value or (os.altsep is not None and os.altsep in value)


def _runnable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def launcher_candidate() -> str | None:
    """The launcher to suggest for `HERMES_BIN`: the checkout's PM launcher, else `hermes` on PATH."""
    try:
        import hermes_cli

        pm_launcher = Path(hermes_cli.__file__).resolve().parent.parent / ".hermes" / "bin" / "hermes"
        if _runnable(str(pm_launcher)):
            return str(pm_launcher)
    except Exception:  # noqa: BLE001 — a suggestion only
        pass
    found = shutil.which("hermes")
    return str(Path(found).resolve()) if found else None


def _module_importable(executable: str, module: str) -> bool | None:
    """Whether `executable` imports `module` with the environment a worker gets (no `PYTHONPATH`). None if the
    probe itself could not run."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    try:
        done = subprocess.run(
            [executable, "-c", f"import {module}"],
            env=env, cwd=os.path.abspath(os.sep), capture_output=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode == 0


@lru_cache(maxsize=4)
def _report(hermes_bin: str, executable: str, module: str) -> dict:
    launcher = launcher_candidate()
    if hermes_bin:
        target = hermes_bin if _looks_like_path(hermes_bin) else shutil.which(hermes_bin)
        ok = bool(target) and _runnable(str(target))
        return {"ok": ok, "reason": None if ok else "hermes_bin_missing", "hermes_bin": hermes_bin,
                "launcher": launcher}
    importable = _module_importable(executable, module)
    if importable is None:
        return {"ok": None, "reason": "probe_failed", "hermes_bin": None, "launcher": launcher}
    return {"ok": importable, "reason": None if importable else "hermes_bin_unset", "hermes_bin": None,
            "launcher": launcher}


def report(*, module: str = "hermes_cli") -> dict:
    """`{"ok": bool|None, "reason": None|"hermes_bin_unset"|"hermes_bin_missing"|"probe_failed",
    "hermes_bin": str|None, "launcher": str|None}` — `launcher` is what to set `HERMES_BIN` to."""
    return dict(_report(os.environ.get("HERMES_BIN", "").strip(), sys.executable, module))


def reset_cache() -> None:
    _report.cache_clear()

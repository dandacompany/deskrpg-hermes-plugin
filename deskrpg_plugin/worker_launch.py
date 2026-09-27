"""Can this gateway start kanban workers? — `/deskrpg/info` `kanban.worker_launch`.

Hermes starts a kanban worker with `$HERMES_BIN` when it is set, and otherwise as `<gateway python> -m hermes_cli.main`
(`hermes_cli/kanban_db_dispatch.py`, `_resolve_hermes_argv`). On the upstream PM runtime the gateway imports Hermes
only through the `PYTHONPATH` its bootstrap exports, so whether that command works depends on the worker's
environment. The dispatcher builds it with `build_subprocess_env(scrub_secrets=is_multiplex_active() or routed)`, and
only the scrubbed build strips the Hermes-owned `PYTHONPATH` entries
(`tools/environments/local_pythonpath.py`). So:

- under multiplexing (how DeskRPG runs a gateway) every worker is scrubbed, and without `HERMES_BIN` none can start;
- without it, a worker for the gateway's own profile keeps the gateway's `PYTHONPATH` and starts, while a worker for
  any other (routed) profile is scrubbed.

The check mirrors that: with `HERMES_BIN` set it only checks that the file can be run; without it, it tries the
import without `PYTHONPATH` (a scrubbed worker) and, when the gateway does not multiplex, also with it (a worker for
the gateway's own profile). When the two disagree the answer depends on the card's assignee: `ok: null`, reason
`assignee_dependent`. Only public Hermes names are used (`agent.secret_scope.is_multiplex_active`). The answer cannot change while the process lives, so it is computed once.
It never raises — an inconclusive probe reports `ok: null`.
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


def _module_importable(executable: str, module: str, env: dict) -> bool | None:
    """Whether `executable` imports `module` with `env`. None if the probe itself could not run."""
    try:
        done = subprocess.run(
            [executable, "-c", f"import {module}"],
            env=env, cwd=os.path.abspath(os.sep), capture_output=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode == 0


def _scrubbed_env() -> dict:
    """The gateway's environment without `PYTHONPATH`. On the PM runtime every entry there is one Hermes put in
    (the checkout and its dependency environment), and Hermes strips exactly those from a scrubbed worker."""
    return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def _multiplex_active() -> bool:
    """Whether this gateway serves several profiles. A Hermes without the flag scrubs every worker."""
    try:
        from agent.secret_scope import is_multiplex_active

        return bool(is_multiplex_active())
    except Exception:  # noqa: BLE001
        return True


@lru_cache(maxsize=4)
def _report(hermes_bin: str, executable: str, module: str) -> dict:
    launcher = launcher_candidate()
    if hermes_bin:
        target = hermes_bin if _looks_like_path(hermes_bin) else shutil.which(hermes_bin)
        ok = bool(target) and _runnable(str(target))
        return {"ok": ok, "reason": None if ok else "hermes_bin_missing", "hermes_bin": hermes_bin,
                "launcher": launcher}
    routed = _module_importable(executable, module, _scrubbed_env())
    own = routed if _multiplex_active() else _module_importable(executable, module, dict(os.environ))
    if routed is None or own is None:
        return {"ok": None, "reason": "probe_failed", "hermes_bin": None, "launcher": launcher}
    if routed != own:
        return {"ok": None, "reason": "assignee_dependent", "hermes_bin": None, "launcher": launcher}
    return {"ok": routed, "reason": None if routed else "hermes_bin_unset", "hermes_bin": None,
            "launcher": launcher}


def report(*, module: str = "hermes_cli") -> dict:
    """`{"ok": bool|None, "reason": None|"hermes_bin_unset"|"hermes_bin_missing"|"probe_failed"|"assignee_dependent",
    "hermes_bin": str|None, "launcher": str|None}` — `launcher` is what to set `HERMES_BIN` to."""
    return dict(_report(os.environ.get("HERMES_BIN", "").strip(), sys.executable, module))


def reset_cache() -> None:
    _report.cache_clear()

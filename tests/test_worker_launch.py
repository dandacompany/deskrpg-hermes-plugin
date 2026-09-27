"""`kanban.worker_launch`: whether Hermes can start kanban workers from this gateway."""

import os
import stat
import sys

import pytest

from deskrpg_plugin import routes, worker_launch


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    worker_launch.reset_cache()
    monkeypatch.delenv("HERMES_BIN", raising=False)
    yield
    worker_launch.reset_cache()


def _script(path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_without_hermes_bin_a_worker_that_cannot_import_hermes_is_reported(monkeypatch):
    # The worker command is `<gateway python> -m hermes_cli.main`; a module the bare interpreter cannot import
    # stands in for Hermes on the PM runtime.
    got = worker_launch.report(module="deskrpg_worker_launch_probe_missing")
    assert (got["ok"], got["reason"], got["hermes_bin"]) == (False, "hermes_bin_unset", None)


def test_without_hermes_bin_an_importable_hermes_is_fine():
    got = worker_launch.report(module="json")
    assert (got["ok"], got["reason"]) == (True, None)


def test_without_hermess_strip_the_whole_pythonpath_is_dropped(tmp_path, monkeypatch):
    # A Hermes without tools.environments.local_pythonpath: its workers lose the whole PYTHONPATH.
    monkeypatch.setitem(sys.modules, "tools.environments.local_pythonpath", None)
    monkeypatch.setattr(worker_launch, "_multiplex_active", lambda: True)
    (tmp_path / "only_on_pythonpath.py").write_text("")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    got = worker_launch.report(module="only_on_pythonpath")
    assert got["reason"] == "hermes_bin_unset"


def test_a_runnable_hermes_bin_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_BIN", _script(tmp_path / "hermes"))
    got = worker_launch.report(module="deskrpg_worker_launch_probe_missing")
    assert (got["ok"], got["reason"], got["hermes_bin"]) == (True, None, str(tmp_path / "hermes"))


def test_a_hermes_bin_that_cannot_run_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_BIN", str(tmp_path / "gone" / "hermes"))
    got = worker_launch.report()
    assert (got["ok"], got["reason"]) == (False, "hermes_bin_missing")


def test_a_probe_that_cannot_run_is_inconclusive(monkeypatch):
    monkeypatch.setattr(worker_launch.sys, "executable", "/nonexistent/python")
    got = worker_launch.report(module="json")
    assert (got["ok"], got["reason"]) == (None, "probe_failed")


def test_the_launcher_suggestion_prefers_hermes_on_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _script(bin_dir / "hermes")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setitem(sys.modules, "hermes_cli", None)  # no checkout launcher to find
    assert worker_launch.launcher_candidate() == str((bin_dir / "hermes").resolve())


def test_the_answer_is_computed_once(monkeypatch):
    monkeypatch.setattr(worker_launch, "_multiplex_active", lambda: True)
    calls = []
    real = worker_launch._module_importable
    monkeypatch.setattr(worker_launch, "_module_importable", lambda e, m, env: calls.append(m) or real(e, m, env))
    worker_launch.report(module="json")
    worker_launch.report(module="json")
    assert calls == ["json"]


def test_info_reports_null_when_the_check_fails(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(worker_launch, "report", boom)
    assert routes._info_worker_launch() is None


# The worker's environment is what Hermes's dispatcher builds: `build_subprocess_env(scrub_secrets=
# is_multiplex_active() or routed)` (hermes_cli/kanban_db_dispatch.py). Only the scrubbed build strips the
# Hermes-owned PYTHONPATH entries (tools/environments/local_pythonpath.py); a worker for the gateway's own profile
# on a gateway without multiplexing keeps the gateway's PYTHONPATH and starts without HERMES_BIN.


def _hermes_env(monkeypatch, tmp_path, *, multiplex):
    """Fake Hermes modules: a strip that removes exactly `owned` from PYTHONPATH, and the multiplex flag."""
    import types

    owned, user = tmp_path / "owned", tmp_path / "user"
    owned.mkdir()
    user.mkdir()

    def strip(env):
        kept = [e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e and e != str(owned)]
        if kept:
            env["PYTHONPATH"] = os.pathsep.join(kept)
        else:
            env.pop("PYTHONPATH", None)

    monkeypatch.setitem(
        sys.modules,
        "tools.environments.local_pythonpath",
        types.SimpleNamespace(_strip_hermes_owned_pythonpath_and_runtime_markers=strip),
    )
    monkeypatch.setitem(
        sys.modules, "agent.secret_scope", types.SimpleNamespace(is_multiplex_active=lambda: multiplex)
    )
    monkeypatch.setenv("PYTHONPATH", f"{owned}{os.pathsep}{user}")
    return owned, user


def test_under_multiplex_only_hermes_owned_entries_are_dropped(monkeypatch, tmp_path):
    owned, user = _hermes_env(monkeypatch, tmp_path, multiplex=True)
    (user / "probe_on_user_path.py").write_text("")
    (owned / "probe_on_owned_path.py").write_text("")
    assert worker_launch.report(module="probe_on_user_path")["ok"] is True
    got = worker_launch.report(module="probe_on_owned_path")
    assert (got["ok"], got["reason"]) == (False, "hermes_bin_unset")


def test_without_multiplex_own_profile_workers_keep_pythonpath_so_it_depends_on_the_assignee(
    monkeypatch, tmp_path
):
    owned, _ = _hermes_env(monkeypatch, tmp_path, multiplex=False)
    (owned / "probe_on_owned_path.py").write_text("")
    got = worker_launch.report(module="probe_on_owned_path")
    # Cards for this gateway's own profile start; cards for any other profile are scrubbed and do not.
    assert (got["ok"], got["reason"]) == (None, "assignee_dependent")


def test_without_multiplex_a_hermes_every_worker_can_import_is_fine(monkeypatch, tmp_path):
    _, user = _hermes_env(monkeypatch, tmp_path, multiplex=False)
    (user / "probe_on_user_path.py").write_text("")
    assert worker_launch.report(module="probe_on_user_path")["ok"] is True


def test_without_multiplex_a_hermes_no_worker_can_import_is_reported(monkeypatch, tmp_path):
    _hermes_env(monkeypatch, tmp_path, multiplex=False)
    got = worker_launch.report(module="deskrpg_worker_launch_probe_missing")
    assert (got["ok"], got["reason"]) == (False, "hermes_bin_unset")

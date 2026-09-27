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


def test_a_scrubbed_worker_gets_no_pythonpath(tmp_path, monkeypatch):
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
# PYTHONPATH the PM bootstrap exports; a worker for the gateway's own profile on a gateway without multiplexing keeps
# it. The three cases below are the ones measured on upstream main's PM runtime (2026-09-27).


def _hermes(monkeypatch, tmp_path, *, multiplex):
    """Hermes reachable only through the gateway's PYTHONPATH (the PM runtime), and the multiplex flag."""
    import types

    pm = tmp_path / "pm"
    pm.mkdir()
    (pm / "probe_hermes_on_pm_path.py").write_text("")
    monkeypatch.setenv("PYTHONPATH", str(pm))
    monkeypatch.setitem(
        sys.modules, "agent.secret_scope", types.SimpleNamespace(is_multiplex_active=lambda: multiplex)
    )
    return "probe_hermes_on_pm_path"


def test_measured_multiplex_gateway_without_hermes_bin_cannot_start_workers(monkeypatch, tmp_path):
    module = _hermes(monkeypatch, tmp_path, multiplex=True)
    got = worker_launch.report(module=module)
    assert (got["ok"], got["reason"]) == (False, "hermes_bin_unset")


def test_measured_standalone_profile_gateway_depends_on_the_assignee(monkeypatch, tmp_path):
    module = _hermes(monkeypatch, tmp_path, multiplex=False)
    got = worker_launch.report(module=module)
    # Its own profile's cards start; any other profile's cards are scrubbed and do not.
    assert (got["ok"], got["reason"]) == (None, "assignee_dependent")


def test_measured_multiplex_gateway_with_hermes_bin_starts_workers(monkeypatch, tmp_path):
    module = _hermes(monkeypatch, tmp_path, multiplex=True)
    monkeypatch.setenv("HERMES_BIN", _script(tmp_path / "hermes"))
    got = worker_launch.report(module=module)
    assert (got["ok"], got["reason"]) == (True, None)


def test_without_multiplex_a_hermes_every_worker_can_import_is_fine(monkeypatch, tmp_path):
    _hermes(monkeypatch, tmp_path, multiplex=False)
    assert worker_launch.report(module="json")["ok"] is True


def test_a_hermes_without_the_multiplex_flag_is_treated_as_multiplexing(monkeypatch, tmp_path):
    module = _hermes(monkeypatch, tmp_path, multiplex=True)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    assert worker_launch.report(module=module)["reason"] == "hermes_bin_unset"


def test_only_public_hermes_names_are_used():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(worker_launch))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in ("agent", "tools"):
            assert all(not alias.name.startswith("_") for alias in node.names), node.module

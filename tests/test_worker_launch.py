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


def test_the_probe_ignores_the_gateways_pythonpath(tmp_path, monkeypatch):
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
    calls = []
    real = worker_launch._module_importable
    monkeypatch.setattr(worker_launch, "_module_importable", lambda e, m: calls.append(m) or real(e, m))
    worker_launch.report(module="json")
    worker_launch.report(module="json")
    assert calls == ["json"]


def test_info_reports_null_when_the_check_fails(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(worker_launch, "report", boom)
    assert routes._info_worker_launch() is None

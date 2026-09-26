import types

from deskrpg_plugin.review_hooks import WorkerContext, worker_context

API = types.SimpleNamespace(get_active_profile_name=lambda: "impl")


def _clear(monkeypatch):
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_BOARD", "HERMES_PROFILE_NAME",
                 "HERMES_PROFILE"):
        monkeypatch.delenv(name, raising=False)


def test_not_a_kanban_worker(monkeypatch):
    _clear(monkeypatch)
    assert worker_context(API) is None


def test_reads_the_dispatcher_pins(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "office-1")
    monkeypatch.setenv("HERMES_PROFILE_NAME", "rev")
    assert worker_context(API) == WorkerContext("t_1", 7, "rev", "office-1")


def test_falls_back_to_the_profile_home_and_tolerates_missing_run(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "not-a-number")
    assert worker_context(API) == WorkerContext("t_1", None, "impl", None)

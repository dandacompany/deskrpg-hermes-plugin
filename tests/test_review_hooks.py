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


# --- Hooks on a fake kanban -------------------------------------------------------------------------------------

import pytest  # noqa: E402

from deskrpg_plugin import review_store as rs  # noqa: E402
from deskrpg_plugin.review_contract import (  # noqa: E402
    BLOCK_RETURN_MESSAGE, BLOCK_SUBMIT_MESSAGE, BLOCK_TERMINAL_MESSAGE, BLOCK_UNAVAILABLE_MESSAGE,
    BLOCK_VERDICT_MESSAGE,
)
from deskrpg_plugin.review_hooks import make_post_hook, make_pre_hook  # noqa: E402
from tests.fakes_kanban import DEFAULT_BOARD  # noqa: E402

REVIEW_DONE = '{"ok": true, "task_id": "t", "run_id": 1, "status": "review"}'


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKRPG_SHARED_DIR", str(tmp_path / "shared"))
    conn = rs.open_store(tmp_path / "shared" / "review.sqlite")
    yield conn
    conn.close()


@pytest.fixture
def card(fake_api, monkeypatch):
    """A card assigned to `impl`, and a worker environment for it. `claim(profile, from_review)` opens a run."""
    db = fake_api.kanban
    with fake_api.connect_closing(board=DEFAULT_BOARD) as conn:
        task_id = fake_api.create_task(conn, title="card", assignee="impl")

    def claim(profile, from_review=False):
        with fake_api.connect_closing(board=DEFAULT_BOARD) as conn:
            run_id = db._next()
            payload = {"source_status": "review"} if from_review else {}
            db._append_event(conn, task_id, "claimed", payload, run_id=run_id)
        _clear(monkeypatch)
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", DEFAULT_BOARD)
        monkeypatch.setenv("HERMES_PROFILE_NAME", profile)
        return run_id

    return task_id, claim


def _assigned(fake_api):
    return [(c["task_id"], c["profile"]) for c in fake_api.kanban.calls.get("assign_task", [])]


def test_implementer_cannot_complete_a_policy_card(fake_api, shared, card):
    task_id, claim = card
    rs.put_policy(shared, rs.Policy(task_id, "human", "impl", None, "card"))
    claim("impl")
    got = make_pre_hook(fake_api)(tool_name="kanban_complete", args={"summary": "x"})
    assert got == {"action": "block", "message": BLOCK_SUBMIT_MESSAGE}


def test_cards_without_policy_follow_upstream(fake_api, shared, card):
    _task_id, claim = card
    claim("impl")
    assert make_pre_hook(fake_api)(tool_name="kanban_complete", args={}) is None


def test_board_default_applies_to_a_card_without_its_own_row(fake_api, shared, card):
    _task_id, claim = card
    rs.put_board_default(shared, DEFAULT_BOARD, "human", None)
    claim("impl")
    got = make_pre_hook(fake_api)(tool_name="kanban_complete", args={})
    assert got == {"action": "block", "message": BLOCK_SUBMIT_MESSAGE}


def test_silent_outside_kanban_workers(fake_api, monkeypatch):
    _clear(monkeypatch)
    assert make_pre_hook(fake_api)(tool_name="kanban_complete", args={}) is None
    assert make_post_hook(fake_api)(tool_name="kanban_request_review", args={}, result=REVIEW_DONE) is None


def test_unrelated_tools_are_not_inspected(fake_api, card, monkeypatch):
    _task_id, claim = card
    claim("impl")
    monkeypatch.setattr("deskrpg_plugin.review_hooks.open_store", lambda p: pytest.fail("store opened"))
    assert make_pre_hook(fake_api)(tool_name="read_file", args={"path": "x"}) is None
    assert make_pre_hook(fake_api)(tool_name="terminal", args={"command": "ls"}) is None


def test_fails_closed_when_the_store_cannot_open(fake_api, card, monkeypatch):
    _task_id, claim = card
    claim("impl")
    monkeypatch.setattr("deskrpg_plugin.review_hooks.open_store", lambda p: (_ for _ in ()).throw(OSError("ro")))
    pre = make_pre_hook(fake_api)
    assert pre(tool_name="kanban_complete", args={}) == {"action": "block", "message": BLOCK_UNAVAILABLE_MESSAGE}
    assert pre(tool_name="terminal", args={"command": "hermes kanban complete t"})["action"] == "block"


def test_fails_closed_when_hermes_cannot_be_read(fake_api, shared, card, monkeypatch):
    task_id, claim = card
    rs.put_policy(shared, rs.Policy(task_id, "human", "impl", None, "card"))
    claim("impl")
    monkeypatch.setattr(fake_api, "get_task", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    got = make_pre_hook(fake_api)(tool_name="kanban_complete", args={})
    assert got == {"action": "block", "message": BLOCK_UNAVAILABLE_MESSAGE}
    # A failure never blocks a submission — that is the way forward.
    assert make_pre_hook(fake_api)(tool_name="kanban_request_review", args={"summary": "s"}) is None


def test_agent_submission_gets_the_policy_reviewer(fake_api, shared, card):
    task_id, claim = card
    rs.put_policy(shared, rs.Policy(task_id, "agent", "impl", "rev", "card"))
    claim("impl")
    got = make_pre_hook(fake_api)(tool_name="kanban_request_review", args={"summary": "s", "reviewer": "x"})
    assert got == {"action": "modify", "args": {"summary": "s", "reviewer": "rev"}}


def test_agent_reviewer_may_complete_and_mixed_reviewer_leaves_a_verdict(fake_api, shared, card):
    task_id, claim = card
    rs.put_policy(shared, rs.Policy(task_id, "agent", "impl", "rev", "card"))
    claim("rev", from_review=True)
    assert make_pre_hook(fake_api)(tool_name="kanban_complete", args={}) is None
    rs.put_policy(shared, rs.Policy(task_id, "mixed", "impl", "rev", "card"))
    got = make_pre_hook(fake_api)(tool_name="kanban_complete", args={})
    assert got == {"action": "block", "message": BLOCK_VERDICT_MESSAGE}


def test_a_run_that_slipped_onto_a_waiting_card_is_sent_back(fake_api, shared, card):
    task_id, claim = card
    rs.put_policy(shared, rs.Policy(task_id, "human", "impl", None, "card"))
    claim("impl", from_review=True)  # claimed from review in the gap before the card was unassigned
    pre = make_pre_hook(fake_api)
    assert pre(tool_name="kanban_complete", args={}) == {"action": "block", "message": BLOCK_RETURN_MESSAGE}
    assert pre(tool_name="kanban_request_changes", args={"reason": "x"}) == {
        "action": "block", "message": BLOCK_RETURN_MESSAGE}


def test_mixed_card_run_by_someone_other_than_the_reviewer_is_sent_back(fake_api, shared, card):
    task_id, claim = card
    rs.put_policy(shared, rs.Policy(task_id, "mixed", "impl", "rev", "card"))
    claim("impl", from_review=True)
    assert make_pre_hook(fake_api)(tool_name="kanban_complete", args={}) == {
        "action": "block", "message": BLOCK_RETURN_MESSAGE}


def test_shell_completion_is_blocked_on_policy_cards(fake_api, shared, card):
    task_id, claim = card
    rs.put_policy(shared, rs.Policy(task_id, "human", "impl", None, "card"))
    claim("impl")
    got = make_pre_hook(fake_api)(tool_name="terminal", args={"command": "hermes kanban complete " + task_id})
    assert got == {"action": "block", "message": BLOCK_TERMINAL_MESSAGE}


def test_post_hook_unassigns_after_a_human_submission(fake_api, shared, card):
    task_id, claim = card
    rs.put_policy(shared, rs.Policy(task_id, "human", "impl", None, "card"))
    claim("impl")
    make_post_hook(fake_api)(tool_name="kanban_request_review", args={}, result=REVIEW_DONE)
    assert _assigned(fake_api) == [(task_id, None)]


def test_post_hook_unassigns_after_a_mixed_verdict(fake_api, shared, card):
    task_id, claim = card
    rs.put_policy(shared, rs.Policy(task_id, "mixed", "impl", "rev", "card"))
    claim("rev", from_review=True)
    make_post_hook(fake_api)(tool_name="kanban_request_review", args={}, result=REVIEW_DONE)
    assert _assigned(fake_api) == [(task_id, None)]


def test_post_hook_leaves_agent_and_mixed_submissions_with_their_reviewer(fake_api, shared, card):
    task_id, claim = card
    for mode, reviewer in (("agent", "rev"), ("mixed", "rev")):
        rs.put_policy(shared, rs.Policy(task_id, mode, "impl", reviewer, "card"))
        claim("impl")
        make_post_hook(fake_api)(tool_name="kanban_request_review", args={}, result=REVIEW_DONE)
    assert _assigned(fake_api) == []


def test_post_hook_ignores_a_failed_submission(fake_api, shared, card):
    task_id, claim = card
    rs.put_policy(shared, rs.Policy(task_id, "human", "impl", None, "card"))
    claim("impl")
    make_post_hook(fake_api)(tool_name="kanban_request_review", args={}, result='{"error": "not in running"}')
    assert _assigned(fake_api) == []


# --- Registration and capability ---------------------------------------------------------------------------------

import types as _types  # noqa: E402

from deskrpg_plugin import contract_fields, review_hooks  # noqa: E402
from deskrpg_plugin import register as plugin_register  # noqa: E402


class _Ctx:
    def __init__(self, fail=()):
        self.hooks, self.fail = [], set(fail)

    def register_platform_handler(self, *a, **k):
        pass

    def register_tool(self, *a, **k):
        pass

    def register_system_prompt_section(self, *a, **k):
        pass

    def register_skill(self, *a, **k):
        pass

    def register_hook(self, name, fn):
        if name in self.fail:
            raise RuntimeError("refused")
        self.hooks.append(name)


def test_plugin_registers_both_review_hooks(fake_api, monkeypatch):
    monkeypatch.setattr(review_hooks, "HOOKS_REGISTERED", False)
    monkeypatch.setattr("deskrpg_plugin.load", lambda: fake_api)
    ctx = _Ctx()
    plugin_register(ctx)
    assert ctx.hooks.count("pre_tool_call") == 1
    assert review_hooks.HOOKS_REGISTERED is True


def test_capability_needs_both_hooks_registered(fake_api, monkeypatch):
    monkeypatch.setattr(review_hooks, "HOOKS_REGISTERED", False)
    monkeypatch.setattr("deskrpg_plugin.load", lambda: fake_api)
    plugin_register(_Ctx(fail={"pre_tool_call"}))
    assert review_hooks.HOOKS_REGISTERED is False
    assert "review_hooks_v1" not in contract_fields.capabilities(fake_api)


def test_capability_is_announced_when_hooks_and_store_are_ready(fake_api, shared, monkeypatch):
    monkeypatch.setattr(review_hooks, "HOOKS_REGISTERED", True)
    assert "review_hooks_v1" in contract_fields.capabilities(fake_api)


def test_capability_is_withheld_when_the_store_cannot_open(fake_api, monkeypatch):
    monkeypatch.setattr(review_hooks, "HOOKS_REGISTERED", True)
    monkeypatch.setattr("deskrpg_plugin.review_store.open_store", lambda p: (_ for _ in ()).throw(OSError("ro")))
    assert "review_hooks_v1" not in contract_fields.capabilities(fake_api)


def test_capability_needs_the_public_kanban_verbs(monkeypatch, shared):
    monkeypatch.setattr(review_hooks, "HOOKS_REGISTERED", True)
    bare = _types.SimpleNamespace(kanban_home=lambda: "/nonexistent")
    assert contract_fields.has_review_hooks(bare) is False

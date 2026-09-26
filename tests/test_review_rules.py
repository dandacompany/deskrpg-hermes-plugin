from deskrpg_plugin.review_contract import (
    BLOCK_RETURN_MESSAGE, BLOCK_SUBMIT_MESSAGE, BLOCK_TERMINAL_MESSAGE, BLOCK_UNAVAILABLE_MESSAGE, BLOCK_VERDICT_MESSAGE,
)
from deskrpg_plugin.review_rules import (
    Situation, decide_pre, is_terminal_completion, resolve_policy, should_release_to_human,
)
from deskrpg_plugin.review_store import Policy

H = Policy("t", "human", "impl", None, "card")
A = Policy("t", "agent", "impl", "rev", "card")
M = Policy("t", "mixed", "impl", "rev", "card")


def s(policy, caller="impl", reviewer_run=False, waiting=False, ok=True):
    return Situation(policy, caller, reviewer_run, waiting, ok)


def test_card_policy_wins_over_board_default():
    assert resolve_policy(A, ("human", None), "t", "impl") == A


def test_board_default_applies_when_card_has_none():
    got = resolve_policy(None, ("human", None), "t", "impl")
    assert (got.mode, got.implementer, got.source) == ("human", "impl", "board_default")


def test_no_policy_anywhere_means_upstream_behaviour():
    assert resolve_policy(None, None, "t", "impl") is None
    assert decide_pre("kanban_complete", {}, s(None)) is None


def test_implementer_cannot_complete_in_any_mode():
    for p in (H, A, M):
        assert decide_pre("kanban_complete", {}, s(p)) == {"action": "block", "message": BLOCK_SUBMIT_MESSAGE}


def test_agent_reviewer_may_complete():
    assert decide_pre("kanban_complete", {}, s(A, caller="rev", reviewer_run=True)) is None


def test_mixed_reviewer_must_leave_a_verdict():
    got = decide_pre("kanban_complete", {}, s(M, caller="rev", reviewer_run=True))
    assert got == {"action": "block", "message": BLOCK_VERDICT_MESSAGE}


def test_run_on_a_card_waiting_for_a_person_is_sent_back():
    got = decide_pre("kanban_complete", {}, s(H, caller="impl", waiting=True))
    assert got == {"action": "block", "message": BLOCK_RETURN_MESSAGE}


def test_store_unavailable_blocks_completion():
    got = decide_pre("kanban_complete", {}, s(None, ok=False))
    assert got == {"action": "block", "message": BLOCK_UNAVAILABLE_MESSAGE}


def test_submission_gets_the_policy_reviewer_and_ignores_the_models_choice():
    assert decide_pre("kanban_request_review", {"reviewer": "x", "summary": "s"}, s(A)) == {
        "action": "modify", "args": {"reviewer": "rev", "summary": "s"}}
    assert decide_pre("kanban_request_review", {"reviewer": "x", "summary": "s"}, s(H)) == {
        "action": "modify", "args": {"summary": "s", "reviewer": None}}


def test_mixed_reviewer_verdict_goes_to_a_person_without_reviewer():
    got = decide_pre("kanban_request_review", {"reviewer": "rev", "summary": "pass"}, s(M, caller="rev", reviewer_run=True))
    assert got == {"action": "modify", "args": {"summary": "pass", "reviewer": None}}


def test_release_to_human_cases():
    ok = {"ok": True, "status": "review"}
    assert should_release_to_human("kanban_request_review", ok, s(H))
    assert should_release_to_human("kanban_request_review", ok, s(M, caller="rev", reviewer_run=True))
    assert not should_release_to_human("kanban_request_review", ok, s(A))
    assert not should_release_to_human("kanban_request_review", ok, s(M))
    assert not should_release_to_human("kanban_request_review", {"ok": False}, s(H))


def test_terminal_completion_detection():
    for cmd in ("hermes kanban complete t_1", "HERMES_HOME=x hermes kanban done t_1",
                "hermes -p impl kanban complete t_1 --summary ok", "hermes kanban move t_1 --status done"):
        assert is_terminal_completion(cmd)
    for cmd in ("hermes kanban show t_1", "echo complete", "hermes kanban list"):
        assert not is_terminal_completion(cmd)


def test_terminal_tool_is_blocked_on_policy_cards():
    got = decide_pre("terminal", {"command": "hermes kanban complete t"}, s(H))
    assert got == {"action": "block", "message": BLOCK_TERMINAL_MESSAGE}
    assert decide_pre("terminal", {"command": "ls"}, s(H)) is None


def test_run_on_a_card_waiting_for_a_person_cannot_send_it_back():
    got = decide_pre("kanban_request_changes", {"reason": "x"}, s(H, caller="impl", waiting=True))
    assert got == {"action": "block", "message": BLOCK_RETURN_MESSAGE}
    # A real AI review may still request changes.
    assert decide_pre("kanban_request_changes", {"reason": "x"}, s(A, caller="rev", reviewer_run=True)) is None


def test_shell_completion_is_refused_when_the_store_is_unavailable():
    got = decide_pre("terminal", {"command": "hermes kanban complete t"}, s(None, ok=False))
    assert got == {"action": "block", "message": BLOCK_UNAVAILABLE_MESSAGE}
    assert decide_pre("terminal", {"command": "hermes kanban complete t"}, s(None)) is None


def test_agent_decisions_are_the_reviewers_completion_and_request_for_changes():
    from deskrpg_plugin.review_rules import agent_decision

    ok = {"ok": True}
    rev = lambda p: s(p, caller="rev", reviewer_run=True)  # noqa: E731
    assert agent_decision("kanban_complete", {"summary": "LGTM"}, ok, rev(A)) == ("approve", "LGTM")
    assert agent_decision("kanban_request_changes", {"reason": "fix"}, ok, rev(A)) == ("reject", "fix")
    assert agent_decision("kanban_request_changes", {"reason": "fix"}, ok, rev(M)) == ("reject", "fix")
    # A mixed verdict handed to a person is an opinion, not a decision.
    assert agent_decision("kanban_request_review", {"summary": "pass"}, ok, rev(M)) is None
    assert agent_decision("kanban_complete", {"summary": "x"}, ok, rev(M)) is None
    # Not a review run, no policy, or a failed call: nothing to record.
    assert agent_decision("kanban_complete", {"summary": "x"}, ok, s(A)) is None
    assert agent_decision("kanban_complete", {"summary": "x"}, ok, s(None, reviewer_run=True)) is None
    assert agent_decision("kanban_complete", {"summary": "x"}, {"error": "no"}, rev(A)) is None

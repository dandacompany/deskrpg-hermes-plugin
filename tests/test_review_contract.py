from deskrpg_plugin import review_contract as c


def test_block_messages_point_at_an_official_kanban_tool():
    for message in (c.BLOCK_SUBMIT_MESSAGE, c.BLOCK_VERDICT_MESSAGE, c.BLOCK_RETURN_MESSAGE,
                    c.BLOCK_TERMINAL_MESSAGE, c.BLOCK_UNAVAILABLE_MESSAGE):
        assert "kanban_request_review" in message


def test_contract_names():
    assert c.REVIEW_HOOKS_CAPABILITY == "review_hooks_v1"
    assert c.REVIEW_MODES == ("human", "agent", "mixed")
    assert c.SIDECAR_RELATIVE[-1] == "review.sqlite"

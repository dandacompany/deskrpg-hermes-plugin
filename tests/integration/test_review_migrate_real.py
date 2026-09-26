"""The migration on a real Hermes: a policy-patched core's own table moves into the approval store; an upstream
core has no patch table and nothing moves."""

import pytest

from deskrpg_plugin import review_migrate, review_store
from deskrpg_plugin.common import board_conn
from deskrpg_plugin.contract_fields import has_review_policy
from tests.integration.test_kanban_real import BOARD, B, _board

pytestmark = pytest.mark.integration

POLICY = {"version": 1, "mode": "human", "reviewer_profile": None}


async def test_migrates_what_the_core_holds(client, api, profile, tmp_path, monkeypatch):
    monkeypatch.setenv("DESKRPG_SHARED_DIR", str(tmp_path / "shared"))
    await _board(client)
    extra = {"review_policy": POLICY} if has_review_policy(api) else {}
    created = await (await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "card", "assignee": profile, **extra})).json()
    tid = created["task"]["id"]
    store = review_store.open_store(review_store.sidecar_path(api))
    try:
        with board_conn(api, BOARD) as conn:
            result = review_migrate.migrate(api, BOARD, conn, store)
        if has_review_policy(api):
            assert result["policies"] == 1 and result["skipped"] == []
            assert review_store.get_policy(store, tid) == review_store.Policy(tid, "human", profile, None, "migrated")
        else:
            assert result == {"policies": 0, "approvals": 0, "waiting_human": 0, "skipped": []}
    finally:
        store.close()

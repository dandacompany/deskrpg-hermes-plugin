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


async def test_the_trigger_list_matches_what_the_core_installs(client, api, profile, tmp_path, monkeypatch):
    """On the patched core every trigger it installs is on our list, and dropping them leaves none of the patch's;
    on an upstream core there is nothing to drop."""
    import sqlite3

    monkeypatch.setenv("DESKRPG_SHARED_DIR", str(tmp_path / "shared"))
    await _board(client)
    extra = {"review_policy": POLICY} if has_review_policy(api) else {}
    await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "card", "assignee": profile, **extra})
    db = api.kanban_db_path(board=BOARD)

    def names():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        finally:
            conn.close()

    patch_like = {n for n in names() if n.startswith("review_")}
    store = review_store.open_store(review_store.sidecar_path(api))
    try:
        with board_conn(api, BOARD) as conn:
            result = review_migrate.migrate(api, BOARD, conn, store, drop_triggers=True)
    finally:
        store.close()
    if has_review_policy(api):
        assert patch_like == set(review_migrate.PATCH_TRIGGERS), patch_like ^ set(review_migrate.PATCH_TRIGGERS)
        assert sorted(result["dropped_triggers"]) == sorted(review_migrate.PATCH_TRIGGERS)
    else:
        assert result["dropped_triggers"] == []
    assert not (names() & set(review_migrate.PATCH_TRIGGERS))

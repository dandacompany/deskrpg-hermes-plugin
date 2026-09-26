"""One-time move of patch-era approval policies into the approval store — the patch table is only ever read."""

import json
import sqlite3

import pytest

from deskrpg_plugin import review_migrate
from tests.fakes_kanban_actions import install_fake_kanban_actions
from tests.review_fixtures import rs, store  # noqa: F401 — fixtures

BOARD = "deskrpg-abc"
HUMAN = {"version": 1, "mode": "human", "reviewer_profile": None}
AGENT = {"version": 1, "mode": "agent", "reviewer_profile": "rev"}


@pytest.fixture
def kanban(fake_api, tmp_path):
    db = install_fake_kanban_actions(fake_api, tmp_path / "kanban")
    db.create_board(BOARD, name="DeskRPG")
    return db


@pytest.fixture
def patch_db(fake_api, tmp_path):
    """A board database file holding the patch's `task_review_policies` table."""
    path = tmp_path / "board.db"
    fake_api.kanban_db_path = lambda board=None: path
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE task_review_policies (
        task_id TEXT PRIMARY KEY, policy TEXT NOT NULL, policy_revision INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL DEFAULT 'awaiting_submission', review_round INTEGER NOT NULL DEFAULT 0,
        submission TEXT, approval TEXT, review_run_id INTEGER, require_human INTEGER NOT NULL DEFAULT 0, reason TEXT)""")
    conn.commit()
    yield path, conn
    conn.close()


def _row(conn, task_id, policy, state, submission=None, approval=None):
    conn.execute(
        "INSERT INTO task_review_policies(task_id, policy, state, submission, approval) VALUES (?,?,?,?,?)",
        (task_id, json.dumps(policy), state, json.dumps(submission) if submission else None,
         json.dumps(approval) if approval else None))
    conn.commit()


def _cards(kanban):
    """A human card waiting for a person in review, an agent card being worked on, and an approved card."""
    conn = kanban.connect(board=BOARD)
    waiting = kanban.create_task(conn, title="waiting", assignee="impl")
    run = kanban.start_run(conn, waiting, profile="impl")
    kanban.request_review(conn, waiting, summary="r", expected_run_id=run)
    working = kanban.create_task(conn, title="working", assignee="impl")
    approved = kanban.create_task(conn, title="approved", assignee="impl")
    kanban.complete_task(conn, approved, summary="ok")
    return conn, waiting, working, approved


def test_policies_approvals_and_waiting_cards_are_moved(fake_api, kanban, store, patch_db):
    _path, pconn = patch_db
    conn, waiting, working, approved = _cards(kanban)
    _row(pconn, waiting, HUMAN, "human_required", submission={"implementer": "impl", "run_id": 1})
    _row(pconn, working, AGENT, "awaiting_submission")
    _row(pconn, approved, HUMAN, "approved", approval={"actor_kind": "human", "actor_id": "deskrpg:u1"})

    result = review_migrate.migrate(fake_api, BOARD, conn, store)

    assert result == {"policies": 3, "approvals": 1, "waiting_human": 1, "skipped": []}
    assert rs.get_policy(store, waiting) == rs.Policy(waiting, "human", "impl", None, "migrated")
    assert rs.get_policy(store, working) == rs.Policy(working, "agent", "impl", "rev", "migrated")
    assert [(d["actor"], d["verdict"]) for d in rs.decisions(store, approved)] == [("deskrpg:u1", "approve")]
    assert [c for c in kanban.calls["assign_task"] if c["task_id"] == waiting] == [{"task_id": waiting, "profile": None}]
    assert kanban.get_task(conn, waiting).assignee is None


def test_dry_run_writes_nothing(fake_api, kanban, store, patch_db):
    _path, pconn = patch_db
    conn, waiting, _working, approved = _cards(kanban)
    _row(pconn, waiting, HUMAN, "human_required")
    _row(pconn, approved, HUMAN, "approved", approval={"actor_kind": "human", "actor_id": "deskrpg:u1"})
    before = len(kanban.calls.get("assign_task", []))

    result = review_migrate.migrate(fake_api, BOARD, conn, store, dry_run=True)

    assert (result["policies"], result["approvals"], result["waiting_human"]) == (2, 1, 1)
    assert rs.get_policy(store, waiting) is None and rs.decisions(store, approved) == []
    assert len(kanban.calls.get("assign_task", [])) == before


def test_running_it_twice_does_not_duplicate_approvals(fake_api, kanban, store, patch_db):
    _path, pconn = patch_db
    conn, _waiting, _working, approved = _cards(kanban)
    _row(pconn, approved, HUMAN, "approved", approval={"actor_kind": "human", "actor_id": "deskrpg:u1"})
    review_migrate.migrate(fake_api, BOARD, conn, store)
    again = review_migrate.migrate(fake_api, BOARD, conn, store)
    assert again["approvals"] == 0 and len(rs.decisions(store, approved)) == 1


def test_no_patch_table_means_nothing_to_move(fake_api, kanban, store, tmp_path):
    path = tmp_path / "plain.db"
    sqlite3.connect(path).close()
    fake_api.kanban_db_path = lambda board=None: path
    conn = kanban.connect(board=BOARD)
    assert review_migrate.migrate(fake_api, BOARD, conn, store) == {
        "policies": 0, "approvals": 0, "waiting_human": 0, "skipped": []}


def test_rows_that_cannot_move_are_reported_not_guessed(fake_api, kanban, store, patch_db):
    _path, pconn = patch_db
    conn = kanban.connect(board=BOARD)
    ready = kanban.create_task(conn, title="waiting but not in review", assignee="impl")
    _row(pconn, "t_gone", HUMAN, "awaiting_submission")
    _row(pconn, ready, HUMAN, "human_required")
    _row(pconn, kanban.create_task(conn, title="bad", assignee="impl"), {"version": 1, "mode": "agent"}, "submitted")
    result = review_migrate.migrate(fake_api, BOARD, conn, store)
    assert sorted(s["reason"] for s in result["skipped"]) == [
        "agent review needs a reviewer profile", "task_not_found", "waiting_card_in_ready"]


def test_the_patch_table_is_opened_read_only(fake_api, kanban, store, patch_db, monkeypatch):
    path, pconn = patch_db
    conn, waiting, _working, _approved = _cards(kanban)
    _row(pconn, waiting, HUMAN, "human_required")
    opened = []
    real_connect = sqlite3.connect

    def spy(target, *a, **kw):
        opened.append((str(target), kw.get("uri")))
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(review_migrate.sqlite3, "connect", spy)
    review_migrate.migrate(fake_api, BOARD, conn, store)
    assert opened == [(f"file:{path}?mode=ro", True)]
    with pytest.raises(sqlite3.OperationalError):
        ro = real_connect(f"file:{path}?mode=ro", uri=True)
        ro.execute("DELETE FROM task_review_policies")

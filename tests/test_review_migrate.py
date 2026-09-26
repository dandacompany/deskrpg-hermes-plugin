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


def _install_patch_triggers(path):
    """The patch's trigger names on a board database, with its real done guard (the one that blocks completion)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT, title TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS task_attachments (id INTEGER PRIMARY KEY, task_id TEXT)")
    conn.execute("""CREATE TRIGGER review_policy_done_guard
        BEFORE UPDATE OF status ON tasks
        WHEN NEW.status = 'done' AND OLD.status != 'done'
         AND EXISTS (SELECT 1 FROM task_review_policies WHERE task_id = NEW.id
                     AND (state != 'approved' OR approval IS NULL OR submission IS NULL))
        BEGIN SELECT RAISE(ABORT, 'review policy requires explicit approval'); END""")
    for name in review_migrate.PATCH_TRIGGERS[1:]:
        conn.execute(f"CREATE TRIGGER {name} AFTER UPDATE OF title ON tasks BEGIN SELECT 1; END")
    conn.execute("CREATE TRIGGER someone_elses_trigger AFTER UPDATE OF title ON tasks BEGIN SELECT 1; END")
    conn.commit()
    conn.close()


def _triggers(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    finally:
        conn.close()


def test_dropping_patch_triggers_lets_an_unapproved_card_complete(fake_api, kanban, store, patch_db):
    path, pconn = patch_db
    conn = kanban.connect(board=BOARD)
    tid = kanban.create_task(conn, title="card", assignee="impl")
    _row(pconn, tid, HUMAN, "awaiting_submission")
    _install_patch_triggers(path)
    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO tasks(id, status, title) VALUES (?, 'review', 'card')", (tid,))
    raw.commit()
    with pytest.raises(sqlite3.IntegrityError, match="explicit approval"):
        raw.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
    raw.rollback()

    result = review_migrate.migrate(fake_api, BOARD, conn, store, drop_triggers=True)

    assert result["dropped_triggers"] == list(review_migrate.PATCH_TRIGGERS)
    assert _triggers(path) == {"someone_elses_trigger"}
    # The patch's tables stay as a record.
    assert raw.execute("SELECT COUNT(*) FROM task_review_policies").fetchone()[0] == 1
    raw.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
    raw.commit()
    raw.close()
    # A second run finds nothing left to drop.
    assert review_migrate.migrate(fake_api, BOARD, conn, store, drop_triggers=True)["dropped_triggers"] == []


def test_dry_run_lists_the_triggers_it_would_drop_and_keeps_them(fake_api, kanban, store, patch_db):
    path, _pconn = patch_db
    _install_patch_triggers(path)
    conn = kanban.connect(board=BOARD)
    result = review_migrate.migrate(fake_api, BOARD, conn, store, dry_run=True, drop_triggers=True)
    assert result["dropped_triggers"] == list(review_migrate.PATCH_TRIGGERS)
    assert set(review_migrate.PATCH_TRIGGERS) <= _triggers(path)


def test_triggers_are_left_alone_unless_asked(fake_api, kanban, store, patch_db):
    path, _pconn = patch_db
    _install_patch_triggers(path)
    result = review_migrate.migrate(fake_api, BOARD, kanban.connect(board=BOARD), store)
    assert "dropped_triggers" not in result
    assert set(review_migrate.PATCH_TRIGGERS) <= _triggers(path)

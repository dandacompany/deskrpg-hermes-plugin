import sqlite3
from pathlib import Path

import pytest

from deskrpg_plugin import review_store as rs


def test_policy_round_trip(tmp_path: Path):
    conn = rs.open_store(tmp_path / "review.sqlite")
    rs.put_policy(conn, rs.Policy("t_1", "mixed", "impl", "rev", "card"))
    assert rs.get_policy(conn, "t_1") == rs.Policy("t_1", "mixed", "impl", "rev", "card")
    assert rs.get_policy(conn, "t_missing") is None


def test_mixed_without_reviewer_is_rejected(tmp_path: Path):
    conn = rs.open_store(tmp_path / "review.sqlite")
    with pytest.raises(ValueError):
        rs.put_policy(conn, rs.Policy("t_1", "mixed", "impl", None, "card"))


def test_unknown_mode_is_rejected(tmp_path: Path):
    conn = rs.open_store(tmp_path / "review.sqlite")
    with pytest.raises(ValueError):
        rs.put_policy(conn, rs.Policy("t_1", "auto", "impl", None, "card"))


def test_board_default_and_decisions(tmp_path: Path):
    conn = rs.open_store(tmp_path / "review.sqlite")
    rs.put_board_default(conn, "office-1", "human", None)
    assert rs.get_board_default(conn, "office-1") == ("human", None)
    rs.record_decision(conn, "t_1", "human", "deskrpg:u1", "approve", "ok")
    assert [d["verdict"] for d in rs.decisions(conn, "t_1")] == ["approve"]


def test_store_uses_wal(tmp_path: Path):
    conn = rs.open_store(tmp_path / "review.sqlite")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_env_overrides_location(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DESKRPG_SHARED_DIR", str(tmp_path / "shared"))
    assert rs.sidecar_path(api=None) == tmp_path / "shared" / "review.sqlite"


def test_agent_without_reviewer_is_rejected(tmp_path: Path):
    conn = rs.open_store(tmp_path / "review.sqlite")
    with pytest.raises(ValueError):
        rs.put_policy(conn, rs.Policy("t_1", "agent", "impl", None, "card"))
    with pytest.raises(ValueError):
        rs.put_board_default(conn, "office-1", "agent", None)


def test_default_location_is_under_the_shared_kanban_home(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DESKRPG_SHARED_DIR", raising=False)

    class Api:
        def kanban_home(self):
            return str(tmp_path / "root")

    assert rs.sidecar_path(Api()) == tmp_path / "root" / "plugin-data" / "deskrpg-shared" / "review.sqlite"


def test_policy_update_replaces_the_row(tmp_path: Path):
    conn = rs.open_store(tmp_path / "review.sqlite")
    rs.put_policy(conn, rs.Policy("t_1", "human", "impl", None, "card"))
    rs.put_policy(conn, rs.Policy("t_1", "agent", "impl", "rev", "board_default"))
    assert rs.get_policy(conn, "t_1") == rs.Policy("t_1", "agent", "impl", "rev", "board_default")


def test_clear_board_default(tmp_path: Path):
    conn = rs.open_store(tmp_path / "review.sqlite")
    rs.put_board_default(conn, "office-1", "human", None)
    assert rs.clear_board_default(conn, "office-1") is True
    assert rs.get_board_default(conn, "office-1") is None
    assert rs.clear_board_default(conn, "office-1") is False

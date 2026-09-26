"""Stand-in for `deskrpg_plugin.review_store` until the real module lands — the interface the plan fixes.

Installed as `deskrpg_plugin.review_store` by `tests/review_fixtures.py` only when the real module is absent."""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from deskrpg_plugin.review_contract import REVIEW_MODES, SHARED_DIR_ENV, SIDECAR_RELATIVE

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_policies (
  task_id TEXT PRIMARY KEY, mode TEXT NOT NULL, implementer TEXT, reviewer_profile TEXT,
  source TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS board_defaults (
  board TEXT PRIMARY KEY, mode TEXT NOT NULL, reviewer_profile TEXT, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, actor_kind TEXT NOT NULL, actor TEXT NOT NULL,
  verdict TEXT NOT NULL, summary TEXT, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS review_decisions_task ON review_decisions(task_id, id);
"""


@dataclass(frozen=True)
class Policy:
    task_id: str
    mode: str
    implementer: str | None
    reviewer_profile: str | None
    source: str


def sidecar_path(api) -> Path:
    override = os.environ.get(SHARED_DIR_ENV)
    if override:
        return Path(override) / SIDECAR_RELATIVE[-1]
    return Path(api.kanban_home()).joinpath(*SIDECAR_RELATIVE)


def open_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


def _validate(mode: str, reviewer_profile: str | None) -> None:
    if mode not in REVIEW_MODES:
        raise ValueError(f"unknown review mode: {mode}")
    if mode in ("agent", "mixed") and not reviewer_profile:
        raise ValueError(f"{mode} review needs a reviewer profile")


def put_policy(conn, policy: Policy) -> None:
    _validate(policy.mode, policy.reviewer_profile)
    conn.execute(
        "INSERT INTO review_policies(task_id, mode, implementer, reviewer_profile, source, created_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET mode=excluded.mode, "
        "implementer=excluded.implementer, reviewer_profile=excluded.reviewer_profile, source=excluded.source",
        (policy.task_id, policy.mode, policy.implementer, policy.reviewer_profile, policy.source, time.time()),
    )


def get_policy(conn, task_id: str) -> Policy | None:
    row = conn.execute(
        "SELECT task_id, mode, implementer, reviewer_profile, source FROM review_policies WHERE task_id=?", (task_id,)
    ).fetchone()
    return Policy(*row) if row else None


def put_board_default(conn, board: str, mode: str, reviewer_profile: str | None) -> None:
    _validate(mode, reviewer_profile)
    conn.execute(
        "INSERT INTO board_defaults(board, mode, reviewer_profile, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(board) DO UPDATE SET mode=excluded.mode, reviewer_profile=excluded.reviewer_profile, "
        "updated_at=excluded.updated_at",
        (board, mode, reviewer_profile, time.time()),
    )


def get_board_default(conn, board: str):
    row = conn.execute("SELECT mode, reviewer_profile FROM board_defaults WHERE board=?", (board,)).fetchone()
    return (row[0], row[1]) if row else None


def record_decision(conn, task_id, actor_kind, actor, verdict, summary) -> int:
    cur = conn.execute(
        "INSERT INTO review_decisions(task_id, actor_kind, actor, verdict, summary, at) VALUES (?,?,?,?,?,?)",
        (task_id, actor_kind, actor, verdict, summary, time.time()),
    )
    return int(cur.lastrowid)


def decisions(conn, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, actor_kind, actor, verdict, summary, at FROM review_decisions WHERE task_id=? ORDER BY id",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]

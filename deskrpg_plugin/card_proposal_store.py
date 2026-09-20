"""카드 제안 저장소 — 아티팩트 레지스트리(`registry.db`)에 표 하나를 더 둔다.

제안은 **카드가 아니다.** 프로필이 대화 중 "업무 카드로 남길 만한 요청" 을 찾으면 여기에 기록만 하고,
실제 카드 등록은 사용자가 DeskRPG 에서 버튼으로 고른다. 이 모듈은 칸반(정본)에 쓰지 않는다.

연결·스키마 보장·사건 append 는 `artifacts_store.py` 의 모양을 따른다. 사건은 아티팩트와 같은
`artifact_events` 표에 쌓아 `/deskrpg/events` 의 네 번째 출처(`a`)로 그대로 흘려보낸다 —
DeskRPG 는 `include=card_proposals` 로 옵트인했을 때만 이 종류를 받는다.

전부 블로킹이다. 라우트 핸들러는 `run_blocking` 안에서, 도구는 Hermes 가 주는 워커 스레드에서 부른다.
"""

import json
import sqlite3
import time
from uuid import uuid4

from . import artifacts_store as _artifacts

EVENT_CREATED = "card_proposal.created"

_SCHEMA = """CREATE TABLE IF NOT EXISTS card_proposals (
  proposal_id TEXT PRIMARY KEY,
  profile TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  body TEXT,
  acceptance TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_choice TEXT,
  resolved_task_id TEXT
)"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """멱등. 아티팩트 스키마의 `user_version` 마이그레이션과 섞지 않는다 — 표가 하나뿐이라
    `CREATE TABLE IF NOT EXISTS` 로 충분하고, 이 표가 늘어도 아티팩트 버전이 움직이지 않는다."""
    conn.execute(_SCHEMA)


def open_store(api) -> sqlite3.Connection:
    conn = _artifacts.open_registry(api)
    ensure_schema(conn)
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_event(conn: sqlite3.Connection, ts: int, kind: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO artifact_events (ts, kind, payload) VALUES (?,?,?)",
        (ts, kind, json.dumps({k: v for k, v in payload.items() if v is not None}, ensure_ascii=False)),
    )


def create(api, *, profile: str, title: str, summary: str, body=None, acceptance=None) -> str:
    """제안을 남기고 `proposal_id` 를 준다. 행과 사건은 한 트랜잭션이다 —
    사건만 남고 행이 없으면 DeskRPG 가 해소할 수 없는 제안을 보게 된다."""
    proposal_id = uuid4().hex
    created_at = _now()
    conn = open_store(api)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO card_proposals (proposal_id, profile, title, summary, body, acceptance, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (proposal_id, profile, title, summary, body, acceptance, created_at),
            )
            _append_event(conn, int(time.time()), EVENT_CREATED, {
                "proposal_id": proposal_id, "title": title, "summary": summary,
                "body": body, "acceptance": acceptance, "profile": profile,
            })
    finally:
        conn.close()
    return proposal_id


def resolve(api, proposal_id: str, choice: str, task_id=None) -> bool:
    """방금 해소했으면 True, 이미 해소됐거나 없는 제안이면 False.

    판정은 `UPDATE … WHERE resolved_at IS NULL` 의 `rowcount` 다 — 읽고 나서 쓰면 동시에 들어온
    두 요청이 둘 다 통과해 카드가 두 장 생긴다."""
    conn = open_store(api)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE card_proposals SET resolved_at=?, resolved_choice=?, resolved_task_id=?"
                " WHERE proposal_id=? AND resolved_at IS NULL",
                (_now(), choice, task_id, proposal_id),
            )
            return cur.rowcount == 1
    finally:
        conn.close()


def get(api, proposal_id: str):
    conn = open_store(api)
    try:
        row = conn.execute("SELECT * FROM card_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None

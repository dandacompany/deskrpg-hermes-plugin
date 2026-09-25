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
    두 요청이 둘 다 통과해 카드가 두 장 생긴다.

    `task_id` 는 `card` 일 때만 적는다. `inline` 은 카드를 만들지 않으므로, 거기 카드 id 가 적히면
    카드는 없는데 `unresolve` 가 영영 막힌다(`record_task` 와 같은 갈래 가드)."""
    conn = open_store(api)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE card_proposals SET resolved_at=?, resolved_choice=?, resolved_task_id=?"
                " WHERE proposal_id=? AND resolved_at IS NULL",
                (_now(), choice, task_id if choice == "card" else None, proposal_id),
            )
            return cur.rowcount == 1
    finally:
        conn.close()


def unresolve(api, proposal_id: str) -> bool:
    """해소를 되돌렸으면 True. **카드가 기록된 제안(`resolved_task_id` 가 있는 것)은 절대 다시 열지 않는다.**

    되돌릴 수 있는 것은 "해소는 됐는데 카드가 기록되지 않은" 반쪽 상태뿐이다 — 카드 생성이 실패한 그 상태가
    정확히 롤백이 필요한 상태이고, 그 조건 덕에 되돌리기로 두 번째 카드를 만드는 길이 없다.
    판정은 `resolve` 와 같이 단일 `UPDATE` 의 `rowcount` 다."""
    conn = open_store(api)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE card_proposals SET resolved_at=NULL, resolved_choice=NULL"
                " WHERE proposal_id=? AND resolved_at IS NOT NULL AND resolved_task_id IS NULL",
                (proposal_id,),
            )
            return cur.rowcount == 1
    finally:
        conn.close()


def record_task(api, proposal_id: str, task_id: str) -> bool:
    """해소된 제안에 만들어진 카드 id 를 적었으면 True.

    DeskRPG 는 카드를 만들기 **전에** `resolve` 를 부르므로(그 순서가 "한 번만 해소" 를 보장한다)
    카드 id 는 사후에만 적을 수 있다. 이 값이 채워지는 순간부터 `unresolve` 가 그 제안을 막는다 —
    그게 카드 중복을 막는 이중 방어이고, 이 경로가 없으면 그 가드가 영영 놀게 된다.
    `resolved_choice='card'` 인 제안에만 적는다 — `inline` 로 해소된 제안에는 만들어진 카드가 없으므로
    카드 id 가 적힐 자리가 아니다(DeskRPG 도 그 갈래에서 이 경로를 부르지 않는다).
    판정은 `resolve`·`unresolve` 와 같이 단일 `UPDATE` 의 `rowcount` 다."""
    conn = open_store(api)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE card_proposals SET resolved_task_id=?"
                " WHERE proposal_id=? AND resolved_at IS NOT NULL AND resolved_task_id IS NULL"
                " AND resolved_choice='card'",
                (task_id, proposal_id),
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

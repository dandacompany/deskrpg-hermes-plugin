"""사건 스트림(T5) 테스트용 가짜 — `tests/fakes_kanban.py` 를 **상속**해 raw SQL 을 받는 연결을 더한다.

`events.py` 는 대시보드처럼 `task_events` 를 raw SQL 로 tail 한다. `FakeConn` 은 슬러그만 든 표식이라
`execute` 가 없으므로, 여기서 `events.SQL_*` 문자열을 **그대로** 대조해 인메모리 사건 목록으로 답하는
연결을 만든다 — SQL 이 바뀌면 이 파일도 같이 바뀌어야 하고, 모르는 SQL 은 AssertionError 로 잡힌다.

그 밖에 테스트가 상태를 심는 도우미: `emit`(kind·ts·payload 를 고른 사건 행), `remove_task`(Hermes 가
카드와 사건을 같이 지운 뒤 상태), `set_status`, 그리고 삭제 기록 파일에 §5.3 형식 그대로 한 줄을 붙이는
`append_deleted`.
"""

import json
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

from deskrpg_plugin import events
from tests.fakes_kanban import KANBAN_DB_SYMBOLS, FakeConn, FakeEvent, FakeKanbanDb


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeEventsConn(FakeConn):
    """`execute(sql, params)` 를 받는 연결. `events.SQL_*` 세 문장만 안다."""

    def __init__(self, db, board):
        super().__init__(board=board)
        self.db = db
        self.executed = []

    def _rows(self):
        return sorted(self.db.boards[self.board].events, key=lambda e: e.id)

    @staticmethod
    def _row(ev: FakeEvent) -> dict:
        # 진짜 sqlite 행처럼 payload 는 JSON 문자열(없으면 None)이다.
        payload = json.dumps(ev.payload) if ev.payload is not None else None
        return {
            "id": ev.id, "task_id": ev.task_id, "run_id": ev.run_id, "kind": ev.kind,
            "payload": payload, "created_at": ev.created_at,
        }

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))
        if sql == events.SQL_TAIL:
            since, limit = params
            rows = [self._row(e) for e in self._rows() if e.id > since][:limit]
            return _Result(rows)
        if sql == events.SQL_EARLIER_FOR_TASK:
            task_id, before = params
            rows = [self._row(e) for e in reversed(self._rows()) if e.task_id == task_id and e.id < before]
            return _Result([{"kind": r["kind"], "payload": r["payload"]} for r in rows])
        if sql == events.SQL_MAX_ID:
            ids = [e.id for e in self._rows()]
            return _Result([{"max_id": max(ids) if ids else 0}])
        raise AssertionError(f"가짜 연결이 모르는 SQL: {sql!r}")


class FakeEventsKanban(FakeKanbanDb):
    """`connect` 가 SQL 을 받는 연결을 돌려주고, 사건 행을 마음대로 심을 수 있는 칸반."""

    def connect(self, db_path=None, *, board: Optional[str] = None) -> FakeEventsConn:
        slug = board or self.current_board
        if slug not in self.boards:
            raise KeyError(f"unknown board: {slug}")
        return FakeEventsConn(self, slug)

    def emit(self, board: str, task_id: str, kind: str, payload=None, *, ts=None, run_id=None) -> FakeEvent:
        """사건 행 하나. `payload=None` 은 진짜처럼 NULL 로 저장된다."""
        ev = FakeEvent(
            id=self._next(), task_id=task_id, kind=kind, payload=payload,
            created_at=int(time.time()) if ts is None else int(ts), run_id=run_id,
        )
        self.boards[board].events.append(ev)
        return ev

    def remove_task(self, board: str, task_id: str) -> None:
        """Hermes `delete_task` 뒤의 상태 — 카드·링크·사건이 전부 사라진다."""
        state = self.boards[board]
        state.tasks.pop(task_id, None)
        state.links = {(p, c) for p, c in state.links if task_id not in (p, c)}
        state.events = [e for e in state.events if e.task_id != task_id]

    def set_status(self, board: str, task_id: str, status: str) -> None:
        self.boards[board].tasks[task_id].status = status


def install_fake_events(fake_api, root) -> FakeEventsKanban:
    """conftest 가 심은 `FakeKanbanDb` 를 SQL 가능한 하위 클래스로 갈아 끼운다."""
    db = FakeEventsKanban(root)
    for name in KANBAN_DB_SYMBOLS:
        setattr(fake_api, name, getattr(db, name))
    fake_api.kanban = db
    return db


def append_deleted(api, slug: str, task_id: str, title: str, ts) -> int:
    """§5.3 형식 그대로 `{"n","task_id","title","ts"}` 한 줄을 append 한다. 붙인 줄의 `n` 을 돌려준다."""
    path = Path(api.board_dir(slug)) / events.DELETED_LOG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    if path.is_file():
        with path.open("r", encoding="utf-8") as fh:
            n = sum(1 for line in fh if line.strip())
    n += 1
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"n": n, "task_id": task_id, "title": title, "ts": ts}) + "\n")
    return n


def mount_events_route(app, adapter, api):
    """routes.py 테이블은 다른 태스크 몫이다 — 여기서는 사건 라우트만 같은 경로 모양으로 붙인다."""
    from deskrpg_plugin.auth import Scope, require_auth

    app.router.add_route("GET", "/deskrpg/events", require_auth(adapter, Scope.DEFAULT, events.events_handler(api)))


async def events_client(aiohttp_client, api, *, authorized=True):
    from tests.conftest import FakeAdapter

    app = web.Application()
    mount_events_route(app, FakeAdapter(authorized=authorized), api)
    return await aiohttp_client(app)

"""인메모리 가짜 `hermes_cli.kanban_db`.

Hermes 함수와 **같은 시그니처**를 가진 메서드를 가진 골격이다. 보드·카드·댓글·링크·이력·
상태 전이는 Hermes 0.21.x 의 규칙을 단순화해 구현했고(어느 상태에서 어느 상태로 갈 수
있는지, 어떤 사건을 남기는지), 첨부 저장·워커 로그·디스패치처럼 뒤 태스크 몫인 것은
`NotImplementedError` 를 던진다 — 뒤 태스크가 필요한 만큼 채운다.

`conn` 인자는 Hermes 시그니처를 맞추려 받는다. `board_conn()` 이 `api.connect(board=)` 로
돌려주는 값이 `FakeConn` 이라 어느 보드인지는 거기서 읽는다.

**raw SQL**: 대시보드에서 이식한 코드(보드 롤업, 제목/본문/우선순위 UPDATE, 직접 상태 전이)는
`conn.execute(sql, params)` 를 직접 부른다. `FakeConn.execute` 는 그 플러그인이 내는 문장만
알아듣는 아주 작은 SQL 흉내다 — 모르는 문장은 `NotImplementedError` 로 시끄럽게 실패한다.
행은 dict 로 돌려준다(`sqlite3.Row` 처럼 `row["col"]`·`row.keys()` 가 된다).

설치: `install_fake_kanban(fake_api, tmp_path)` — `fake_api` 의 kanban_db 심볼을 이 인스턴스의
메서드로 바꿔치기하고 인스턴스를 돌려준다.

테스트 전용 도우미(Hermes 심볼이 아니라 `api` 에 설치되지 않는다): `start_run`, `add_attachment`,
`calls`(전이 함수 호출 기록), `notified`.
"""

import contextlib
import json
import re
import time
import types
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

from deskrpg_plugin import kanban_views

DEFAULT_BOARD = "default"
VALID_STATUSES = frozenset(
    {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}
)
_SATISFIED = ("done", "archived")


class AttachmentTooLarge(ValueError):
    """Hermes 의 동명 예외를 흉내 낸다."""


class _FakeCursor:
    """`conn.execute()` 가 돌려주는 값. `fetchall`·`fetchone`·순회·`rowcount` 만 있다."""

    def __init__(self, rows, rowcount=-1):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


@dataclass
class FakeConn:
    """`connect(board=)` 가 돌려주는 값. 보드 슬러그와 DB 참조를 든 표식이다."""

    db: "FakeKanbanDb"
    board: str
    closed: bool = False

    def close(self):
        self.closed = True

    def execute(self, sql, params=()):
        return self.db._execute(self, " ".join(sql.split()), tuple(params))


@dataclass
class FakeTask:
    """Hermes `Task` 데이터클래스의 필드 이름을 그대로 쓴다(핸들러가 속성으로 읽는다)."""

    id: str
    title: str
    body: Optional[str]
    assignee: Optional[str]
    status: str
    priority: int
    created_by: Optional[str]
    created_at: int
    started_at: Optional[int] = None
    completed_at: Optional[int] = None
    workspace_kind: str = "scratch"
    workspace_path: Optional[str] = None
    claim_lock: Optional[str] = None
    claim_expires: Optional[int] = None
    tenant: Optional[str] = None
    branch_name: Optional[str] = None
    project_id: Optional[str] = None
    result: Optional[str] = None
    idempotency_key: Optional[str] = None
    consecutive_failures: int = 0
    worker_pid: Optional[int] = None
    last_failure_error: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    current_run_id: Optional[int] = None
    skills: Optional[list] = None
    model_override: Optional[str] = None
    provider_override: Optional[str] = None
    reasoning_effort: Optional[str] = None
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None


@dataclass
class FakeComment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int


@dataclass
class FakeEvent:
    id: int
    task_id: str
    kind: str
    payload: Optional[dict]
    created_at: int
    run_id: Optional[int] = None


@dataclass
class FakeRun:
    id: int
    task_id: str
    profile: Optional[str]
    status: str
    started_at: int
    step_key: Optional[str] = None
    claim_lock: Optional[str] = None
    claim_expires: Optional[int] = None
    worker_pid: Optional[int] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    ended_at: Optional[int] = None
    outcome: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[dict] = None
    error: Optional[str] = None


@dataclass
class FakeAttachment:
    id: int
    task_id: str
    filename: str
    stored_path: str
    content_type: Optional[str]
    size: int
    uploaded_by: Optional[str]
    created_at: int


@dataclass
class _BoardState:
    meta: dict
    tasks: dict = field(default_factory=dict)  # id -> FakeTask
    links: set = field(default_factory=set)  # (parent_id, child_id)
    comments: list = field(default_factory=list)  # FakeComment
    events: list = field(default_factory=list)  # FakeEvent
    runs: dict = field(default_factory=dict)  # id -> FakeRun
    attachments: dict = field(default_factory=dict)  # id -> FakeAttachment


class FakeKanbanDb:
    """보드별 dict 를 든 인메모리 칸반. 메서드 시그니처는 Hermes 0.21.1 과 같다."""

    VALID_STATUSES = VALID_STATUSES
    KANBAN_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
    AttachmentTooLarge = AttachmentTooLarge

    def __init__(self, root):
        self.root = root  # kanban_home() 이 돌려줄 경로(tmp_path 아래)
        self.boards: dict[str, _BoardState] = {}
        self.current_board = DEFAULT_BOARD
        self._seq = 0
        self.notified = []  # notify_task_updated 호출 기록 (task_id, changed_fields, board)
        self.calls: dict[str, list[dict]] = {}  # 전이 함수 이름 -> 호출 kwargs 목록
        self.create_board(DEFAULT_BOARD, name="Default")

    # ---- 내부 ---------------------------------------------------------------
    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> int:
        return int(time.time())

    def _record(self, name: str, **kwargs) -> None:
        self.calls.setdefault(name, []).append(kwargs)

    def _state(self, conn_or_board) -> _BoardState:
        slug = conn_or_board.board if isinstance(conn_or_board, FakeConn) else (conn_or_board or self.current_board)
        if slug not in self.boards:
            raise KeyError(f"unknown board: {slug}")
        return self.boards[slug]

    def _append_event(self, conn, task_id: str, kind: str, payload: Optional[dict] = None, *, run_id=None) -> FakeEvent:
        ev = FakeEvent(id=self._next(), task_id=task_id, kind=kind, payload=payload, created_at=self._now(), run_id=run_id)
        self._state(conn).events.append(ev)
        return ev

    def _require_task(self, conn, task_id: str) -> FakeTask:
        task = self.get_task(conn, task_id)
        if task is None:
            raise KeyError(f"unknown task: {task_id}")
        return task

    def _gated_ready(self, conn, task_id: str) -> str:
        return "ready" if self._parents_satisfied(conn, task_id) else "todo"

    def _resume_status(self, conn, task_id: str) -> str:
        """마지막 claimed 사건이 review 에서 잡은 run 이면 review, 아니면 부모 게이트를 거친 ready/todo."""
        if self._retry_status_for_run(conn, task_id, self._last_run_id(conn, task_id)) == "review":
            return "review"
        return self._gated_ready(conn, task_id)

    def _last_run_id(self, conn, task_id: str):
        runs = [r for r in self._state(conn).runs.values() if r.task_id == task_id]
        return runs[-1].id if runs else None

    # ---- raw SQL 흉내 -------------------------------------------------------
    def _execute(self, conn: FakeConn, sql: str, params: tuple) -> _FakeCursor:
        state = self._state(conn)

        def rows_of(table: str):
            if table == "tasks":
                return [asdict(t) for t in state.tasks.values()]
            if table == "task_events":
                return [asdict(e) for e in sorted(state.events, key=lambda e: e.id)]
            if table == "task_runs":
                return [asdict(r) for r in sorted(state.runs.values(), key=lambda r: r.id)]
            raise NotImplementedError(sql)

        if sql == "SELECT parent_id, child_id FROM task_links":
            return _FakeCursor({"parent_id": p, "child_id": c} for p, c in sorted(state.links))
        if sql == "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id":
            return _FakeCursor({"parent_id": p, "child_id": c} for p, c in sorted(state.links))

        # 타임라인 묶음 조회(`GET /kanban/runs`). 겹침 판정·정렬·상한을 실제 SQL 그대로 흉내 낸다 —
        # 가짜가 더 너그러우면 창 경계 결함이 테스트를 그대로 통과한다.
        if sql == (
            "SELECT r.*, t.tenant AS tenant, t.title AS task_title FROM task_runs r "
            "LEFT JOIN tasks t ON t.id = r.task_id WHERE r.started_at <= ? "
            "AND (r.ended_at IS NULL OR r.ended_at >= ?) ORDER BY r.started_at DESC, r.id DESC LIMIT ?"
        ):
            to_ts, from_ts, limit = params
            picked = []
            for run in state.runs.values():
                if run.started_at > to_ts:
                    continue
                if run.ended_at is not None and run.ended_at < from_ts:
                    continue
                row = asdict(run)
                task = state.tasks.get(run.task_id)
                row["tenant"] = task.tenant if task else None
                row["task_title"] = task.title if task else None
                picked.append(row)
            picked.sort(key=lambda r: (r["started_at"], r["id"]), reverse=True)
            return _FakeCursor(picked[: int(limit)])
        # Status history for `GET /kanban/events`. Mirrors the real window/kind filter and ordering exactly —
        # a more lenient fake would let a window-boundary bug pass.
        if sql == kanban_views.SQL_STATUS_HISTORY:
            to_ts, *kinds = params
            picked = []
            for ev in state.events:
                if ev.created_at > to_ts or ev.kind not in kinds:
                    continue
                task = state.tasks.get(ev.task_id)
                picked.append({
                    "id": ev.id, "task_id": ev.task_id, "kind": ev.kind, "payload": ev.payload,
                    "created_at": ev.created_at, "tenant": task.tenant if task else None,
                })
            picked.sort(key=lambda r: (r["task_id"], r["id"]))
            return _FakeCursor(picked)
        if sql == "SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id":
            counts: dict[str, int] = {}
            for c in state.comments:
                counts[c.task_id] = counts.get(c.task_id, 0) + 1
            return _FakeCursor({"task_id": k, "n": v} for k, v in sorted(counts.items()))
        if sql == "SELECT l.parent_id AS pid, t.status AS cstatus FROM task_links l JOIN tasks t ON t.id = l.child_id":
            return _FakeCursor(
                {"pid": p, "cstatus": state.tasks[c].status} for p, c in sorted(state.links) if c in state.tasks
            )
        if sql == "SELECT COALESCE(MAX(id), 0) AS m FROM task_events":
            return _FakeCursor([{"m": max((e.id for e in state.events), default=0)}])
        if sql == "SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant":
            return _FakeCursor({"tenant": t} for t in sorted({t.tenant for t in state.tasks.values() if t.tenant}))
        if sql == "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL AND status != 'archived' ORDER BY assignee":
            names = {t.assignee for t in state.tasks.values() if t.assignee and t.status != "archived"}
            return _FakeCursor({"assignee": a} for a in sorted(names))
        if sql == "SELECT * FROM tasks WHERE status != 'archived'":
            return _FakeCursor(r for r in rows_of("tasks") if r["status"] != "archived")

        m = re.fullmatch(r"SELECT \* FROM (tasks|task_events|task_runs) WHERE (id|task_id) IN \((\?(?:,\?)*)\)(?: ORDER BY id)?", sql)
        if m:
            table, col = m.group(1), m.group(2)
            wanted = set(params)
            return _FakeCursor(r for r in rows_of(table) if r[col] in wanted)

        m = re.fullmatch(r"SELECT (.+) FROM tasks WHERE id = \?", sql)
        if m:
            task = state.tasks.get(params[0])
            if task is None:
                return _FakeCursor([])
            row = asdict(task)
            if m.group(1).strip() != "*":
                row = {c.strip(): row[c.strip()] for c in m.group(1).split(",")}
            return _FakeCursor([row])

        m = re.fullmatch(r"UPDATE tasks SET (.+) WHERE id = \?", sql)
        if m:
            task = state.tasks.get(params[-1])
            if task is None:
                return _FakeCursor([], rowcount=0)
            it = iter(params[:-1])
            for assign in m.group(1).split(","):
                col, expr = (s.strip() for s in assign.split("=", 1))
                if expr == "?":
                    value = next(it)
                elif expr.startswith("CASE WHEN ? = 'running' THEN"):
                    value = getattr(task, col) if next(it) == "running" else None
                elif expr == "NULL":
                    value = None
                elif expr.startswith("'") and expr.endswith("'"):
                    value = expr[1:-1]
                else:
                    raise NotImplementedError(sql)
                setattr(task, col, value)
            return _FakeCursor([], rowcount=1)

        m = re.fullmatch(r"INSERT INTO task_events \(([^)]+)\) VALUES \(([^)]+)\)", sql)
        if m:
            cols = [c.strip() for c in m.group(1).split(",")]
            it = iter(params)
            values = {}
            for col, expr in zip(cols, (v.strip() for v in m.group(2).split(","))):
                if expr == "?":
                    values[col] = next(it)
                elif expr == "NULL":
                    values[col] = None
                elif expr.startswith("'") and expr.endswith("'"):
                    values[col] = expr[1:-1]
                else:
                    raise NotImplementedError(sql)
            payload = values.get("payload")
            if isinstance(payload, str):
                payload = json.loads(payload)
            self._append_event(conn, values["task_id"], values["kind"], payload, run_id=values.get("run_id"))
            return _FakeCursor([], rowcount=1)

        raise NotImplementedError(f"FakeConn.execute 가 모르는 SQL: {sql}")

    # ---- 경로·연결 ----------------------------------------------------------
    def kanban_home(self):
        return self.root

    def board_dir(self, board: Optional[str] = None):
        return self.root / "boards" / (board or self.current_board)

    def kanban_db_path(self, board: Optional[str] = None):
        return self.board_dir(board) / "kanban.db"

    def attachments_root(self, board: Optional[str] = None):
        return self.board_dir(board) / "attachments"

    def init_db(self, db_path=None, *, board: Optional[str] = None):
        slug = board or self.current_board
        if slug not in self.boards:
            raise KeyError(f"unknown board: {slug}")
        return self.kanban_db_path(slug)

    def connect(self, db_path=None, *, board: Optional[str] = None) -> FakeConn:
        slug = board or self.current_board
        if slug not in self.boards:
            raise KeyError(f"unknown board: {slug}")
        return FakeConn(db=self, board=slug)

    @contextlib.contextmanager
    def connect_closing(self, db_path=None, *, board: Optional[str] = None):
        conn = self.connect(db_path, board=board)
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def write_txn(self, conn):
        yield conn

    # ---- 보드 ---------------------------------------------------------------
    def board_exists(self, board: Optional[str] = None) -> bool:
        return (board or self.current_board) in self.boards

    def create_board(self, slug: str, *, name=None, description=None, icon=None, color=None, default_workdir=None, project_id=None) -> dict:
        """Hermes 처럼 `mkdir -p` 의미다 — 이미 있으면 메타를 갱신하고 돌려준다."""
        if slug not in self.boards:
            self.boards[slug] = _BoardState(
                meta={"slug": slug, "name": slug, "description": "", "icon": None, "color": None,
                      "archived": False, "default_workdir": None, "project_id": None, "created_at": self._now()}
            )
        return self.write_board_metadata(
            slug, name=name, description=description, icon=icon, color=color,
            default_workdir=default_workdir, project_id=project_id,
        )

    def write_board_metadata(self, board, *, name=None, description=None, icon=None, color=None, archived=None, default_workdir=None, project_id=None) -> dict:
        """Hermes 규칙: `None` 은 그대로, `""` 는 비움(name 은 기본 표시 이름으로)."""
        meta = self._state(board).meta
        if name is not None:
            meta["name"] = str(name).strip() or meta["slug"]
        for key, value in (("description", description), ("icon", icon), ("color", color)):
            if value is not None:
                meta[key] = str(value)
        if archived is not None:
            meta["archived"] = bool(archived)
        for key, value in (("default_workdir", default_workdir), ("project_id", project_id)):
            if value is not None:
                meta[key] = str(value) if value else None
        return {**meta, "db_path": str(self.kanban_db_path(meta["slug"]))}

    def list_boards(self, *, include_archived: bool = True) -> list:
        out = []
        for slug, state in self.boards.items():
            if not include_archived and state.meta.get("archived"):
                continue
            out.append({**state.meta, "db_path": str(self.kanban_db_path(slug))})
        return out

    def get_current_board(self) -> str:
        return self.current_board

    @contextlib.contextmanager
    def scoped_current_board(self, slug: str):
        prev = self.current_board
        self.current_board = slug
        try:
            yield
        finally:
            self.current_board = prev

    # ---- 카드 ---------------------------------------------------------------
    def create_task(self, conn, *, title: str, body=None, assignee=None, created_by=None, workspace_kind=None,
                    workspace_path=None, branch_name=None, tenant=None, priority: int = 0, parents: Iterable[str] = (),
                    triage: bool = False, idempotency_key=None, max_runtime_seconds=None, skills=None,
                    max_retries=None, model_override=None, provider_override=None, reasoning_effort=None,
                    goal_mode: bool = False, goal_max_turns=None, initial_status: str = "running",
                    session_id=None, board=None, project_id=None, **extra) -> str:
        """Hermes 처럼 **id 문자열**을 돌려준다. 상태: 부모가 안 끝났으면 todo, 아니면 ready, triage 면 triage."""
        state = self._state(conn)
        if not title or not title.strip():
            raise ValueError("title is required")
        if provider_override and not model_override:
            raise ValueError("provider_override requires model_override")
        parents = list(parents or ())
        missing = [p for p in parents if p not in state.tasks]
        if missing:
            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
        if idempotency_key:
            for existing in state.tasks.values():
                if existing.idempotency_key == idempotency_key and existing.status != "archived":
                    return existing.id
        if initial_status not in ("running", "blocked"):
            raise ValueError(f"invalid initial_status: {initial_status}")
        if triage:
            status = "triage"
        elif initial_status == "blocked":
            # Hermes 처럼 사람이 풀어 줄 때까지 세워 둔다(`kanban_db.py` 의 sticky block).
            status = "blocked"
        elif any(state.tasks[p].status != "done" for p in parents):
            status = "todo"
        else:
            status = "ready"
        task = FakeTask(
            id=f"t{self._next():04d}", title=title.strip(), body=body, assignee=assignee or None,
            status=status, priority=int(priority), created_by=created_by,
            created_at=self._now(), workspace_kind=workspace_kind or "scratch", workspace_path=workspace_path,
            branch_name=branch_name, tenant=tenant, idempotency_key=idempotency_key,
            max_runtime_seconds=max_runtime_seconds, project_id=project_id or None,
            skills=list(skills) if skills is not None else None, model_override=model_override or None,
            provider_override=provider_override or None, reasoning_effort=reasoning_effort or None,
            goal_mode=bool(goal_mode), goal_max_turns=goal_max_turns,
        )
        state.tasks[task.id] = task
        for parent in parents:
            state.links.add((parent, task.id))
        self._append_event(conn, task.id, "created", {"title": task.title, "status": task.status, "assignee": task.assignee})
        return task.id

    def get_task(self, conn, task_id: str) -> Optional[FakeTask]:
        return self._state(conn).tasks.get(task_id)

    def list_tasks(self, conn, *, assignee=None, status=None, tenant=None, session_id=None, include_archived: bool = False,
                   limit=None, order_by=None, workflow_template_id=None, current_step_key=None) -> list:
        rows = list(self._state(conn).tasks.values())
        if not include_archived and status != "archived":
            rows = [t for t in rows if t.status != "archived"]
        if assignee is not None:
            rows = [t for t in rows if t.assignee == assignee]
        if status is not None:
            rows = [t for t in rows if t.status == status]
        if tenant is not None:
            rows = [t for t in rows if t.tenant == tenant]
        rows.sort(key=lambda t: (-t.priority, t.created_at, t.id))  # Hermes 기본 정렬
        return rows[:limit] if limit else rows

    def task_age(self, task: FakeTask) -> dict:
        now = self._now()
        return {"age_seconds": now - task.created_at, "running_seconds": (now - task.started_at) if task.started_at else None}

    def task_graph_contexts(self, conn, task_ids: Iterable[str]) -> dict:
        return {tid: {"parents": self.parent_ids(conn, tid), "children": self.child_ids(conn, tid)} for tid in task_ids}

    def known_assignees(self, conn) -> list:
        counts = {}
        for t in self._state(conn).tasks.values():
            if t.assignee:
                counts[t.assignee] = counts.get(t.assignee, 0) + 1
        return [{"name": name, "count": n} for name, n in sorted(counts.items())]

    def notify_task_updated(self, conn, task_id: str, changed_fields: Iterable[str], *, board=None) -> None:
        self.notified.append((task_id, tuple(changed_fields), board or conn.board))

    # ---- 상태 전이 (Hermes 규칙의 단순화) ------------------------------------
    def assign_task(self, conn, task_id: str, profile: Optional[str]) -> bool:
        self._record("assign_task", task_id=task_id, profile=profile)
        task = self.get_task(conn, task_id)
        if task is None:
            return False
        if task.claim_lock is not None and task.status == "running":
            raise RuntimeError(f"cannot reassign {task_id}: currently running (claimed)")
        task.assignee = profile or None
        self._append_event(conn, task_id, "assigned", {"assignee": task.assignee})
        self.notify_task_updated(conn, task_id, ("assignee",))
        return True

    def complete_task(self, conn, task_id: str, *, result=None, summary=None, metadata=None, created_cards=None,
                      expected_run_id=None, fire_lifecycle_hook: bool = True) -> bool:
        self._record("complete_task", task_id=task_id, result=result, summary=summary, metadata=metadata)
        task = self.get_task(conn, task_id)
        if task is None or task.status not in ("running", "ready", "blocked", "review"):
            return False
        run_id = self._end_run(conn, task_id, outcome="completed", summary=summary or result, metadata=metadata)
        task.status, task.result, task.completed_at = "done", result, self._now()
        task.claim_lock = task.claim_expires = task.worker_pid = None
        self._append_event(conn, task_id, "completed", {"status": "done"}, run_id=run_id)
        self.recompute_ready(conn)
        return True

    def block_task(self, conn, task_id: str, *, reason=None, kind=None, expected_run_id=None) -> bool:
        self._record("block_task", task_id=task_id, reason=reason, kind=kind)
        task = self.get_task(conn, task_id)
        if task is None or task.status not in ("running", "ready"):
            return False
        run_id = self._end_run(conn, task_id, outcome="blocked", error=reason)
        task.status = "blocked"
        task.claim_lock = task.claim_expires = task.worker_pid = None
        self._append_event(conn, task_id, "blocked", {"reason": reason, "kind": kind}, run_id=run_id)
        return True

    def schedule_task(self, conn, task_id: str, *, reason=None, expected_run_id=None) -> bool:
        self._record("schedule_task", task_id=task_id, reason=reason)
        task = self.get_task(conn, task_id)
        if task is None or task.status not in ("todo", "ready", "running", "blocked"):
            return False
        run_id = self._end_run(conn, task_id, outcome="scheduled")
        task.status = "scheduled"
        task.claim_lock = task.claim_expires = task.worker_pid = None
        self._append_event(conn, task_id, "scheduled", {"reason": reason}, run_id=run_id)
        return True

    def request_review(self, conn, task_id: str, *, summary=None, metadata=None, reviewer=None, expected_run_id=None,
                       force: bool = False, with_reason: bool = False):
        self._record("request_review", task_id=task_id, summary=summary, metadata=metadata, reviewer=reviewer,
                     expected_run_id=expected_run_id, force=force)

        def ret(ok, reason=None):
            return (ok, reason) if with_reason else ok

        task = self.get_task(conn, task_id)
        if task is None or task.status not in ("running", "ready"):
            return ret(False, "not in running/ready")
        if task.status == "running" and not force and expected_run_id != task.current_run_id:
            return ret(False, "live claim owned by another run")
        run_id = self._end_run(conn, task_id, outcome="review_requested", summary=summary, metadata=metadata)
        implementer = task.assignee
        task.status = "review"
        task.claim_lock = task.claim_expires = task.worker_pid = None
        if reviewer:
            task.assignee = reviewer
        self._append_event(conn, task_id, "review_requested", {"implementer": implementer, "reviewer": reviewer}, run_id=run_id)
        return ret(True)

    def request_changes(self, conn, task_id: str, *, reason: str, expected_run_id=None):
        raise NotImplementedError("request_changes 는 뒤 태스크가 채운다")

    def unblock_task(self, conn, task_id: str) -> bool:
        self._record("unblock_task", task_id=task_id)
        task = self.get_task(conn, task_id)
        if task is None or task.status not in ("blocked", "scheduled"):
            return False
        task.status = self._resume_status(conn, task_id)
        self._append_event(conn, task_id, "unblocked", {"status": task.status})
        return True

    def reopen_review_task(self, conn, task_id: str) -> bool:
        self._record("reopen_review_task", task_id=task_id)
        task = self.get_task(conn, task_id)
        if task is None or task.status != "review":
            return False
        task.status = self._gated_ready(conn, task_id)
        self._append_event(conn, task_id, "review_reopened", {"status": task.status})
        return True

    def archive_task(self, conn, task_id: str) -> bool:
        self._record("archive_task", task_id=task_id)
        task = self.get_task(conn, task_id)
        if task is None or task.status == "archived":
            return False
        run_id = self._end_run(conn, task_id, outcome="reclaimed", status="reclaimed", summary="task archived with run still active")
        task.status = "archived"
        task.claim_lock = task.claim_expires = task.worker_pid = None
        self._append_event(conn, task_id, "archived", None, run_id=run_id)
        self.recompute_ready(conn)
        return True

    def delete_task(self, conn, task_id: str) -> bool:
        self._record("delete_task", task_id=task_id)
        state = self._state(conn)
        if state.tasks.pop(task_id, None) is None:
            return False
        state.links = {(p, c) for p, c in state.links if task_id not in (p, c)}
        state.comments = [c for c in state.comments if c.task_id != task_id]
        state.events = [e for e in state.events if e.task_id != task_id]
        state.runs = {rid: r for rid, r in state.runs.items() if r.task_id != task_id}
        state.attachments = {aid: a for aid, a in state.attachments.items() if a.task_id != task_id}
        self.recompute_ready(conn)
        return True

    def _set_override(self, conn, task_id, event_kind, payload, changed, msg) -> bool:
        task = self.get_task(conn, task_id)
        if task is None:
            return False
        if task.status == "archived":
            raise RuntimeError(f"{msg} on archived task {task_id}")
        for key, value in payload.items():
            setattr(task, key, value)
        self._append_event(conn, task_id, event_kind, payload)
        self.notify_task_updated(conn, task_id, changed)
        return True

    def set_model_override(self, conn, task_id: str, model: Optional[str], provider: Optional[str] = None) -> bool:
        self._record("set_model_override", task_id=task_id, model=model, provider=provider)
        model = (model or "").strip() or None
        provider = (provider or "").strip() or None
        if provider and not model:
            raise ValueError("provider_override requires model_override")
        if model is None:
            provider = None  # 빈 모델은 둘 다 비운다
        return self._set_override(
            conn, task_id, "model_override_set", {"model_override": model, "provider_override": provider},
            ("model_override", "provider_override"), "cannot set model override",
        )

    def set_reasoning_effort(self, conn, task_id: str, effort: Optional[str]) -> bool:
        self._record("set_reasoning_effort", task_id=task_id, effort=effort)
        effort = (effort or "").strip().lower() or None
        return self._set_override(
            conn, task_id, "reasoning_effort_set", {"reasoning_effort": effort}, ("reasoning_effort",),
            "cannot set reasoning effort",
        )

    def reclaim_task(self, conn, task_id: str, *, reason=None, signal_fn=None):
        raise NotImplementedError("reclaim_task 는 뒤 태스크가 채운다")

    def reassign_task(self, conn, task_id: str, profile: Optional[str], *, reclaim_first: bool = False, reason=None):
        raise NotImplementedError("reassign_task 는 뒤 태스크가 채운다")

    def invalidate_descendants_for_parent_reopen(self, conn, task_id: str, *, author: str) -> dict:
        """ready/review/running/done 후손을 전부 todo 로 되돌린다. running 은 run 을 닫고 종료 목록에 싣는다."""
        self._record("invalidate_descendants_for_parent_reopen", task_id=task_id, author=author)
        seen, stack, terminations, invalidated = set(), list(self.child_ids(conn, task_id)), [], []
        while stack:
            tid = stack.pop()
            if tid in seen:
                continue
            seen.add(tid)
            stack.extend(self.child_ids(conn, tid))
            task = self.get_task(conn, tid)
            if task is None or task.status not in ("ready", "review", "running", "done"):
                continue
            run_id = None
            if task.status == "running":
                terminations.append((task.worker_pid, task.claim_lock))
                run_id = self._end_run(conn, tid, outcome="reclaimed", status="reclaimed", summary=f"ancestor {task_id} reopened")
            task.status = "todo"
            task.claim_lock = task.claim_expires = task.worker_pid = None
            task.consecutive_failures = 0
            self._append_event(conn, tid, "descendant_invalidated", {"ancestor": task_id, "author": author}, run_id=run_id)
            self._append_event(conn, tid, "status", {"status": "todo"}, run_id=run_id)
            invalidated.append(tid)
        return {"terminations": terminations, "invalidated": invalidated}

    def recompute_ready(self, conn, failure_limit=None) -> int:
        """부모가 전부 done/archived 인 todo 를 ready 로(Hermes 처럼 부모 없는 todo 도 올라간다)."""
        promoted = 0
        for task in list(self._state(conn).tasks.values()):
            if task.status != "todo" or not self._parents_satisfied(conn, task.id):
                continue
            task.status = "ready"
            self._append_event(conn, task.id, "promoted", None)
            promoted += 1
        return promoted

    def _retry_status_for_run(self, conn, task_id: str, run_id=None) -> str:
        if run_id is None:
            run_id = self._require_task(conn, task_id).current_run_id
        if run_id is None:
            return "ready"
        for ev in reversed(self._state(conn).events):
            if ev.task_id == task_id and ev.kind == "claimed" and ev.run_id == run_id:
                return "review" if (ev.payload or {}).get("source_status") == "review" else "ready"
        return "ready"

    def _parents_satisfied(self, conn, task_id: str) -> bool:
        parents = self.parent_ids(conn, task_id)
        return all(self._require_task(conn, p).status in _SATISFIED for p in parents)

    def _end_run(self, conn, task_id: str, *, outcome: str, summary=None, error=None, metadata=None, status=None):
        task = self._require_task(conn, task_id)
        run_id = task.current_run_id
        if run_id is None:
            return None
        run = self._state(conn).runs[run_id]
        run.status, run.outcome, run.summary, run.error, run.metadata = (status or outcome), outcome, summary, error, metadata
        run.ended_at = self._now()
        task.current_run_id = None
        return run_id

    # ---- 댓글·이력 ----------------------------------------------------------
    def add_comment(self, conn, task_id: str, author: str, body: str) -> int:
        if not body or not body.strip():
            raise ValueError("comment body is required")
        if not author or not author.strip():
            raise ValueError("comment author is required")
        self._require_task(conn, task_id)
        comment = FakeComment(id=self._next(), task_id=task_id, author=author, body=body, created_at=self._now())
        self._state(conn).comments.append(comment)
        self._append_event(conn, task_id, "commented", {"author": author, "comment_id": comment.id})
        return comment.id

    def list_comments(self, conn, task_id: str) -> list:
        return [c for c in self._state(conn).comments if c.task_id == task_id]

    def list_events(self, conn, task_id: str) -> list:
        return [e for e in self._state(conn).events if e.task_id == task_id]

    def latest_summary(self, conn, task_id: str) -> Optional[str]:
        runs = [r for r in self._state(conn).runs.values() if r.task_id == task_id and r.summary]
        return runs[-1].summary if runs else None

    def latest_summaries(self, conn, task_ids: Iterable[str]) -> dict:
        out = {}
        for tid in task_ids:
            s = self.latest_summary(conn, tid)
            if s:
                out[tid] = s
        return out

    # ---- 링크 ---------------------------------------------------------------
    def _would_cycle(self, conn, parent_id: str, child_id: str) -> bool:
        seen, stack = set(), [child_id]
        while stack:
            node = stack.pop()
            if node == parent_id:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(self.child_ids(conn, node))
        return False

    def link_tasks(self, conn, parent_id: str, child_id: str) -> None:
        if parent_id == child_id:
            raise ValueError("a task cannot depend on itself")
        state = self._state(conn)
        missing = [t for t in (parent_id, child_id) if t not in state.tasks]
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        if self._would_cycle(conn, parent_id, child_id):
            raise ValueError(f"linking {parent_id} -> {child_id} would create a cycle")
        state.links.add((parent_id, child_id))
        child = state.tasks[child_id]
        if state.tasks[parent_id].status != "done" and child.status == "ready":
            child.status = "todo"
        self._append_event(conn, child_id, "linked", {"parent": parent_id, "child": child_id})

    def unlink_tasks(self, conn, parent_id: str, child_id: str) -> bool:
        links = self._state(conn).links
        if (parent_id, child_id) not in links:
            return False
        links.discard((parent_id, child_id))
        self._append_event(conn, child_id, "unlinked", {"parent": parent_id, "child": child_id})
        self.recompute_ready(conn)
        return True

    def parent_ids(self, conn, task_id: str) -> list:
        return sorted(p for p, c in self._state(conn).links if c == task_id)

    def child_ids(self, conn, task_id: str) -> list:
        return sorted(c for p, c in self._state(conn).links if p == task_id)

    # ---- 실행·로그 ----------------------------------------------------------
    def list_runs(self, conn, task_id: str, *, include_active: bool = True, state_type=None, state_name=None) -> list:
        runs = [r for r in self._state(conn).runs.values() if r.task_id == task_id]
        if not include_active:
            runs = [r for r in runs if r.ended_at is not None]
        return sorted(runs, key=lambda r: r.id)

    def get_run(self, conn, run_id: int) -> Optional[FakeRun]:
        return self._state(conn).runs.get(int(run_id))

    def read_worker_log(self, task_id: str, *, tail_bytes=None, board=None):
        raise NotImplementedError("read_worker_log 는 뒤 태스크가 채운다")

    # ---- 첨부 ---------------------------------------------------------------
    def store_attachment_bytes(self, conn, task_id: str, filename: str, data: bytes, *, content_type=None,
                               uploaded_by=None, board=None, max_bytes=None):
        raise NotImplementedError("store_attachment_bytes 는 뒤 태스크가 채운다")

    def list_attachments(self, conn, task_id: str) -> list:
        return [a for a in self._state(conn).attachments.values() if a.task_id == task_id]

    def get_attachment(self, conn, attachment_id: int) -> Optional[FakeAttachment]:
        return self._state(conn).attachments.get(int(attachment_id))

    def delete_attachment(self, conn, attachment_id: int):
        raise NotImplementedError("delete_attachment 는 뒤 태스크가 채운다")

    # ---- 테스트 전용 도우미 (Hermes 심볼 아님) --------------------------------
    def start_run(self, conn, task_id: str, *, profile=None, worker_pid: int = 1000, claim_lock=None,
                  summary=None, source_status: str = "ready") -> int:
        """디스패처가 카드를 잡은 상태를 만든다: running + 활성 run + `claimed` 사건."""
        task = self._require_task(conn, task_id)
        now = self._now()
        run = FakeRun(id=self._next(), task_id=task_id, profile=profile or task.assignee, status="running",
                      started_at=now, claim_lock=claim_lock or f"lock-{task_id}", worker_pid=worker_pid, summary=summary)
        self._state(conn).runs[run.id] = run
        task.status, task.started_at, task.current_run_id = "running", now, run.id
        task.claim_lock, task.worker_pid, task.last_heartbeat_at = run.claim_lock, worker_pid, now
        if profile:
            task.assignee = profile
        self._append_event(conn, task_id, "claimed", {"profile": run.profile, "source_status": source_status}, run_id=run.id)
        return run.id

    def add_attachment(self, conn, task_id: str, *, filename: str, size: int = 0, content_type=None, uploaded_by=None) -> FakeAttachment:
        self._require_task(conn, task_id)
        att = FakeAttachment(id=self._next(), task_id=task_id, filename=filename,
                             stored_path=str(self.attachments_root(conn.board) / task_id / filename),
                             content_type=content_type, size=size, uploaded_by=uploaded_by, created_at=self._now())
        self._state(conn).attachments[att.id] = att
        return att


# `_hermes_api.SPEC` 의 kanban_db(+kanban_db_connect) 이름 가운데 FakeKanbanDb 가 제공하는 것.
KANBAN_DB_SYMBOLS = (
    "connect", "connect_closing", "init_db", "board_exists", "list_boards", "create_board",
    "write_board_metadata", "get_current_board", "scoped_current_board", "list_tasks", "get_task",
    "create_task", "assign_task", "complete_task", "block_task", "schedule_task", "request_review",
    "request_changes", "unblock_task", "reopen_review_task", "archive_task", "delete_task",
    "set_model_override", "set_reasoning_effort", "add_comment", "list_comments", "list_events",
    "link_tasks", "unlink_tasks", "parent_ids", "child_ids", "list_runs", "get_run", "reclaim_task",
    "reassign_task", "list_attachments", "get_attachment", "delete_attachment", "store_attachment_bytes",
    "attachments_root", "read_worker_log", "latest_summaries", "latest_summary", "task_age",
    "task_graph_contexts", "notify_task_updated", "known_assignees", "write_txn", "VALID_STATUSES",
    "KANBAN_ATTACHMENT_MAX_BYTES", "kanban_home", "kanban_db_path", "board_dir", "AttachmentTooLarge",
    "_retry_status_for_run", "_parents_satisfied", "_end_run", "invalidate_descendants_for_parent_reopen",
    "recompute_ready",
)


def install_fake_kanban(fake_api: types.SimpleNamespace, root) -> FakeKanbanDb:
    """`fake_api` 의 kanban_db 심볼을 FakeKanbanDb 인스턴스로 바꿔치기한다."""
    db = FakeKanbanDb(root)
    for name in KANBAN_DB_SYMBOLS:
        setattr(fake_api, name, getattr(db, name))
    return db

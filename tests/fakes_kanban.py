"""인메모리 가짜 `hermes_cli.kanban_db`.

Hermes 함수와 **같은 시그니처**를 가진 메서드를 가진 골격이다. 사소한 것(보드·카드
생성/조회, 댓글, 링크, 이력)은 구현했고, 상태 전이·실행·첨부처럼 동작이 뒤 태스크 몫인
것은 `NotImplementedError` 를 던진다 — 뒤 태스크가 필요한 만큼 채운다.

`conn` 인자는 Hermes 시그니처를 맞추려 받기만 하고 무시한다. `board_conn()` 이
`api.connect(board=)` 로 돌려주는 값이 `FakeConn` 이라 어느 보드인지는 거기서 읽는다.

설치: `install_fake_kanban(fake_api, tmp_path)` — `fake_api` 의 kanban_db 심볼을 이 인스턴스의
메서드로 바꿔치기하고 인스턴스를 돌려준다.
"""

import contextlib
import time
import types
from dataclasses import dataclass, field
from typing import Iterable, Optional

DEFAULT_BOARD = "default"
VALID_STATUSES = frozenset(
    {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}
)


class AttachmentTooLarge(ValueError):
    """Hermes 의 동명 예외를 흉내 낸다."""


@dataclass
class FakeConn:
    """`connect(board=)` 가 돌려주는 값. sqlite 연결이 아니라 보드 슬러그를 든 표식이다."""

    board: str
    closed: bool = False

    def close(self):
        self.closed = True


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
        self.create_board(DEFAULT_BOARD, name="Default")

    # ---- 내부 ---------------------------------------------------------------
    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> int:
        return int(time.time())

    def _state(self, conn_or_board) -> _BoardState:
        slug = conn_or_board.board if isinstance(conn_or_board, FakeConn) else (conn_or_board or self.current_board)
        if slug not in self.boards:
            raise KeyError(f"unknown board: {slug}")
        return self.boards[slug]

    def _append_event(self, conn, task_id: str, kind: str, payload: Optional[dict] = None, *, run_id=None) -> FakeEvent:
        ev = FakeEvent(id=self._next(), task_id=task_id, kind=kind, payload=payload or {}, created_at=self._now(), run_id=run_id)
        self._state(conn).events.append(ev)
        return ev

    def _require_task(self, conn, task_id: str) -> FakeTask:
        task = self.get_task(conn, task_id)
        if task is None:
            raise KeyError(f"unknown task: {task_id}")
        return task

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
        return FakeConn(board=slug)

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
        if slug in self.boards:
            raise ValueError(f"board already exists: {slug}")
        meta = {
            "slug": slug, "name": name or slug, "description": description or "", "icon": icon,
            "color": color, "archived": False, "default_workdir": default_workdir, "project_id": project_id,
        }
        self.boards[slug] = _BoardState(meta=meta)
        return dict(meta)

    def write_board_metadata(self, board, *, name=None, description=None, icon=None, color=None, archived=None, default_workdir=None, project_id=None) -> dict:
        meta = self._state(board).meta
        for key, value in (
            ("name", name), ("description", description), ("icon", icon), ("color", color),
            ("archived", archived), ("default_workdir", default_workdir), ("project_id", project_id),
        ):
            if value is not None:
                meta[key] = value
        return dict(meta)

    def list_boards(self, *, include_archived: bool = True) -> list:
        out = []
        for slug, state in self.boards.items():
            if not include_archived and state.meta.get("archived"):
                continue
            out.append({**state.meta, "total": len(state.tasks)})
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
                    triage: bool = False, idempotency_key=None, max_runtime_seconds=None, skills=None, **extra) -> FakeTask:
        state = self._state(conn)
        if idempotency_key:
            for existing in state.tasks.values():
                if existing.idempotency_key == idempotency_key:
                    return existing
        task = FakeTask(
            id=f"t{self._next():04d}", title=title, body=body, assignee=assignee,
            status="triage" if triage else "todo", priority=priority, created_by=created_by,
            created_at=self._now(), workspace_kind=workspace_kind or "scratch", workspace_path=workspace_path,
            branch_name=branch_name, tenant=tenant, idempotency_key=idempotency_key,
            max_runtime_seconds=max_runtime_seconds, project_id=extra.get("project_id"),
        )
        state.tasks[task.id] = task
        for parent in parents:
            self.link_tasks(conn, parent, task.id)
        self._append_event(conn, task.id, "created", {"title": title, "status": task.status, "assignee": assignee})
        return task

    def get_task(self, conn, task_id: str) -> Optional[FakeTask]:
        return self._state(conn).tasks.get(task_id)

    def list_tasks(self, conn, *, assignee=None, status=None, tenant=None, session_id=None, include_archived: bool = False,
                   limit=None, order_by=None, workflow_template_id=None, current_step_key=None) -> list:
        rows = list(self._state(conn).tasks.values())
        if not include_archived:
            rows = [t for t in rows if t.status != "archived"]
        if assignee is not None:
            rows = [t for t in rows if t.assignee == assignee]
        if status is not None:
            rows = [t for t in rows if t.status == status]
        if tenant is not None:
            rows = [t for t in rows if t.tenant == tenant]
        rows.sort(key=lambda t: (t.created_at, t.id))
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

    # 상태 전이 — 동작은 뒤 태스크 몫이다.
    def assign_task(self, conn, task_id: str, profile: Optional[str]) -> bool:
        raise NotImplementedError("assign_task 는 뒤 태스크가 채운다")

    def complete_task(self, conn, task_id: str, *, result=None, summary=None, metadata=None, created_cards=None,
                      expected_run_id=None, fire_lifecycle_hook: bool = True):
        raise NotImplementedError("complete_task 는 뒤 태스크가 채운다")

    def block_task(self, conn, task_id: str, *, reason=None, kind=None, expected_run_id=None):
        raise NotImplementedError("block_task 는 뒤 태스크가 채운다")

    def schedule_task(self, conn, task_id: str, *, reason=None, expected_run_id=None):
        raise NotImplementedError("schedule_task 는 뒤 태스크가 채운다")

    def request_review(self, conn, task_id: str, *, summary=None, metadata=None, reviewer=None, expected_run_id=None,
                       force: bool = False, with_reason: bool = False):
        raise NotImplementedError("request_review 는 뒤 태스크가 채운다")

    def request_changes(self, conn, task_id: str, *, reason: str, expected_run_id=None):
        raise NotImplementedError("request_changes 는 뒤 태스크가 채운다")

    def unblock_task(self, conn, task_id: str) -> bool:
        raise NotImplementedError("unblock_task 는 뒤 태스크가 채운다")

    def reopen_review_task(self, conn, task_id: str) -> bool:
        raise NotImplementedError("reopen_review_task 는 뒤 태스크가 채운다")

    def archive_task(self, conn, task_id: str) -> bool:
        raise NotImplementedError("archive_task 는 뒤 태스크가 채운다")

    def delete_task(self, conn, task_id: str) -> bool:
        raise NotImplementedError("delete_task 는 뒤 태스크가 채운다")

    def set_model_override(self, conn, task_id: str, model: Optional[str], provider: Optional[str] = None) -> bool:
        raise NotImplementedError("set_model_override 는 뒤 태스크가 채운다")

    def set_reasoning_effort(self, conn, task_id: str, effort: Optional[str]) -> bool:
        raise NotImplementedError("set_reasoning_effort 는 뒤 태스크가 채운다")

    def reclaim_task(self, conn, task_id: str, *, reason=None, signal_fn=None):
        raise NotImplementedError("reclaim_task 는 뒤 태스크가 채운다")

    def reassign_task(self, conn, task_id: str, profile: Optional[str], *, reclaim_first: bool = False, reason=None):
        raise NotImplementedError("reassign_task 는 뒤 태스크가 채운다")

    def invalidate_descendants_for_parent_reopen(self, conn, task_id: str, *, author: str):
        raise NotImplementedError("invalidate_descendants_for_parent_reopen 는 뒤 태스크가 채운다")

    def recompute_ready(self, conn, failure_limit=None) -> int:
        raise NotImplementedError("recompute_ready 는 뒤 태스크가 채운다")

    def _retry_status_for_run(self, conn, task_id: str, run_id=None):
        raise NotImplementedError("_retry_status_for_run 는 뒤 태스크가 채운다")

    def _parents_satisfied(self, conn, task_id: str) -> bool:
        parents = self.parent_ids(conn, task_id)
        return all(self._require_task(conn, p).status == "done" for p in parents)

    def _end_run(self, conn, task_id: str, *, outcome: str, summary=None, error=None, metadata=None, status=None):
        raise NotImplementedError("_end_run 는 뒤 태스크가 채운다")

    # ---- 댓글·이력 ----------------------------------------------------------
    def add_comment(self, conn, task_id: str, author: str, body: str) -> int:
        if not body or not body.strip():
            raise ValueError("comment body is required")
        if not author or not author.strip():
            raise ValueError("comment author is required")
        self._require_task(conn, task_id)
        comment = FakeComment(id=self._next(), task_id=task_id, author=author, body=body, created_at=self._now())
        self._state(conn).comments.append(comment)
        self._append_event(conn, task_id, "comment", {"author": author, "comment_id": comment.id})
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
    def link_tasks(self, conn, parent_id: str, child_id: str) -> None:
        if parent_id == child_id:
            raise ValueError("a task cannot depend on itself")
        self._require_task(conn, parent_id)
        self._require_task(conn, child_id)
        self._state(conn).links.add((parent_id, child_id))
        self._append_event(conn, child_id, "linked", {"parent_id": parent_id})

    def unlink_tasks(self, conn, parent_id: str, child_id: str) -> bool:
        links = self._state(conn).links
        if (parent_id, child_id) not in links:
            return False
        links.discard((parent_id, child_id))
        self._append_event(conn, child_id, "unlinked", {"parent_id": parent_id})
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

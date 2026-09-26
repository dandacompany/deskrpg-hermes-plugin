"""`FakeKanbanDb` 위에 T3 가 필요로 하는 것만 얹은 가짜.

상태 전이·run 개폐·검토자 판정(`_retry_status_for_run`)·`start_run` 은 전부 base(T2) 것을 쓴다.
여기서는 base 가 `NotImplementedError` 로 비워 둔 것만 채운다 — `reclaim_task`·`reassign_task`·
`request_changes`, 첨부 저장·삭제, 워커 로그 읽기. 반환 규약은 Hermes 0.21.2 와 같다.

테스트 도우미(Hermes 에 없는 것): `write_worker_log(task_id, text, board=)`.
호출 기록은 base 의 `calls`(이름 → kwargs 목록)를 그대로 쓴다.

설치: `install_fake_kanban_actions(fake_api, root)` — `fake_api.kanban` 도 같이 바꿔 둔다.
"""

import types
from pathlib import Path
from typing import Optional

from tests.fakes_kanban import KANBAN_DB_SYMBOLS, AttachmentTooLarge, FakeAttachment, FakeKanbanDb


class FakeKanbanActionsDb(FakeKanbanDb):
    """되찾기·재배정·재작업 요청·첨부·로그가 동작하는 인메모리 칸반."""

    def __init__(self, root):
        super().__init__(root)

    # ---- 테스트 도우미 --------------------------------------------------------
    def write_worker_log(self, task_id: str, text: str, *, board=None):
        path = self._worker_log_path(task_id, board=board)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _worker_log_path(self, task_id: str, *, board=None):
        return self.board_dir(board) / "logs" / f"{task_id}.log"

    def _latest_event(self, conn, task_id: str, kind: str):
        for ev in reversed(self._state(conn).events):
            if ev.task_id == task_id and ev.kind == kind:
                return ev
        return None

    # ---- 되찾기·재배정·재작업 -------------------------------------------------
    def reassign_task(self, conn, task_id: str, profile: Optional[str], *, reclaim_first: bool = False, reason=None):
        """Hermes: `assign_task` 의 RuntimeError(실행 중)를 삼켜 False 로 돌려준다."""
        self._record("reassign_task", task_id=task_id, profile=profile, reclaim_first=reclaim_first, reason=reason)
        if reclaim_first:
            self.reclaim_task(conn, task_id, reason=reason or "reassign")
        try:
            return self.assign_task(conn, task_id, profile)
        except RuntimeError:
            return False

    def request_changes(self, conn, task_id: str, *, reason: str, expected_run_id=None):
        """Hermes: `(ok, implementer | reason)`. 검토자 run(review 에서 claim) 이 활성일 때만 성공한다."""
        self._record("request_changes", task_id=task_id, reason=reason)
        if not (reason or "").strip():
            return False, "reason is required"
        task = self.get_task(conn, task_id)
        if task is None:
            return False, "task not found"
        if task.status != "running" or task.current_run_id is None:
            return False, "task is not in an active review run"
        if self._retry_status_for_run(conn, task_id, task.current_run_id) != "review":
            return False, "active run was not claimed from review"
        requested = self._latest_event(conn, task_id, "review_requested")
        implementer = (requested.payload or {}).get("implementer") if requested else None
        if not implementer:
            return False, "review handoff has no valid implementer provenance"
        new_status = self._gated_ready(conn, task_id)
        run_id = self._end_run(conn, task_id, outcome="changes_requested", status=new_status, summary=reason)
        task.status = new_status
        task.assignee = implementer
        task.claim_lock = task.claim_expires = task.worker_pid = None
        self._append_event(conn, task_id, "changes_requested", {"reason": reason, "implementer": implementer}, run_id=run_id)
        return True, implementer

    # ---- 첨부 ---------------------------------------------------------------
    def store_attachment_bytes(self, conn, task_id: str, filename: str, data: bytes, *, content_type=None,
                               uploaded_by=None, board=None, max_bytes=None):
        self._record("store_attachment_bytes", task_id=task_id, filename=filename, content_type=content_type,
                     uploaded_by=uploaded_by, board=board, max_bytes=max_bytes)
        if max_bytes is None:
            max_bytes = self.KANBAN_ATTACHMENT_MAX_BYTES
        if len(data) > max_bytes:
            raise AttachmentTooLarge(f"attachment exceeds {max_bytes} bytes limit")
        safe = (filename or "").replace("\\", "/").split("/")[-1].strip().lstrip(".").strip()
        if not safe:
            raise ValueError("invalid attachment filename")
        self._require_task(conn, task_id)
        dest_dir = self.attachments_root(board or conn.board) / task_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe
        n = 1
        while dest.exists():
            stem, dot, ext = safe.partition(".")
            dest = dest_dir / f"{stem} ({n}){dot}{ext}"
            n += 1
        dest.write_bytes(data)
        att = FakeAttachment(
            id=self._next(), task_id=task_id, filename=dest.name, stored_path=str(dest.resolve()),
            content_type=content_type, size=len(data), uploaded_by=uploaded_by, created_at=self._now(),
        )
        self._state(conn).attachments[att.id] = att
        self._append_event(conn, task_id, "attached", {"filename": att.filename, "size": att.size, "by": uploaded_by})
        return att.id

    def delete_attachment(self, conn, attachment_id: int):
        att = self._state(conn).attachments.pop(int(attachment_id), None)
        if att is None:
            return None
        self._append_event(conn, att.task_id, "attachment_removed", {"filename": att.filename})
        # Hermes 처럼 blob 도 지운다 — 핸들러가 따로 unlink 하지 않는다는 규약의 근거.
        try:
            path = Path(att.stored_path)
            if path.is_file():
                path.unlink()
        except OSError:
            pass
        return att

    # ---- 로그 ---------------------------------------------------------------
    def read_worker_log(self, task_id: str, *, tail_bytes=None, board=None):
        """Hermes 와 같은 규약: 파일이 없으면 None, tail 이면 마지막 tail_bytes(첫 잘린 줄은 건너뜀)."""
        path = self._worker_log_path(task_id, board=board)
        if not path.exists():
            return None
        raw = path.read_bytes()
        if tail_bytes is None or len(raw) <= tail_bytes:
            return raw.decode("utf-8", errors="replace")
        window = raw[len(raw) - tail_bytes:]
        nl = window.find(b"\n")
        if 0 <= nl < len(window) - 1:
            window = window[nl + 1:]
        return window.decode("utf-8", errors="replace")


def install_fake_kanban_actions(fake_api: types.SimpleNamespace, root) -> FakeKanbanActionsDb:
    """`fake_api` 의 kanban_db 심볼을 동작하는 가짜로 바꿔치기하고 `fake_api.kanban` 도 갱신한다."""
    db = FakeKanbanActionsDb(root)
    for name in KANBAN_DB_SYMBOLS:
        setattr(fake_api, name, getattr(db, name))
    fake_api.kanban = db
    return db

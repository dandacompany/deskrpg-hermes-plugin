"""`FakeKanbanDb` 골격 위에 **동작·실행·첨부·로그**를 채운 가짜.

`tests/fakes_kanban.py` 는 T2 소유라 손대지 않는다. 여기서 하위 클래스로 상태 전이
(`reclaim/reassign/complete/request_changes/unblock/reopen_review/archive`), 실행(run) 개폐,
첨부 저장·삭제, 워커 로그 읽기를 Hermes 0.21.2 `kanban_db` 와 같은 **반환 규약**으로 흉내 낸다.

Hermes 와 같은 점만 재현한다 — 상태 문자열, 성공/실패의 bool·tuple 반환, 검토자 run 판정
(`claimed` 사건 payload 의 `source_status == "review"`), 실행 중 재배정의 `RuntimeError`.
텍스트 검열(redact)·부모 게이트·워크스페이스 정리 같은 부수 동작은 흉내 내지 않는다.

테스트가 상황을 만들 때 쓰는 도우미(Hermes 에 없는 것):
- `start_run(conn, task_id, source_status=)` — 워커가 claim 한 상태를 만든다.
- `write_worker_log(task_id, text, board=)` — 워커 로그 파일을 놓는다.

설치: `install_fake_kanban_actions(fake_api, root)` — `fake_api.kanban` 도 같이 바꿔 둔다.
"""

import types
from pathlib import Path
from typing import Optional

from tests.fakes_kanban import (
    KANBAN_DB_SYMBOLS,
    AttachmentTooLarge,
    FakeAttachment,
    FakeKanbanDb,
    FakeRun,
)


class FakeKanbanActionsDb(FakeKanbanDb):
    """상태 전이·실행·첨부·로그가 동작하는 인메모리 칸반."""

    def __init__(self, root):
        super().__init__(root)
        self.signals = []  # reclaim 이 워커에 보낸 (pid, lock) 기록 — 실제 kill 은 하지 않는다
        self.calls = []  # (함수 이름, kwargs) 호출 기록 — 핸들러가 넘긴 인자를 단정할 때 쓴다

    # ---- 테스트 도우미 --------------------------------------------------------
    def start_run(self, conn, task_id: str, *, source_status: str = "ready", profile=None, pid=None) -> FakeRun:
        """`source_status -> running` 으로 claim 하고 run 을 연다. Hermes `_claim_and_open_run` 과 같이
        `claimed` 사건 payload 에 `source_status` 를 남긴다 — 검토자 run 판정의 근거가 이것이다."""
        task = self._require_task(conn, task_id)
        task.status = "running"
        task.claim_lock = f"lock-{self._next()}"
        task.worker_pid = pid
        task.started_at = task.started_at or self._now()
        run = FakeRun(
            id=self._next(), task_id=task_id, profile=profile or task.assignee, status="running",
            started_at=self._now(), claim_lock=task.claim_lock, worker_pid=pid,
        )
        self._state(conn).runs[run.id] = run
        task.current_run_id = run.id
        self._append_event(
            conn, task_id, "claimed",
            {"lock": task.claim_lock, "run_id": run.id, "source_status": source_status}, run_id=run.id,
        )
        return run

    def write_worker_log(self, task_id: str, text: str, *, board=None):
        path = self._worker_log_path(task_id, board=board)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _worker_log_path(self, task_id: str, *, board=None):
        return self.board_dir(board) / "logs" / f"{task_id}.log"

    def _active_run(self, conn, task_id: str) -> Optional[FakeRun]:
        runs = [r for r in self.list_runs(conn, task_id) if r.ended_at is None]
        return runs[-1] if runs else None

    def _latest_event(self, conn, task_id: str, kind: str, run_id=None):
        for ev in reversed(self._state(conn).events):
            if ev.task_id == task_id and ev.kind == kind and (run_id is None or ev.run_id == run_id):
                return ev
        return None

    # ---- 실행 ---------------------------------------------------------------
    def _retry_status_for_run(self, conn, task_id: str, run_id=None):
        """Hermes 와 같은 규칙: run 의 `claimed` 사건이 `source_status=review` 면 review, 아니면 ready."""
        if run_id is None:
            task = self.get_task(conn, task_id)
            run_id = task.current_run_id if task else None
        if run_id is None:
            return "ready"
        ev = self._latest_event(conn, task_id, "claimed", run_id)
        payload = ev.payload if ev else {}
        return "review" if payload.get("source_status") == "review" else "ready"

    def _end_run(self, conn, task_id: str, *, outcome: str, summary=None, error=None, metadata=None, status=None):
        run = self._active_run(conn, task_id)
        if run is None:
            return None
        run.ended_at = self._now()
        run.outcome = outcome
        run.status = status or outcome
        run.summary = summary
        run.error = error
        run.metadata = metadata
        task = self._require_task(conn, task_id)
        task.current_run_id = None
        task.claim_lock = None
        task.worker_pid = None
        return run.id

    # ---- 상태 전이 ----------------------------------------------------------
    def assign_task(self, conn, task_id: str, profile: Optional[str]) -> bool:
        task = self.get_task(conn, task_id)
        if task is None:
            return False
        if task.status == "running":
            raise RuntimeError(f"task {task_id} is running; reclaim first")
        task.assignee = profile
        self._append_event(conn, task_id, "assigned", {"assignee": profile})
        return True

    def reclaim_task(self, conn, task_id: str, *, reason=None, signal_fn=None):
        self.calls.append(("reclaim_task", {"reason": reason}))
        task = self.get_task(conn, task_id)
        if task is None or (task.status != "running" and task.claim_lock is None):
            return False
        self.signals.append((task.worker_pid, task.claim_lock))
        retry_status = self._retry_status_for_run(conn, task_id)
        self._end_run(conn, task_id, outcome="reclaimed", status="reclaimed", error=f"manual_reclaim: {reason}")
        task.status = retry_status
        task.consecutive_failures = 0
        self._append_event(conn, task_id, "reclaimed", {"manual": True, "reason": reason, "retry_status": retry_status})
        return True

    def reassign_task(self, conn, task_id: str, profile: Optional[str], *, reclaim_first: bool = False, reason=None):
        self.calls.append(("reassign_task", {"profile": profile, "reclaim_first": reclaim_first, "reason": reason}))
        if reclaim_first:
            self.reclaim_task(conn, task_id, reason=reason or "reassign")
        # Hermes 는 RuntimeError 를 삼켜 False 로 바꾼다 — 실행 중이면 False 다.
        try:
            return self.assign_task(conn, task_id, profile)
        except RuntimeError:
            return False

    def complete_task(self, conn, task_id: str, *, result=None, summary=None, metadata=None, created_cards=None,
                      expected_run_id=None, fire_lifecycle_hook: bool = True):
        self.calls.append(("complete_task", {"result": result, "summary": summary}))
        task = self.get_task(conn, task_id)
        if task is None or task.status not in ("running", "ready", "blocked", "review"):
            return False
        if not self._parents_satisfied(conn, task_id):
            return False
        task.status = "done"
        task.result = result
        task.completed_at = self._now()
        self._end_run(conn, task_id, outcome="completed", status="done", summary=summary or result, metadata=metadata)
        task.claim_lock = None
        task.worker_pid = None
        self._append_event(conn, task_id, "completed", {"summary": summary or result})
        return True

    def request_changes(self, conn, task_id: str, *, reason: str, expected_run_id=None):
        self.calls.append(("request_changes", {"reason": reason}))
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
        self._end_run(conn, task_id, outcome="changes_requested", status="ready", summary=reason)
        task.status = "ready"
        task.assignee = implementer
        self._append_event(conn, task_id, "changes_requested", {"reason": reason, "implementer": implementer})
        return True, implementer

    def request_review(self, conn, task_id: str, *, summary=None, metadata=None, reviewer=None, expected_run_id=None,
                       force: bool = False, with_reason: bool = False):
        task = self.get_task(conn, task_id)
        if task is None:
            return False
        implementer = task.assignee
        self._end_run(conn, task_id, outcome="review_requested", status="review", summary=summary, metadata=metadata)
        task.status = "review"
        if reviewer:
            task.assignee = reviewer
        self._append_event(conn, task_id, "review_requested", {"implementer": implementer, "reviewer": reviewer})
        return True

    def unblock_task(self, conn, task_id: str) -> bool:
        self.calls.append(("unblock_task", {}))
        task = self.get_task(conn, task_id)
        if task is None or task.status not in ("blocked", "scheduled"):
            return False
        task.status = "ready"
        self._append_event(conn, task_id, "unblocked", {"status": "ready"})
        return True

    def reopen_review_task(self, conn, task_id: str) -> bool:
        self.calls.append(("reopen_review_task", {}))
        task = self.get_task(conn, task_id)
        if task is None or task.status != "review":
            return False
        requested = self._latest_event(conn, task_id, "review_requested")
        implementer = (requested.payload or {}).get("implementer") if requested else None
        task.status = "ready"
        task.current_run_id = None
        if implementer:
            task.assignee = implementer
        self._append_event(conn, task_id, "review_reopened", {"status": "ready"})
        return True

    def archive_task(self, conn, task_id: str) -> bool:
        self.calls.append(("archive_task", {}))
        task = self.get_task(conn, task_id)
        if task is None or task.status == "archived":
            return False
        task.status = "archived"
        self._end_run(conn, task_id, outcome="reclaimed", status="reclaimed", summary="task archived with run still active")
        self._append_event(conn, task_id, "archived", None)
        return True

    def block_task(self, conn, task_id: str, *, reason=None, kind=None, expected_run_id=None):
        task = self.get_task(conn, task_id)
        if task is None:
            return False
        task.status = "blocked"
        self._append_event(conn, task_id, "blocked", {"reason": reason, "kind": kind})
        return True

    # ---- 첨부 ---------------------------------------------------------------
    def store_attachment_bytes(self, conn, task_id: str, filename: str, data: bytes, *, content_type=None,
                               uploaded_by=None, board=None, max_bytes=None):
        self.calls.append(("store_attachment_bytes", {"filename": filename, "content_type": content_type,
                                                      "uploaded_by": uploaded_by, "board": board, "max_bytes": max_bytes}))
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


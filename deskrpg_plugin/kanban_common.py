"""칸반 네 모듈(`kanban_board`·`kanban_actions`·`kanban_files`·`kanban_ops`)이 공유하는 도우미.

보드 열기·카드 조회·행위자 판정·직렬화를 한 곳에 둔다. 예전에는 `kanban_board` 의 비공개 이름을
다른 모듈이 끌어다 썼는데, 그러면 `kanban_board` 의 내부 정리가 이웃 모듈을 깨뜨린다 — 여기 있는
이름은 전부 공개 API 다(밑줄 없음). **동작은 옮기기 전과 같다.**

응답 모양은 `contract_fields` 의 키 집합으로 **투영**한다. Hermes 가 주는 추가 필드(claim_lock,
db_path …)는 계약에 없으므로 내보내지 않는다 — DeskRPG 는 계약 키만 읽고, 키 집합이 고정돼야
테스트가 계약 위반을 잡는다.
"""

import contextlib
from dataclasses import asdict

from .common import RequestError, board_conn
from .contract_fields import KANBAN_TASK_FULL_KEYS

ACTOR_HEADER = "X-DeskRPG-Actor"
DEFAULT_ACTOR = "deskrpg"
# 헤더 값 상한 — DeskRPG 닉네임을 `deskrpg:<nick>` 으로 넘기므로 넉넉하되 무한하지 않게.
ACTOR_MAX_CHARS = 100

# 카드 요약(보드 열)에 싣는 latest_summary 미리보기 길이 — 대시보드와 같다. 전문은 상세에서.
CARD_SUMMARY_PREVIEW_CHARS = 200
# Hermes `kanban_diagnostics.SEVERITY_ORDER` 와 같은 순서(낮음 → 높음).
SEVERITY_ORDER = ("warning", "error", "critical")


# ---------------------------------------------------------------------------
# 행위자 · 보드 · 카드
# ---------------------------------------------------------------------------


def actor_from_request(request) -> str:
    """`X-DeskRPG-Actor: <값>` 이 있으면 `deskrpg:<값>`, 없으면 `deskrpg`(spec §5.3 created_by 규칙과 같다)."""
    raw = (request.headers.get(ACTOR_HEADER) or "").strip()
    return f"{DEFAULT_ACTOR}:{raw[:ACTOR_MAX_CHARS]}" if raw else DEFAULT_ACTOR


@contextlib.contextmanager
def open_board(api, slug: str):
    """보드 존재를 확인한 뒤 연결을 연다. **워커 스레드 안에서만** 쓴다. 없으면 404.

    `board_conn` 은 `init_db(board=)` 부터 부르는데, 모르는 슬러그로 그걸 부르면 Hermes 가
    빈 보드를 만들어 버릴 수 있다 — 존재 검사가 먼저다.
    """
    if not api.board_exists(slug):
        raise RequestError(404, "board_not_found", slug)
    with board_conn(api, slug) as conn:
        yield conn


def require_task(api, conn, task_id: str):
    """카드 객체. 없으면 404 `task_not_found`."""
    task = api.get_task(conn, task_id)
    if task is None:
        raise RequestError(404, "task_not_found", task_id)
    return task


# ---------------------------------------------------------------------------
# 직렬화 — 투영 · 롤업 · 진단
# ---------------------------------------------------------------------------


def project(d: dict, keys) -> dict:
    return {k: v for k, v in d.items() if k in keys}


def task_dict(task, *, latest_summary=None) -> dict:
    d = asdict(task)
    d["latest_summary"] = latest_summary
    return d


def _placeholders(ids) -> str:
    return ",".join("?" for _ in ids)


def compute_diagnostics(api, conn, task_ids=None) -> dict:
    """`{task_id: [diagnostic_dict…]}` — 진단이 없는 카드는 빠진다. 대시보드 `_compute_task_diagnostics` 이식."""
    if task_ids is not None and not task_ids:
        return {}
    diag_config = api.config_from_runtime_config(api.load_config())
    if task_ids is not None:
        rows = conn.execute(f"SELECT * FROM tasks WHERE id IN ({_placeholders(task_ids)})", tuple(task_ids)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tasks WHERE status != 'archived'").fetchall()
    if not rows:
        return {}
    row_ids = [r["id"] for r in rows]

    def rows_by_task(table: str) -> dict:
        by_task = {tid: [] for tid in row_ids}
        for row in conn.execute(
            f"SELECT * FROM {table} WHERE task_id IN ({_placeholders(row_ids)}) ORDER BY id", tuple(row_ids)
        ):
            by_task.setdefault(row["task_id"], []).append(row)
        return by_task

    events_by_task = rows_by_task("task_events")
    runs_by_task = rows_by_task("task_runs")
    graph_by_task = api.task_graph_contexts(conn, row_ids)
    out = {}
    for r in rows:
        tid = r["id"]
        diags = api.compute_task_diagnostics(
            r, events_by_task[tid], runs_by_task[tid], config=diag_config, graph=graph_by_task.get(tid)
        )
        if diags:
            out[tid] = [d.to_dict() for d in diags]
    return out


def warnings_summary(diagnostics) -> dict | None:
    """카드 배지용 `{count, highest_severity}`; 진단이 없으면 None."""
    if not diagnostics:
        return None
    count, highest = 0, -1
    for d in diagnostics:
        count += d.get("count", 1)
        sev = d.get("severity")
        if sev in SEVERITY_ORDER:
            highest = max(highest, SEVERITY_ORDER.index(sev))
    return {"count": count, "highest_severity": SEVERITY_ORDER[highest] if highest >= 0 else None}


def rollups(conn) -> tuple[dict, dict, dict]:
    """(link_counts, comment_counts, progress) — 각각 집계 질의 한 번. 대시보드와 같은 SQL."""
    link_counts: dict = {}
    for row in conn.execute("SELECT parent_id, child_id FROM task_links").fetchall():
        link_counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})["children"] += 1
        link_counts.setdefault(row["child_id"], {"parents": 0, "children": 0})["parents"] += 1
    comment_counts = {
        r["task_id"]: r["n"] for r in conn.execute("SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id")
    }
    progress: dict = {}
    for row in conn.execute(
        "SELECT l.parent_id AS pid, t.status AS cstatus FROM task_links l JOIN tasks t ON t.id = l.child_id"
    ).fetchall():
        p = progress.setdefault(row["pid"], {"done": 0, "total": 0})
        p["total"] += 1
        p["done"] += row["cstatus"] == "done"
    return link_counts, comment_counts, progress


def decorate(d: dict, task_id: str, link_counts, comment_counts, progress, diagnostics) -> None:
    d["link_counts"] = link_counts.get(task_id, {"parents": 0, "children": 0})
    d["comment_count"] = comment_counts.get(task_id, 0)
    d["progress"] = progress.get(task_id)  # 자식이 없으면 None
    d["warnings"] = warnings_summary(diagnostics)
    if diagnostics:
        d["diagnostics"] = diagnostics


def task_payload(api, conn, task_id: str) -> dict:
    """KanbanTaskFull — 상세·생성·수정·동작 응답이 전부 이것을 쓴다. latest_summary 는 전문. 없으면 404."""
    task = require_task(api, conn, task_id)
    d = task_dict(task, latest_summary=api.latest_summary(conn, task_id))
    link_counts, comment_counts, progress = rollups(conn)
    diagnostics = compute_diagnostics(api, conn, task_ids=[task_id]).get(task_id)
    from .contract_fields import has_review_policy

    d["review"] = api.get_review_state(conn, task_id) if has_review_policy(api) else None
    decorate(d, task_id, link_counts, comment_counts, progress, diagnostics)
    return project(d, KANBAN_TASK_FULL_KEYS)


def attachment_payload(att) -> dict:
    """첨부 한 건의 유일한 직렬화 — 상세(`GET tasks/{id}`)·목록·업로드가 같은 모양을 낸다.

    계약 키(`KANBAN_ATTACHMENT_KEYS`: id·filename·size)의 **상위집합**이다. `content_type`·`created_at` 은
    DeskRPG 가 미리보기·정렬에 쓴다 — 셋 중 한 응답에서만 빠지면 클라이언트가 응답마다 분기해야 한다.
    """
    return {
        "id": att.id,
        "filename": att.filename,
        "size": att.size,
        "content_type": getattr(att, "content_type", None),
        "created_at": getattr(att, "created_at", None),
    }


__all__ = [
    "ACTOR_HEADER",
    "DEFAULT_ACTOR",
    "ACTOR_MAX_CHARS",
    "CARD_SUMMARY_PREVIEW_CHARS",
    "SEVERITY_ORDER",
    "actor_from_request",
    "open_board",
    "require_task",
    "project",
    "task_dict",
    "compute_diagnostics",
    "warnings_summary",
    "rollups",
    "decorate",
    "task_payload",
    "attachment_payload",
]


def run_claimed_from_review(api, conn, task_id: str, run_id) -> bool:
    """Whether run `run_id` was claimed from the `review` column — a reviewer run, not an implementation run.

    Hermes records it on the run's `claimed` event (`source_status="review"`) and reads it back with its private
    `_retry_status_for_run`. This reads the same event through the public `list_events`, so a reviewer run is never
    mistaken for an implementation run and no Hermes internal is needed."""
    if run_id is None:
        return False
    for event in reversed(api.list_events(conn, task_id)):
        if event.kind == "claimed" and event.run_id == run_id:
            return (event.payload or {}).get("source_status") == "review"
    return False

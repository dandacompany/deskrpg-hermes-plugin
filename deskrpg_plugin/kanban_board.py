"""칸반 보드·보드 보기·카드 CRUD·댓글·링크 (spec §5.1–5.3, §5.5 링크).

Hermes 대시보드 플러그인(`plugins/kanban/dashboard/plugin_api.py`)의 로직을 이식했다. 그쪽은
FastAPI 를 import 하므로 함수를 가져오지 않고 **규칙만** 옮겼다 — 보드 롤업 질의, PATCH 의
전이 분기(`_drag_to`/`_set_status_direct`), 제목·본문·우선순위의 raw UPDATE + 사건 기록.

모든 핸들러는 `xxx_handler(api)` 팩토리다(`profiles.py` 와 같은 꼴). DB 작업은 전부
`run_blocking` 안에서 `board_conn` 으로 연결을 열고 닫는다(C3). 오류는 `RequestError` →
`json_error` 로, 예상 못 한 예외는 500 `internal_error` 로 — 메시지에 본문·비밀을 싣지 않는다(C4·C6).

응답 모양은 `contract_fields` 의 키 집합으로 **투영**한다. Hermes 가 주는 추가 필드(claim_lock,
db_path …)는 계약에 없으므로 내보내지 않는다 — DeskRPG 는 계약 키만 읽고, 키 집합이 고정돼야
테스트가 계약 위반을 잡는다.
"""

import functools
import json
import os
import time
from dataclasses import asdict

from aiohttp import web

from . import deleted_log
from .common import (
    BOARD_SLUG_RE,
    RequestError,
    board_conn,
    json_error,
    log_event,
    logger,
    parse_board_slug,
    read_json_object,
    require_bool,
    require_int,
    require_str,
    require_str_list,
    run_blocking,
)
from .contract_fields import (
    BOARD_COLUMNS,
    BOARD_META_KEYS,
    CARD_STATUSES,
    CREATE_TASK_KEYS,
    KANBAN_ATTACHMENT_KEYS,
    KANBAN_COMMENT_KEYS,
    KANBAN_EVENT_KEYS,
    KANBAN_RUN_KEYS,
    KANBAN_TASK_FULL_KEYS,
    KANBAN_TASK_KEYS,
    UPDATE_BOARD_KEYS,
    UPDATE_TASK_KEYS,
    WORKSPACE_KINDS,
)

# 카드 요약(보드 열)에 싣는 latest_summary 미리보기 길이 — 대시보드와 같다. 전문은 상세에서.
_CARD_SUMMARY_PREVIEW_CHARS = 200
# Hermes `kanban_diagnostics.SEVERITY_ORDER` 와 같은 순서(낮음 → 높음).
_SEVERITY_ORDER = ("warning", "error", "critical")
# 댓글 작성자 상한 — DeskRPG 닉네임을 `deskrpg:<nick>` 으로 넘기므로 넉넉하되 무한하지 않게.
_AUTHOR_MAX_CHARS = 100
_DEFAULT_ACTOR = "deskrpg"
_ACTOR_HEADER = "X-DeskRPG-Actor"


# ---------------------------------------------------------------------------
# 공통 — 오류 변환, 보드 확인, 투영
# ---------------------------------------------------------------------------


def _guarded(fn):
    """핸들러 본문을 감싼다: `RequestError` 는 그 응답으로, 나머지 예외는 500(내용 없이)."""

    @functools.wraps(fn)
    async def wrapper(request):
        try:
            return await fn(request)
        except RequestError as exc:
            return exc.response()
        except Exception as exc:  # noqa: BLE001 — 마지막 방어선. 내용은 로그에만, 그것도 타입만.
            logger.exception("[deskrpg] kanban 핸들러 예외: %s", type(exc).__name__)
            return json_error(500, "internal_error", type(exc).__name__)

    return wrapper


def _require_board(api, slug: str) -> str:
    if not api.board_exists(board=slug):
        raise RequestError(404, "board_not_found", slug)
    return slug


def _board_from_query(api, request) -> str:
    return _require_board(api, parse_board_slug(request))


def _board_from_path(api, request) -> str:
    slug = request.match_info["slug"]
    if not BOARD_SLUG_RE.fullmatch(slug):
        raise RequestError(400, "invalid_board", f"보드 슬러그 형식이 아니다: {slug!r}")
    return _require_board(api, slug)


def _actor(request) -> str:
    value = (request.headers.get(_ACTOR_HEADER) or "").strip()
    return f"{_DEFAULT_ACTOR}:{value}" if value else _DEFAULT_ACTOR


def _reject_unknown_keys(body: dict, allowed) -> None:
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise RequestError(400, "unknown_field", ", ".join(unknown))


def _project(d: dict, keys) -> dict:
    return {k: v for k, v in d.items() if k in keys}


def _require_task(api, conn, task_id: str):
    task = api.get_task(conn, task_id)
    if task is None:
        raise RequestError(404, "task_not_found", task_id)
    return task


def _validate_workdir(path):
    """`default_workdir` 는 존재하는 절대경로만. `""` 는 비움(그대로 통과), None 은 미지정."""
    if path is None or path == "":
        return path
    if not os.path.isabs(path) or not os.path.isdir(path):
        raise RequestError(400, "invalid_workdir", "default_workdir 는 존재하는 절대경로여야 한다")
    return path


# ---------------------------------------------------------------------------
# 보드 메타
# ---------------------------------------------------------------------------


def _board_meta(api, meta: dict, *, total=None, counts=None) -> dict:
    out = _project(dict(meta), BOARD_META_KEYS)
    out["slug"] = meta["slug"]
    out["is_current"] = meta["slug"] == api.get_current_board()
    if total is not None:
        out["total"] = total
    if counts is not None:
        out["counts"] = counts
    return out


def _board_task_counts(api, slug: str) -> tuple[int, dict]:
    """(보관 제외 카드 수, 상태별 개수 — 보관 포함)."""
    with board_conn(api, slug) as conn:
        tasks = api.list_tasks(conn, include_archived=True)
    counts: dict = {}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    total = sum(n for s, n in counts.items() if s != "archived")
    return total, counts


def _find_board(api, slug: str):
    for meta in api.list_boards(include_archived=True):
        if meta.get("slug") == slug:
            return meta
    return None


def list_boards_handler(api):
    @_guarded
    async def handler(request):
        def work():
            out = []
            for meta in api.list_boards(include_archived=True):
                total, counts = _board_task_counts(api, meta["slug"])
                out.append(_board_meta(api, meta, total=total, counts=counts))
            return {"boards": out, "current": api.get_current_board()}

        return web.json_response(await run_blocking(work))

    return handler


def create_board_handler(api):
    """새로 만들면 201, 이미 있으면 200 + 기존(이름을 덮어쓰지 않는다). 현재 보드는 바꾸지 않는다."""

    @_guarded
    async def handler(request):
        body = await read_json_object(request)
        _reject_unknown_keys(body, {"slug", "name", "default_workdir"})
        slug = require_str(body, "slug")
        if not BOARD_SLUG_RE.fullmatch(slug):
            raise RequestError(400, "invalid_board", f"보드 슬러그 형식이 아니다: {slug!r}")
        name = require_str(body, "name")
        workdir = _validate_workdir(require_str(body, "default_workdir", required=False, allow_empty=True))

        def work():
            if api.board_exists(board=slug):
                # Hermes `create_board` 는 mkdir -p 라 name 을 덮어쓴다 — 멱등 재요청에 이름이 바뀌면 안 된다.
                existing = _find_board(api, slug)
                total, counts = _board_task_counts(api, slug)
                return 200, _board_meta(api, existing or {"slug": slug}, total=total)
            meta = api.create_board(slug, name=name, default_workdir=workdir or None)
            log_event("board.create", board=slug)
            return 201, _board_meta(api, meta, total=0)

        status, board = await run_blocking(work)
        return web.json_response({"board": board}, status=status)

    return handler


def patch_board_handler(api):
    @_guarded
    async def handler(request):
        slug = _board_from_path(api, request)
        body = await read_json_object(request)
        _reject_unknown_keys(body, UPDATE_BOARD_KEYS)
        name = require_str(body, "name", required=False, allow_empty=True)
        description = require_str(body, "description", required=False, allow_empty=True)
        workdir = _validate_workdir(require_str(body, "default_workdir", required=False, allow_empty=True))

        def work():
            meta = api.write_board_metadata(slug, name=name, description=description, default_workdir=workdir)
            total, _counts = _board_task_counts(api, slug)
            log_event("board.patch", board=slug, fields=[k for k in body])
            return _board_meta(api, meta, total=total)

        return web.json_response({"board": await run_blocking(work)})

    return handler


# ---------------------------------------------------------------------------
# 보드 보기 — 롤업·진단
# ---------------------------------------------------------------------------


def _placeholders(ids) -> str:
    return ",".join("?" for _ in ids)


def _compute_diagnostics(api, conn, task_ids=None) -> dict:
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


def _warnings_summary(diagnostics) -> dict | None:
    """카드 배지용 `{count, highest_severity}`; 진단이 없으면 None."""
    if not diagnostics:
        return None
    count, highest = 0, -1
    for d in diagnostics:
        count += d.get("count", 1)
        sev = d.get("severity")
        if sev in _SEVERITY_ORDER:
            highest = max(highest, _SEVERITY_ORDER.index(sev))
    return {"count": count, "highest_severity": _SEVERITY_ORDER[highest] if highest >= 0 else None}


def _task_dict(task, *, latest_summary=None) -> dict:
    d = asdict(task)
    d["latest_summary"] = latest_summary
    return d


def _rollups(conn) -> tuple[dict, dict, dict]:
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


def _decorate(d: dict, task_id: str, link_counts, comment_counts, progress, diagnostics) -> None:
    d["link_counts"] = link_counts.get(task_id, {"parents": 0, "children": 0})
    d["comment_count"] = comment_counts.get(task_id, 0)
    d["progress"] = progress.get(task_id)  # 자식이 없으면 None
    d["warnings"] = _warnings_summary(diagnostics)
    if diagnostics:
        d["diagnostics"] = diagnostics


def get_board_handler(api):
    @_guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        include_archived = request.query.get("include_archived", "").lower() in ("1", "true", "yes")

        def work():
            with board_conn(api, slug) as conn:
                tasks = api.list_tasks(conn, include_archived=include_archived)
                link_counts, comment_counts, progress = _rollups(conn)
                diagnostics = _compute_diagnostics(api, conn, task_ids=None)
                latest_event_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"]
                summary_map = api.latest_summaries(conn, [t.id for t in tasks])
                columns = {c: [] for c in BOARD_COLUMNS}
                if include_archived:
                    columns["archived"] = []
                for t in tasks:
                    full = summary_map.get(t.id)
                    d = _task_dict(t, latest_summary=(full[:_CARD_SUMMARY_PREVIEW_CHARS] if full else None))
                    _decorate(d, t.id, link_counts, comment_counts, progress, diagnostics.get(t.id))
                    columns[t.status if t.status in columns else "todo"].append(_project(d, KANBAN_TASK_KEYS))
                tenants = [
                    r["tenant"]
                    for r in conn.execute("SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant")
                ]
                assignees = [
                    r["assignee"]
                    for r in conn.execute(
                        "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL AND status != 'archived' ORDER BY assignee"
                    )
                ]
            return {
                "columns": [{"name": name, "tasks": columns[name]} for name in columns],
                "tenants": tenants,
                "assignees": assignees,
                "latest_event_id": int(latest_event_id),
                "now": int(time.time()),
            }

        return web.json_response(await run_blocking(work))

    return handler


# ---------------------------------------------------------------------------
# 카드 상세·생성
# ---------------------------------------------------------------------------


def _task_full(api, conn, task_id: str) -> dict:
    """KanbanTaskFull — 상세·생성·수정 응답이 공유한다. latest_summary 는 전문."""
    task = _require_task(api, conn, task_id)
    d = _task_dict(task, latest_summary=api.latest_summary(conn, task_id))
    link_counts, comment_counts, progress = _rollups(conn)
    diagnostics = _compute_diagnostics(api, conn, task_ids=[task_id]).get(task_id)
    _decorate(d, task_id, link_counts, comment_counts, progress, diagnostics)
    return _project(d, KANBAN_TASK_FULL_KEYS)


def get_task_handler(api):
    @_guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        task_id = request.match_info["task_id"]

        def work():
            with board_conn(api, slug) as conn:
                task = _task_full(api, conn, task_id)
                return {
                    "task": task,
                    "comments": [_project(asdict(c), KANBAN_COMMENT_KEYS) for c in api.list_comments(conn, task_id)],
                    "events": [_project(asdict(e), KANBAN_EVENT_KEYS) for e in api.list_events(conn, task_id)],
                    "attachments": [_project(asdict(a), KANBAN_ATTACHMENT_KEYS) for a in api.list_attachments(conn, task_id)],
                    "links": {"parents": api.parent_ids(conn, task_id), "children": api.child_ids(conn, task_id)},
                    "runs": [_project(asdict(r), KANBAN_RUN_KEYS) for r in api.list_runs(conn, task_id)],
                }

        return web.json_response(await run_blocking(work))

    return handler


def _parse_create_body(body: dict) -> dict:
    """CreateTaskBody 타입 검사. 값이 있는 키만 담아 `create_task(**fields)` 로 넘긴다."""
    _reject_unknown_keys(body, CREATE_TASK_KEYS)
    fields = {"title": require_str(body, "title")}
    for key in ("body", "assignee", "tenant", "workspace_path", "idempotency_key",
                "model_override", "provider_override", "reasoning_effort", "project_id"):
        value = require_str(body, key, required=False, allow_empty=True)
        if value is not None:
            fields[key] = value
    for key, minimum in (("priority", None), ("max_runtime_seconds", 1), ("goal_max_turns", 1)):
        value = require_int(body, key, required=False, minimum=minimum)
        if value is not None:
            fields[key] = value
    for key in ("triage", "goal_mode"):
        value = require_bool(body, key, required=False)
        if value is not None:
            fields[key] = value
    for key in ("parents", "skills"):
        value = require_str_list(body, key, required=False)
        if value is not None:
            fields[key] = value
    kind = require_str(body, "workspace_kind", required=False)
    if kind is not None:
        if kind not in WORKSPACE_KINDS:
            raise RequestError(400, "invalid_field", f"workspace_kind 는 {'|'.join(WORKSPACE_KINDS)} 중 하나여야 한다")
        fields["workspace_kind"] = kind
    return fields


def _dispatcher_missing(api) -> bool:
    """`_check_dispatcher_presence` 가 (False, …) 를 주면 True. 프로브 실패는 fail-open(경고 없음)."""
    try:
        present, _message = api._check_dispatcher_presence(api.get_hermes_home())
        return not bool(present)
    except Exception:
        return False


def create_task_handler(api):
    @_guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        body = await read_json_object(request)
        fields = _parse_create_body(body)
        created_by = _actor(request)

        def work():
            with board_conn(api, slug) as conn:
                try:
                    task_id = api.create_task(conn, created_by=created_by, board=slug, **fields)
                except ValueError as exc:
                    raise RequestError(400, "invalid_task", str(exc))
                out = {"task": _task_full(api, conn, task_id)}
            if _dispatcher_missing(api):
                out["warning"] = "dispatcher_missing"
            log_event("task.create", board=slug, task_id=task_id, title=fields["title"])
            return out

        return web.json_response(await run_blocking(work), status=201)

    return handler


# ---------------------------------------------------------------------------
# PATCH — 대시보드 `update_task` 의 규칙 이식
# ---------------------------------------------------------------------------

_MISSING = object()
_PATCH_SUPPORTED = frozenset({
    "title", "body", "priority", "assignee", "status", "model_override", "provider_override", "reasoning_effort",
})


def _set_status_direct(api, conn, task_id: str, new_status: str) -> bool:
    """구조화된 동사가 없는 이동(todo↔ready, running→ready …)의 직접 상태 쓰기 + `status` 사건.
    running 에서 나오면 run 을 'reclaimed' 로 닫고 워커는 **커밋 뒤에** 죽인다."""
    terminations = []
    effective = new_status
    with api.write_txn(conn):
        prev = conn.execute(
            "SELECT status, current_run_id, worker_pid, claim_lock FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if prev is None:
            return False
        if prev["status"] == "running" and new_status == "ready":
            if api._retry_status_for_run(conn, task_id, prev["current_run_id"]) == "review":
                effective = "review" if api._parents_satisfied(conn, task_id) else "todo"
        # 부모가 전부 끝나기 전에는 ready 로 올리지 않는다 — 디스패처가 상류가 덜 된 자식을 띄운다.
        if effective == "ready" and not api._parents_satisfied(conn, task_id):
            return False
        was_running = prev["status"] == "running"
        reopening_parent = prev["status"] in ("done", "archived") and effective not in ("done", "archived")
        cur = conn.execute(
            "UPDATE tasks SET status = ?, "
            "claim_lock = CASE WHEN ? = 'running' THEN claim_lock ELSE NULL END, "
            "claim_expires = CASE WHEN ? = 'running' THEN claim_expires ELSE NULL END, "
            "worker_pid = CASE WHEN ? = 'running' THEN worker_pid ELSE NULL END "
            "WHERE id = ?",
            (effective,) * 4 + (task_id,),
        )
        if cur.rowcount != 1:
            return False
        run_id = None
        if was_running and effective != "running" and prev["current_run_id"]:
            run_id = api._end_run(
                conn, task_id, outcome="reclaimed", status="reclaimed",
                summary=f"status changed to {effective} (deskrpg/direct)",
            )
            terminations.append((prev["worker_pid"], prev["claim_lock"]))
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'status', ?, ?)",
            (task_id, run_id, json.dumps({"status": effective, "requested_status": new_status}), int(time.time())),
        )
        if reopening_parent:
            result = api.invalidate_descendants_for_parent_reopen(conn, task_id, author=_DEFAULT_ACTOR)
            terminations.extend(result["terminations"])
    for pid, claim_lock in terminations:
        api._terminate_reclaimed_worker(pid, claim_lock)
    if effective in ("done", "ready", "review"):
        api.recompute_ready(conn)
    return True


def _drag_to(api, conn, task_id: str, status: str) -> bool:
    """ready/todo/triage 로의 이동. spec: blocked/scheduled 에서는 `unblock_task`, review 에서는
    `reopen_review_task`, 나머지는 직접 전이."""
    current = api.get_task(conn, task_id)
    if current is None:
        return False
    if current.status in ("blocked", "scheduled"):
        return api.unblock_task(conn, task_id)
    if current.status == "review":
        return api.reopen_review_task(conn, task_id)
    return _set_status_direct(api, conn, task_id, status)


def _apply_status(api, conn, task_id: str, status: str, reviewer) -> bool:
    if status == "done":
        return api.complete_task(conn, task_id)
    if status == "blocked":
        return api.block_task(conn, task_id)
    if status == "scheduled":
        return api.schedule_task(conn, task_id)
    if status == "review":
        # 사람의 조작은 살아 있는 워커 claim 을 덮어쓴다(force=True) — 대시보드와 같다.
        return api.request_review(conn, task_id, reviewer=reviewer, force=True)
    if status == "archived":
        return api.archive_task(conn, task_id)
    return _drag_to(api, conn, task_id, status)


def _parents_blocking_ready(api, conn, task_id: str) -> list:
    return [
        p for p in (api.get_task(conn, pid) for pid in api.parent_ids(conn, task_id))
        if p is not None and p.status not in ("done", "archived")
    ]


def _refused(api, conn, task_id: str, status: str, detail=None) -> RequestError:
    if status == "ready":
        blockers = _parents_blocking_ready(api, conn, task_id)
        if blockers:
            names = ", ".join(f"{p.title!r} ({p.id}, status={p.status})" for p in blockers)
            return RequestError(409, "invalid_transition", f"부모가 끝나지 않아 ready 로 갈 수 없다 — {names}")
    return RequestError(409, "invalid_transition", detail or f"현재 상태에서 {status!r} 로 갈 수 없다")


def _patch_status(api, conn, task_id: str, status: str, assignee) -> None:
    if status == "running":
        raise RequestError(409, "invalid_transition", "running 은 직접 지정할 수 없다 — 디스패처가 잡는다")
    try:
        ok = _apply_status(api, conn, task_id, status, assignee)
    except (RuntimeError, ValueError) as exc:
        raise _refused(api, conn, task_id, status, str(exc))
    if isinstance(ok, tuple):  # request_review(with_reason=…) 꼴 방어
        ok = ok[0]
    if not ok:
        raise _refused(api, conn, task_id, status)


def _patch_title_body(api, conn, task_id: str, title, body, board: str) -> None:
    """한 번의 UPDATE + `edited` 사건, 커밋 뒤 관찰자 통지(필드 이름만)."""
    sets, vals, changed = [], [], []
    if title is not _MISSING:
        sets.append("title = ?")
        vals.append(title.strip())
        changed.append("title")
    if body is not _MISSING:
        sets.append("body = ?")
        vals.append(body)
        changed.append("body")
    with api.write_txn(conn):
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", (*vals, task_id))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'edited', NULL, ?)",
            (task_id, int(time.time())),
        )
    api.notify_task_updated(conn, task_id, changed, board=board)


def _set_priority(api, conn, task_id: str, priority: int, board: str) -> None:
    with api.write_txn(conn):
        conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (int(priority), task_id))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'reprioritized', ?, ?)",
            (task_id, json.dumps({"priority": int(priority)}), int(time.time())),
        )
    api.notify_task_updated(conn, task_id, ("priority",), board=board)


def _parse_patch_body(body: dict) -> dict:
    """UpdateTaskBody. `null` 은 model_override/reasoning_effort 에서 '비움' 이고 다른 키에서는 400."""
    _reject_unknown_keys(body, UPDATE_TASK_KEYS)
    unsupported = sorted(set(body) - _PATCH_SUPPORTED)
    if unsupported:
        # 계약 키지만 PATCH 로 바꿀 길이 없는 것 — 조용히 버리면 클라이언트가 성공으로 오해한다.
        raise RequestError(400, "unsupported_field", ", ".join(unsupported))
    out = {}
    if "title" in body:
        out["title"] = require_str(body, "title")  # 빈 제목 400
    if "body" in body:
        out["body"] = require_str(body, "body", allow_empty=True)
    if "priority" in body:
        out["priority"] = require_int(body, "priority")
    if "assignee" in body:
        out["assignee"] = require_str(body, "assignee", allow_empty=True)
    if "status" in body:
        status = require_str(body, "status")
        if status not in CARD_STATUSES:
            raise RequestError(400, "invalid_field", f"status 는 {'|'.join(CARD_STATUSES)} 중 하나여야 한다")
        out["status"] = status
    for key in ("model_override", "provider_override", "reasoning_effort"):
        if key in body:
            out[key] = None if body[key] is None else require_str(body, key, allow_empty=True)
    return out


def patch_task_handler(api):
    @_guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        task_id = request.match_info["task_id"]
        p = _parse_patch_body(await read_json_object(request))

        def work():
            with board_conn(api, slug) as conn:
                _require_task(api, conn, task_id)
                status = p.get("status")
                assignee = p.get("assignee", _MISSING)
                # 담당자+review 를 함께 주면 request_review 가 구현자를 먼저 기록해야 하므로 assign 을 미룬다.
                review_deferred = status == "review" and assignee is not _MISSING
                if assignee is not _MISSING and not review_deferred:
                    try:
                        ok = api.assign_task(conn, task_id, assignee or None)
                    except RuntimeError as exc:
                        raise RequestError(409, "invalid_transition", str(exc))
                    if not ok:
                        raise RequestError(404, "task_not_found", task_id)
                if status is not None:
                    _patch_status(api, conn, task_id, status, (assignee or None) if review_deferred else None)
                if "model_override" in p or "provider_override" in p:
                    try:
                        ok = api.set_model_override(conn, task_id, p.get("model_override"), provider=p.get("provider_override"))
                    except (ValueError, RuntimeError) as exc:
                        raise RequestError(400, "invalid_field", str(exc))
                    if not ok:
                        raise RequestError(404, "task_not_found", task_id)
                if "reasoning_effort" in p:
                    try:
                        ok = api.set_reasoning_effort(conn, task_id, p["reasoning_effort"])
                    except (ValueError, RuntimeError) as exc:
                        raise RequestError(400, "invalid_field", str(exc))
                    if not ok:
                        raise RequestError(404, "task_not_found", task_id)
                if "priority" in p:
                    _set_priority(api, conn, task_id, p["priority"], slug)
                if "title" in p or "body" in p:
                    _patch_title_body(api, conn, task_id, p.get("title", _MISSING), p.get("body", _MISSING), slug)
                log_event("task.patch", board=slug, task_id=task_id, fields=sorted(p))
                return {"task": _task_full(api, conn, task_id)}

        return web.json_response(await run_blocking(work))

    return handler


# ---------------------------------------------------------------------------
# 삭제·댓글·링크
# ---------------------------------------------------------------------------


def delete_task_handler(api):
    """`delete_task` 뒤 삭제 장부에 한 줄 — 사건 스트림이 `task.deleted` 를 여기서 합성한다."""

    @_guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        task_id = request.match_info["task_id"]

        def work():
            with board_conn(api, slug) as conn:
                task = _require_task(api, conn, task_id)
                if not api.delete_task(conn, task_id):
                    raise RequestError(404, "task_not_found", task_id)
            line_no = deleted_log.append_deleted(api, slug, task_id, task.title)
            log_event("task.delete", board=slug, task_id=task_id, line_no=line_no)

        await run_blocking(work)
        return web.json_response({"ok": True})

    return handler


def add_comment_handler(api):
    @_guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        task_id = request.match_info["task_id"]
        body = await read_json_object(request)
        _reject_unknown_keys(body, {"author", "body"})
        text = require_str(body, "body")
        author = (require_str(body, "author", required=False) or _DEFAULT_ACTOR)[:_AUTHOR_MAX_CHARS]

        def work():
            with board_conn(api, slug) as conn:
                _require_task(api, conn, task_id)
                comment_id = api.add_comment(conn, task_id, author=author, body=text)
                for c in api.list_comments(conn, task_id):
                    if c.id == comment_id:
                        log_event("task.comment", board=slug, task_id=task_id, comment_id=comment_id, body=text)
                        return _project(asdict(c), KANBAN_COMMENT_KEYS)
            raise RuntimeError(f"add_comment 가 돌려준 id {comment_id} 가 목록에 없다")

        return web.json_response({"comment": await run_blocking(work)}, status=201)

    return handler


def link_handler(api, op: str):
    """`op` ∈ add|remove. 순환 400 `cycle`, 없는 카드 404. remove 는 `{ok: 실제로 지웠는지}`."""
    if op not in ("add", "remove"):
        raise ValueError(f"link_handler op 는 add|remove 다: {op!r}")

    @_guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        body = await read_json_object(request)
        _reject_unknown_keys(body, {"parent_id", "child_id"})
        parent_id = require_str(body, "parent_id")
        child_id = require_str(body, "child_id")

        def work():
            with board_conn(api, slug) as conn:
                for tid in (parent_id, child_id):
                    _require_task(api, conn, tid)
                if op == "remove":
                    ok = bool(api.unlink_tasks(conn, parent_id, child_id))
                else:
                    try:
                        api.link_tasks(conn, parent_id, child_id)
                    except ValueError as exc:
                        msg = str(exc)
                        code = "cycle" if ("cycle" in msg or "itself" in msg) else "invalid_link"
                        raise RequestError(400, code, msg)
                    ok = True
                log_event(f"link.{op}", board=slug, parent_id=parent_id, child_id=child_id, ok=ok)
                return {"ok": ok}

        return web.json_response(await run_blocking(work))

    return handler

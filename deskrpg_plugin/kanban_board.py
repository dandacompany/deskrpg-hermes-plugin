"""칸반 보드·보드 보기·카드 CRUD·댓글·링크 (spec §5.1–5.3, §5.5 링크).

Hermes 대시보드 플러그인(`plugins/kanban/dashboard/plugin_api.py`)의 로직을 이식했다. 그쪽은
FastAPI 를 import 하므로 함수를 가져오지 않고 **규칙만** 옮겼다 — 보드 롤업 질의, PATCH 의
전이 분기(`_drag_to`/`_set_status_direct`), 제목·본문·우선순위의 raw UPDATE + 사건 기록.

모든 핸들러는 `xxx_handler(api)` 팩토리다(`profiles.py` 와 같은 꼴). DB 작업은 전부
`run_blocking` 안에서 `board_conn` 으로 연결을 열고 닫는다(C3). 오류 변환은 `common.guarded` 가
한다 — `RequestError` 는 그 응답으로, 예상 못 한 예외는 500 `internal_error`(메시지에 본문·비밀 없음, C4·C6).

카드 조회·직렬화·행위자 판정은 `kanban_common` 의 공개 도우미를 쓴다(동작·첨부·운영 모듈과 공유).
응답 모양은 `contract_fields` 의 키 집합으로 **투영**한다.
"""

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
    guarded,
    log_event,
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
    INITIAL_STATUSES,
    has_review_policy,
    KANBAN_COMMENT_KEYS,
    KANBAN_EVENT_KEYS,
    KANBAN_RUN_KEYS,
    KANBAN_TASK_KEYS,
    UPDATE_BOARD_KEYS,
    UPDATE_TASK_KEYS,
    WORKSPACE_KINDS,
)
from .review_state import (
    hooks_enabled,
    open_approval_store,
    parse_policy,
    require_approval_store,
    review_field,
)
from .kanban_common import (
    ACTOR_MAX_CHARS,
    CARD_SUMMARY_PREVIEW_CHARS,
    DEFAULT_ACTOR,
    actor_from_request,
    attachment_payload,
    compute_diagnostics,
    decorate,
    project,
    require_task,
    rollups,
    task_dict,
    task_payload,
)


# ---------------------------------------------------------------------------
# 공통 — 보드 확인
# ---------------------------------------------------------------------------


def _require_board(api, slug: str) -> str:
    if not api.board_exists(board=slug):
        raise RequestError(404, "board_not_found", slug)
    return slug


def _board_from_query(api, request) -> str:
    return _require_board(api, parse_board_slug(request))


def _board_from_path(api, request) -> str:
    slug = request.match_info["slug"]
    if not BOARD_SLUG_RE.fullmatch(slug):
        raise RequestError(400, "invalid_board", f"not a valid board slug: {slug!r}")
    return _require_board(api, slug)


def _reject_unknown_keys(body: dict, allowed) -> None:
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise RequestError(400, "unknown_field", ", ".join(unknown))


def _validate_workdir(path):
    """`default_workdir` 는 존재하는 절대경로만. `""` 는 비움(그대로 통과), None 은 미지정."""
    if path is None or path == "":
        return path
    if not os.path.isabs(path) or not os.path.isdir(path):
        raise RequestError(400, "invalid_workdir", "default_workdir must be an existing absolute path")
    return path


# ---------------------------------------------------------------------------
# 보드 메타
# ---------------------------------------------------------------------------


def _board_meta(api, meta: dict, *, total=None, counts=None) -> dict:
    out = project(dict(meta), BOARD_META_KEYS)
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


def _include_archived(request) -> bool:
    return request.query.get("include_archived", "").lower() in ("1", "true", "yes")


def list_boards_handler(api):
    """보관된 보드는 `?include_archived=true` 일 때만 싣는다."""

    @guarded
    async def handler(request):
        include_archived = _include_archived(request)

        def work():
            out = []
            for meta in api.list_boards(include_archived=True):
                if meta.get("archived") and not include_archived:
                    continue
                total, counts = _board_task_counts(api, meta["slug"])
                out.append(_board_meta(api, meta, total=total, counts=counts))
            return {"boards": out, "current": api.get_current_board()}

        return web.json_response(await run_blocking(work))

    return handler


def create_board_handler(api):
    """새로 만들면 201, 이미 있으면 200 + 기존(이름을 덮어쓰지 않는다). 현재 보드는 바꾸지 않는다."""

    @guarded
    async def handler(request):
        body = await read_json_object(request)
        _reject_unknown_keys(body, {"slug", "name", "default_workdir"})
        slug = require_str(body, "slug")
        if not BOARD_SLUG_RE.fullmatch(slug):
            raise RequestError(400, "invalid_board", f"not a valid board slug: {slug!r}")
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


def _default_payload(slug, current) -> dict:
    return {"board": slug, "default": None if current is None else {
        "version": 1, "mode": current[0], "reviewer_profile": current[1]}}


def _require_hooks(api) -> None:
    # Like a card policy on a Hermes that cannot enforce one: refused, never stored and ignored.
    if not hooks_enabled(api):
        raise RequestError(428, "review_policy_required", "Hermes approval policy support is required")


def get_default_policy_handler(api):
    """`GET /deskrpg/kanban/boards/{slug}/default-policy` — the board's default policy, or `default: null`."""

    @guarded
    async def handler(request):
        from . import review_store

        slug = _board_from_path(api, request)
        _require_hooks(api)

        def work():
            store = require_approval_store(api)
            try:
                return _default_payload(slug, review_store.get_board_default(store, slug))
            finally:
                store.close()

        return web.json_response(await run_blocking(work))

    return handler


def default_policy_handler(api):
    """`PUT /deskrpg/kanban/boards/{slug}/default-policy` — the policy a card on this board gets when it has none
    of its own. `{"mode": null}` removes it. The key is the board slug, which is also what a kanban worker sees
    as its board, so the hooks find the same default."""

    @guarded
    async def handler(request):
        from . import review_store

        slug = _board_from_path(api, request)
        _require_hooks(api)
        body = await read_json_object(request)
        _reject_unknown_keys(body, ("mode", "reviewer_profile"))
        clearing = body.get("mode") is None
        policy = None if clearing else parse_policy(api, {"version": 1, **body}, None)

        def work():
            store = require_approval_store(api)
            try:
                if clearing:
                    review_store.clear_board_default(store, slug)
                else:
                    review_store.put_board_default(store, slug, *policy)
                current = review_store.get_board_default(store, slug)
            finally:
                store.close()
            return _default_payload(slug, current)

        return web.json_response(await run_blocking(work))

    return handler


def patch_board_handler(api):
    @guarded
    async def handler(request):
        slug = _board_from_path(api, request)
        body = await read_json_object(request)
        _reject_unknown_keys(body, UPDATE_BOARD_KEYS)
        name = require_str(body, "name", required=False, allow_empty=True)
        description = require_str(body, "description", required=False, allow_empty=True)
        workdir = _validate_workdir(require_str(body, "default_workdir", required=False, allow_empty=True))
        archived = require_bool(body, "archived", required=False)
        if archived and slug == "default":
            # Hermes `remove_board` 와 같은 원칙 — default 는 늘 있어야 하는 보드다.
            raise RequestError(400, "invalid_board", "the default board cannot be archived")

        def work():
            if archived:
                # 보관하면 디스패처가 이 보드를 건너뛴다. 돌고 있는 카드의 결과 알림이 끊기지 않게 막는다.
                _total, counts = _board_task_counts(api, slug)
                running = counts.get("running", 0)
                if running:
                    raise RequestError(409, "board_has_running_cards", f"{running} running").with_extra(running=running)
            meta = api.write_board_metadata(
                slug, name=name, description=description, default_workdir=workdir, archived=archived,
            )
            total, _counts = _board_task_counts(api, slug)
            if archived is not None:
                log_event("board.archive" if archived else "board.unarchive", board=slug)
            log_event("board.patch", board=slug, fields=[k for k in body])
            return _board_meta(api, meta, total=total)

        return web.json_response({"board": await run_blocking(work)})

    return handler


# ---------------------------------------------------------------------------
# 보드 보기 — 롤업·진단
# ---------------------------------------------------------------------------


def get_board_handler(api):
    @guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        include_archived = _include_archived(request)

        def work():
            with board_conn(api, slug) as conn:
                tasks = api.list_tasks(conn, include_archived=include_archived)
                link_counts, comment_counts, progress = rollups(conn)
                diagnostics = compute_diagnostics(api, conn, task_ids=None)
                latest_event_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"]
                summary_map = api.latest_summaries(conn, [t.id for t in tasks])
                columns = {c: [] for c in BOARD_COLUMNS}
                if include_archived:
                    columns["archived"] = []
                store = open_approval_store(api) if hooks_enabled(api) else None
                for t in tasks:
                    full = summary_map.get(t.id)
                    d = task_dict(t, latest_summary=(full[:CARD_SUMMARY_PREVIEW_CHARS] if full else None))
                    d["review"] = review_field(api, conn, t, slug, store=store)
                    decorate(d, t.id, link_counts, comment_counts, progress, diagnostics.get(t.id))
                    columns[t.status if t.status in columns else "todo"].append(project(d, KANBAN_TASK_KEYS))
                if store is not None:
                    store.close()
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


def get_task_handler(api):
    @guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        task_id = request.match_info["task_id"]

        def work():
            with board_conn(api, slug) as conn:
                task = task_payload(api, conn, task_id, board=slug)
                return {
                    "task": task,
                    "comments": [project(asdict(c), KANBAN_COMMENT_KEYS) for c in api.list_comments(conn, task_id)],
                    "events": [project(asdict(e), KANBAN_EVENT_KEYS) for e in api.list_events(conn, task_id)],
                    "attachments": [attachment_payload(a) for a in api.list_attachments(conn, task_id)],
                    "links": {"parents": api.parent_ids(conn, task_id), "children": api.child_ids(conn, task_id)},
                    "runs": [project(asdict(r), KANBAN_RUN_KEYS) for r in api.list_runs(conn, task_id)],
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
            raise RequestError(400, "invalid_field", f"workspace_kind must be one of {'|'.join(WORKSPACE_KINDS)}")
        fields["workspace_kind"] = kind
    status = require_str(body, "initial_status", required=False)
    if status is not None:
        # 모르는 값을 Hermes 로 흘리지 않는다 — 거기서는 ValueError 가 되어 400 invalid_task 로
        # 뭉뚱그려지고, 어느 필드가 틀렸는지 부르는 쪽이 알 수 없다.
        if status not in INITIAL_STATUSES:
            raise RequestError(400, "invalid_field", f"initial_status must be one of {'|'.join(INITIAL_STATUSES)}")
        fields["initial_status"] = status
    if "review_policy" in body:
        policy = body["review_policy"]
        if not isinstance(policy, dict):
            raise RequestError(400, "invalid_field", "review_policy must be an object")
        fields["review_policy"] = policy
    return fields


def _dispatcher_missing(api) -> bool:
    """`_check_dispatcher_presence` 가 (False, …) 를 주면 True. 프로브 실패는 fail-open(경고 없음).
    The probe is an optional Hermes internal; without it there is no warning."""
    probe = getattr(api, "_check_dispatcher_presence", None)
    if probe is None:
        return False
    try:
        present, _message = probe(api.get_hermes_home())
        return not bool(present)
    except Exception:
        return False


def _store_card_policy(api, conn, store, task_id, policy, implementer, *, replayable: bool) -> None:
    """Store a new card's policy; if that fails, delete the card rather than leave it without its policy.

    A request with an idempotency key may have returned an existing card, which is never deleted here."""
    from . import review_store

    mode, reviewer = policy
    try:
        review_store.put_policy(store, review_store.Policy(task_id, mode, implementer, reviewer, "card"))
    except Exception:
        if not replayable:
            api.delete_task(conn, task_id)
        raise RequestError(503, "approval_store_unavailable", "the card's approval policy could not be stored") from None


def create_task_handler(api):
    @guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        body = await read_json_object(request)
        fields = _parse_create_body(body)
        hooks_policy = None
        if "review_policy" in fields and not has_review_policy(api):
            if not hooks_enabled(api):
                raise RequestError(428, "review_policy_required", "Hermes approval policy support is required")
            # Upstream Hermes: create the card with the public `create_task`, then store its policy. A worker that
            # claims it in between is covered by the board default.
            hooks_policy = parse_policy(api, fields.pop("review_policy"), fields.get("assignee"))
        created_by = actor_from_request(request)

        def work():
            store = require_approval_store(api) if hooks_policy else None
            try:
                with board_conn(api, slug) as conn:
                    try:
                        task_id = api.create_task(conn, created_by=created_by, board=slug, **fields)
                    except ValueError as exc:
                        raise RequestError(400, "invalid_task", str(exc))
                    if hooks_policy:
                        _store_card_policy(api, conn, store, task_id, hooks_policy, fields.get("assignee"),
                                           replayable=bool(fields.get("idempotency_key")))
                    out = {"task": task_payload(api, conn, task_id, board=slug)}
            finally:
                if store is not None:
                    store.close()
            if _dispatcher_missing(api):
                out["warning"] = "dispatcher_missing"
            log_event("task.create", board=slug, task_id=task_id, title_len=len(fields["title"]))
            return out

        return web.json_response(await run_blocking(work), status=201)

    return handler


# ---------------------------------------------------------------------------
# PATCH — 대시보드 `update_task` 의 규칙 이식
# ---------------------------------------------------------------------------

_MISSING = object()
_PATCH_SUPPORTED = frozenset({
    "title", "body", "priority", "assignee", "status", "model_override", "provider_override", "reasoning_effort",
    "review_policy", "expected_revision",
})


def _guard_status(api, conn, task_id: str, new_status: str) -> None:
    """The policy core's mutation guard (patched Hermes only), for moves made with a public verb that does not
    run it itself."""
    if has_review_policy(api):
        with api.write_txn(conn):
            api.guard_task_mutation(conn, task_id, {"status": new_status})


def _set_status_direct(api, conn, task_id: str, new_status: str) -> bool:
    """A drag into ready/todo/triage that no structured verb covers.

    Public Hermes verbs first, as the upstream dashboard does where one exists:
    - leaving `running` → `reclaim_task`: it releases the claim, closes the run as reclaimed, stops the worker, and
      returns the card to the column the run was claimed from (`review` for a reviewer run, else `ready`);
    - `todo` → `ready` → `promote_task`, which refuses while a parent is unfinished.
    What is left (ready→todo, →triage, reopening a done/archived card) has no public verb — the upstream dashboard
    writes those directly too — so it goes through `_write_status`."""
    current = api.get_task(conn, task_id)
    if current is None:
        return False
    if current.status == "running":
        _guard_status(api, conn, task_id, new_status)
        # The `(deskrpg/direct)` marker is what DeskRPG's run history reads to label the attempt "moved".
        if not api.reclaim_task(conn, task_id, reason=f"status changed to {new_status} (deskrpg/direct)"):
            return False
        current = api.get_task(conn, task_id)
        # A reviewer run goes back to review even when the drag asked for ready — the review is not done.
        if current is None or current.status == new_status or (new_status == "ready" and current.status == "review"):
            return current is not None
    if current.status == "todo" and new_status == "ready":
        _guard_status(api, conn, task_id, new_status)
        ok, _reason = api.promote_task(conn, task_id, actor=DEFAULT_ACTOR)
        return bool(ok)
    return _write_status(api, conn, task_id, new_status)


def _terminate(api, terminations) -> None:
    """Stop the workers Hermes handed back. Hermes returns `(pid, claim_lock, started_at)`; `started_at` lets it
    refuse to signal a recycled pid."""
    for pid, claim_lock, *rest in terminations:
        api._terminate_reclaimed_worker(pid, claim_lock, **({"started_at": rest[0]} if rest else {}))


def _write_status(api, conn, task_id: str, new_status: str) -> bool:
    """Direct status write + `status` event for a card that is not running (the cases with no public verb)."""
    terminations = []
    with api.write_txn(conn):
        if has_review_policy(api):
            api.guard_task_mutation(conn, task_id, {"status": new_status})
        prev = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        # A card the dispatcher claimed in the meantime is its to move now.
        if prev is None or prev["status"] == "running":
            return False
        # 부모가 전부 끝나기 전에는 ready 로 올리지 않는다 — 디스패처가 상류가 덜 된 자식을 띄운다.
        if new_status == "ready" and api.unsatisfied_parents(conn, task_id):
            return False
        reopening_parent = prev["status"] in ("done", "archived") and new_status not in ("done", "archived")
        cur = conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, task_id))
        if cur.rowcount != 1:
            return False
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, 'status', ?, ?)",
            (task_id, json.dumps({"status": new_status, "requested_status": new_status}), int(time.time())),
        )
        if reopening_parent:
            result = api.invalidate_descendants_for_parent_reopen(conn, task_id, author=DEFAULT_ACTOR)
            terminations.extend(result["terminations"])
            if terminations and getattr(api, "_terminate_reclaimed_worker", None) is None:
                # Raising rolls the whole move back: reopening without stopping the running descendants would
                # leave workers on cards that are no longer theirs.
                raise RuntimeError("this Hermes build cannot stop the running descendants of a reopened card")
    _terminate(api, terminations)
    if new_status in ("done", "ready", "review"):
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
            return RequestError(409, "invalid_transition", f"cannot move to ready because a parent is not done — {names}")
    return RequestError(409, "invalid_transition", detail or f"cannot move to {status!r} from the current status")


def _patch_status(api, conn, task_id: str, status: str, assignee) -> None:
    if status == "running":
        raise RequestError(409, "invalid_transition", "running cannot be set directly — the dispatcher claims cards")
    try:
        ok = _apply_status(api, conn, task_id, status, assignee)
    except (RuntimeError, ValueError) as exc:
        raise _refused(api, conn, task_id, status, str(exc))
    if isinstance(ok, tuple):  # request_review(with_reason=…) 꼴 방어
        ok = ok[0]
    if not ok:
        raise _refused(api, conn, task_id, status)


def _patch_title_body(api, conn, task_id: str, title, body, board: str) -> None:
    """Hermes' public `edit_task`: one UPDATE, an `edited` event naming the fields, then the post-commit observer."""
    ok = api.edit_task(
        conn, task_id,
        title=None if title is _MISSING else title.strip(),
        body=None if body is _MISSING else body,
        board=board,
    )
    if not ok:
        raise RequestError(404, "task_not_found", f"task {task_id} not found")


def _set_priority(api, conn, task_id: str, priority: int, board: str) -> None:
    """Hermes' public `edit_task` — the same call the upstream dashboard makes (`reprioritized` event)."""
    if not api.edit_task(conn, task_id, priority=int(priority), board=board):
        raise RequestError(404, "task_not_found", f"task {task_id} not found")


def _parse_patch_body(body: dict) -> dict:
    """UpdateTaskBody. `null` 은 model_override/reasoning_effort 에서 '비움' 이고 다른 키에서는 400."""
    _reject_unknown_keys(body, UPDATE_TASK_KEYS)
    unsupported = sorted(set(body) - _PATCH_SUPPORTED)
    if unsupported:
        # 계약 키지만 PATCH 로 바꿀 길이 없는 것 — 조용히 버리면 클라이언트가 성공으로 오해한다.
        raise RequestError(400, "unsupported_field", ", ".join(unsupported))
    out = {}
    if "review_policy" in body:
        if not isinstance(body["review_policy"], dict):
            raise RequestError(400, "invalid_field", "review_policy must be an object")
        out["review_policy"] = body["review_policy"]
        out["expected_revision"] = require_int(body, "expected_revision", minimum=1)
    elif "expected_revision" in body:
        raise RequestError(400, "invalid_field", "expected_revision requires review_policy")
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
            raise RequestError(400, "invalid_field", f"status must be one of {'|'.join(CARD_STATUSES)}")
        out["status"] = status
    for key in ("model_override", "provider_override", "reasoning_effort"):
        if key in body:
            out[key] = None if body[key] is None else require_str(body, key, allow_empty=True)
    return out


def _patch_hooks_policy(api, task, raw_policy, expected_revision) -> None:
    """Change a card's policy in the approval store (upstream Hermes). The revision is always 1 there."""
    from . import review_store
    from .review_state import POLICY_REVISION

    if expected_revision is not None and expected_revision != POLICY_REVISION:
        raise RequestError(409, "invalid_transition", "the card's policy changed — reload it before editing")
    store = require_approval_store(api)
    try:
        current = review_store.get_policy(store, task.id)
        implementer = (current.implementer if current else None) or task.assignee
        mode, reviewer = parse_policy(api, raw_policy, implementer)
        review_store.put_policy(store, review_store.Policy(task.id, mode, implementer, reviewer,
                                                           current.source if current else "card"))
    finally:
        store.close()


def patch_task_handler(api):
    @guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        task_id = request.match_info["task_id"]
        p = _parse_patch_body(await read_json_object(request))

        def work():
            with board_conn(api, slug) as conn:
                task = require_task(api, conn, task_id)
                supported = has_review_policy(api)
                if "review_policy" in p and not supported:
                    if not hooks_enabled(api):
                        raise RequestError(428, "review_policy_required")
                    _patch_hooks_policy(api, task, p.pop("review_policy"), p.pop("expected_revision", None))
                review = api.get_review_state(conn, task_id) if supported else None
                if review is not None or "review_policy" in p:
                    if "status" in p and len(p) > 1:
                        raise RequestError(400, "invalid_field", "Change status separately from task fields")
                    try:
                        if "status" in p:
                            _patch_status(api, conn, task_id, p["status"], None)
                        elif p:
                            api.patch_review_task(conn, task_id,
                                fields={k: v for k, v in p.items() if k not in ("review_policy", "expected_revision")},
                                policy=p.get("review_policy"), expected_revision=p.get("expected_revision"))
                    except (ValueError, RuntimeError) as exc:
                        raise RequestError(409, "invalid_transition", str(exc))
                    return {"task": task_payload(api, conn, task_id, board=slug)}
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
                return {"task": task_payload(api, conn, task_id, board=slug)}

        return web.json_response(await run_blocking(work))

    return handler


# ---------------------------------------------------------------------------
# 삭제·댓글·링크
# ---------------------------------------------------------------------------


def delete_task_handler(api):
    """`delete_task` 뒤 삭제 장부에 한 줄 — 사건 스트림이 `task.deleted` 를 여기서 합성한다."""

    @guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        task_id = request.match_info["task_id"]

        def work():
            with board_conn(api, slug) as conn:
                task = require_task(api, conn, task_id)
                if not api.delete_task(conn, task_id):
                    raise RequestError(404, "task_not_found", task_id)
            line_no = deleted_log.append_deleted(api, slug, task_id, task.title)
            log_event("task.delete", board=slug, task_id=task_id, line_no=line_no)

        await run_blocking(work)
        return web.json_response({"ok": True})

    return handler


def add_comment_handler(api):
    @guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        task_id = request.match_info["task_id"]
        body = await read_json_object(request)
        _reject_unknown_keys(body, {"author", "body"})
        text = require_str(body, "body")
        author = (require_str(body, "author", required=False) or DEFAULT_ACTOR)[:ACTOR_MAX_CHARS]

        def work():
            with board_conn(api, slug) as conn:
                require_task(api, conn, task_id)
                comment_id = api.add_comment(conn, task_id, author=author, body=text)
                for c in api.list_comments(conn, task_id):
                    if c.id == comment_id:
                        log_event("task.comment", board=slug, task_id=task_id, comment_id=comment_id, body_len=len(text))
                        return project(asdict(c), KANBAN_COMMENT_KEYS)
            raise RuntimeError(f"the id returned by add_comment {comment_id} is not in the list")

        return web.json_response({"comment": await run_blocking(work)}, status=201)

    return handler


def link_handler(api, op: str):
    """`op` ∈ add|remove. 순환 400 `link_cycle`, 없는 카드 404. remove 는 `{ok: 실제로 지웠는지}`."""
    if op not in ("add", "remove"):
        raise ValueError(f"link_handler op must be add|remove: {op!r}")

    @guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        body = await read_json_object(request)
        _reject_unknown_keys(body, {"parent_id", "child_id"})
        parent_id = require_str(body, "parent_id")
        child_id = require_str(body, "child_id")

        def work():
            with board_conn(api, slug) as conn:
                for tid in (parent_id, child_id):
                    require_task(api, conn, tid)
                if op == "remove":
                    ok = bool(api.unlink_tasks(conn, parent_id, child_id))
                else:
                    try:
                        api.link_tasks(conn, parent_id, child_id)
                    except ValueError as exc:
                        msg = str(exc)
                        code = "link_cycle" if ("cycle" in msg or "itself" in msg) else "invalid_link"
                        raise RequestError(400, code, msg)
                    ok = True
                log_event(f"link.{op}", board=slug, parent_id=parent_id, child_id=child_id, ok=ok)
                return {"ok": ok}

        return web.json_response(await run_blocking(work))

    return handler

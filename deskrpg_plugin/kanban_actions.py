"""칸반 카드 동작 — `POST /deskrpg/kanban/tasks/{id}/<action>?board=`.

동작 하나가 핸들러 하나다(`action_handler(api, name)`). 규칙은 spec §5.4 그대로이고,
Hermes 가 전이를 거절하는 방식이 세 가지(False 반환 · RuntimeError · ValueError)라
전부 409 `invalid_transition` 으로 모은다. 상태 판단은 Hermes 에 맡기고 여기서는
**어느 함수를 어떤 순서로 부를지**만 정한다.

검토자 run 판정: Hermes 는 `review` 열에서 claim 한 run 의 `claimed` 사건 payload 에
`source_status="review"` 를 남기고, `_retry_status_for_run(conn, task_id, run_id)` 가 그것을 읽어
`"review"` 를 돌려준다(crash/timeout/reclaim 이 검토자 run 을 구현자 run 으로 둔갑시키지 않도록
Hermes 자신이 쓰는 단일 판정). 우리도 그 함수를 쓴다 — run 의 `metadata` 나 profile 로
추측하지 않는다.

specify/decompose 는 보조 LLM 을 부른다. 타임아웃은 Hermes 인자(`timeout=`)로 넘기고 밖에서
끊지 않는다 — 밖에서 끊으면 Hermes 는 계속 돌고 우리만 손을 놓는 꼴이 된다. 두 함수는
`conn` 을 받지 않고 현재 보드를 스스로 열므로 `scoped_current_board(slug)` 로 보드를 고정한다.
"""

from urllib.parse import unquote

from aiohttp import web

from .common import (
    RequestError,
    guarded,
    log_event,
    parse_board_slug,
    read_json_object,
    require_bool,
    require_str,
    run_blocking,
)
from .contract_fields import KANBAN_TASK_ACTIONS, has_review_policy
# 카드 조회(404)·KanbanTaskFull 직렬화·행위자 판정은 `kanban_common` 의 것을 쓴다 —
# 상세·생성·수정 응답과 동작 응답의 모양이 갈라지면 안 된다.
from .kanban_common import actor_from_request, open_board, require_task, task_payload

# 보조 LLM 호출의 타임아웃(초). Hermes 기본(120/180)과 같고, 운영에서 조정할 수 있게 상수로 둔다.
SPECIFY_TIMEOUT_SECONDS = 120
DECOMPOSE_TIMEOUT_SECONDS = 180

RECLAIM_REASON = "reclaimed by deskrpg"
TERMINATE_REASON = "terminated by deskrpg"


def transition_error(detail) -> RequestError:
    return RequestError(409, "invalid_transition", str(detail) if detail else None)


def _check(ok, detail="Hermes 가 전이를 거절했다"):
    """Hermes 의 bool 반환을 409 로 바꾼다."""
    if not ok:
        raise transition_error(detail)


def _active_run(api, conn, task_id: str):
    """끝나지 않은(ended_at is None) 가장 최근 run. 없으면 None."""
    runs = [r for r in api.list_runs(conn, task_id, include_active=True) if getattr(r, "ended_at", None) is None]
    return runs[-1] if runs else None


def _is_reviewer_run(api, conn, task_id: str, run) -> bool:
    return api._retry_status_for_run(conn, task_id, run.id) == "review"


# ---------------------------------------------------------------------------
# 동작별 본체 — 전부 워커 스레드 안에서 conn 을 들고 돈다. 반환은 (task, extra) 다.
# ---------------------------------------------------------------------------


def _do_reassign(api, conn, task_id, body, actor):
    profile = require_str(body, "profile")
    reclaim_first = require_bool(body, "reclaim_first", required=False, default=False)
    if not api.profile_exists(profile):
        raise RequestError(400, "profile_not_found", profile)
    ok = api.reassign_task(conn, task_id, profile, reclaim_first=reclaim_first)
    _check(ok, "카드가 실행 중이다 — reclaim_first 로 먼저 되찾아야 한다")
    return {}


def _do_reclaim(api, conn, task_id, body, actor):
    _check(api.reclaim_task(conn, task_id, reason=RECLAIM_REASON), "실행 중이 아니라 되찾을 것이 없다")
    return {}


def _do_approve(api, conn, task_id, body, actor):
    result = require_str(body, "result", required=False, allow_empty=True)
    summary = require_str(body, "summary", required=False, allow_empty=True)
    ok = api.complete_task(conn, task_id, result=result, summary=summary)
    _check(ok, "완료할 수 있는 상태(running/ready/blocked/review)가 아니거나 부모가 끝나지 않았다")
    return {}


def _do_request_changes(api, conn, task_id, body, actor):
    comment = require_str(body, "comment")
    task = require_task(api, conn, task_id)
    # (1) 댓글이 먼저다 — 전이가 거절돼도 사람이 남긴 말은 카드에 남아야 한다.
    api.add_comment(conn, task_id, actor, comment)
    run = _active_run(api, conn, task_id)
    if run is not None:
        # (2) 검토자 run 이면 Hermes 의 재작업 요청 — run 을 닫고 구현자에게 돌려보낸다.
        if _is_reviewer_run(api, conn, task_id, run):
            ok, info = api.request_changes(conn, task_id, reason=comment)
            _check(ok, info)
            return {"outcome": "changes_requested"}
        # (4) 구현자 run 이 도는 중 — 끊지 않는다. terminate 는 별도 동작이다.
        raise RequestError(409, "implementer_running", "구현자 run 이 실행 중이다 — 먼저 terminate 하거나 끝나기를 기다린다")
    # (3) run 없이 review 에 머무는 카드 — 구현자가 새 댓글을 보고 다시 돌도록 연다.
    if task.status == "review":
        _check(api.reopen_review_task(conn, task_id), "review 카드를 다시 열 수 없다")
        return {"outcome": "reopened"}
    raise transition_error(f"카드가 review 가 아니고(status={task.status}) 활성 run 도 없다")


def _do_unblock(api, conn, task_id, body, actor):
    comment = require_str(body, "comment", required=False)
    if comment:
        api.add_comment(conn, task_id, actor, comment)
    _check(api.unblock_task(conn, task_id), "blocked/scheduled 상태가 아니다")
    return {}


def _do_terminate(api, conn, task_id, body, actor):
    if _active_run(api, conn, task_id) is None:
        raise RequestError(409, "no_active_run", task_id)
    # signal_fn 을 넘기지 않는다 — 기본(os.kill)이 워커 PID 를 SIGTERM→SIGKILL 로 실제 종료한다.
    _check(api.reclaim_task(conn, task_id, reason=TERMINATE_REASON), "run 을 되찾을 수 있는 상태가 아니다")
    return {}


def _do_archive(api, conn, task_id, body, actor):
    _check(api.archive_task(conn, task_id), "이미 보관됐다")
    return {}


_SIMPLE = {
    "reassign": _do_reassign,
    "reclaim": _do_reclaim,
    "approve": _do_approve,
    "request-changes": _do_request_changes,
    "unblock": _do_unblock,
    "terminate": _do_terminate,
    "archive": _do_archive,
}


def _llm_outcome_error(outcome) -> RequestError:
    reason = str(getattr(outcome, "reason", "") or "")
    if "timeout" in reason.lower() or "timed out" in reason.lower():
        return RequestError(504, "llm_timeout", reason)
    return RequestError(409, "llm_refused", reason)


def _run_llm_action(api, slug: str, task_id: str, name: str, actor: str):
    """specify/decompose — 보드를 고정한 스레드 안에서 Hermes 를 부르고 `(task, outcome)` 을 돌려준다."""
    with open_board(api, slug) as conn:
        require_task(api, conn, task_id)
    if name == "specify":
        with api.scoped_current_board(slug):
            outcome = api.specify_task(task_id, author=actor, timeout=SPECIFY_TIMEOUT_SECONDS)
    else:
        with api.scoped_current_board(slug):
            outcome = api.decompose_task(task_id, author=actor, timeout=DECOMPOSE_TIMEOUT_SECONDS)
    if outcome is None or not getattr(outcome, "ok", False):
        raise _llm_outcome_error(outcome)
    if name == "specify":
        # SpecifyOutcome 은 new_title 만 알려 준다. Hermes `specify_triage_task` 는 제목·본문을 덮고
        # triage→todo 로 올리므로 바뀐 필드는 항상 이 셋이다.
        extra = {"outcome": {"changed_fields": list(getattr(outcome, "changed_fields", None) or ("title", "body", "status"))}}
    else:
        extra = {"outcome": {"child_ids": list(getattr(outcome, "child_ids", None) or [])}}
    with open_board(api, slug) as conn:
        return task_payload(api, conn, task_id), extra


def action_handler(api, name: str):
    """동작 이름 하나에 대한 aiohttp 핸들러. 모르는 이름은 구성 시점에 거절한다."""
    if name not in KANBAN_TASK_ACTIONS:
        raise ValueError(f"모르는 칸반 동작: {name!r}")

    @guarded
    async def handler(request):
        task_id = request.match_info["id"]
        try:
            slug = parse_board_slug(request)
            if name == "estimate":
                raise RequestError(501, "not_implemented", "estimate 는 아직 지원하지 않는다")
            actor = actor_from_request(request)
            if name in ("specify", "decompose"):
                payload, extra = await run_blocking(_run_llm_action, api, slug, task_id, name, actor)
            else:
                body = await read_json_object(request) if request.can_read_body else {}
                user_id = request.headers.get("X-DeskRPG-User-Id", "").strip()
                payload, extra = await run_blocking(_run_simple_action, api, slug, task_id, name, body, actor, user_id, request.headers.get("X-DeskRPG-User-Name"))
        except RequestError as exc:
            # 상태 코드만 남기고 `guarded` 에 넘긴다 — 응답 변환은 한 곳에서.
            log_event(f"kanban.{name}", board=slug_or_none(request), task_id=task_id, status=str(exc.status))
            raise
        log_event(f"kanban.{name}", board=slug, task_id=task_id, status="200")
        return web.json_response({"task": payload, **extra})

    return handler


def _run_simple_action(api, slug, task_id, name, body, actor, user_id="", encoded_user_name=None):
    with open_board(api, slug) as conn:
        require_task(api, conn, task_id)
        try:
            review = api.get_review_state(conn, task_id) if has_review_policy(api) else None
            if name == "approve" and review is not None:
                if set(body) - {"submission_id", "request_id"}:
                    raise RequestError(400, "invalid_field", "Only submission_id and request_id are accepted")
                if not user_id or len(user_id) > 200:
                    raise RequestError(400, "approval_actor_required", "Authenticated DeskRPG user header is required")
                metadata = {}
                if encoded_user_name is not None:
                    try:
                        actor_name = unquote(encoded_user_name, errors="strict")
                    except UnicodeDecodeError:
                        raise RequestError(400, "invalid_actor_name", "User name must be URL-encoded UTF-8") from None
                    if not actor_name.strip() or len(actor_name) > 200:
                        raise RequestError(400, "invalid_actor_name", "User name must contain 1 to 200 characters")
                    metadata["actor_name"] = actor_name.strip()
                api.approve_task(conn, task_id, actor_id=f"deskrpg:{user_id}",
                                 submission_id=require_str(body, "submission_id"),
                                 request_id=require_str(body, "request_id"), **metadata)
                extra = {}
            else:
                extra = _SIMPLE[name](api, conn, task_id, body, actor)
        except (RuntimeError, ValueError) as exc:
            # Hermes 가 전이를 예외로 거절하는 경우(실행 중 재배정 RuntimeError, HallucinatedCardsError 등).
            # 요청 오류(RequestError)는 Exception 이지만 이 둘의 하위가 아니라 그대로 지나간다.
            raise transition_error(exc)
        return task_payload(api, conn, task_id), extra


def slug_or_none(request):
    return request.query.get("board") or None

"""스웜 — Hermes `hermes_cli.kanban_swarm` 으로 가는 얇은 경로.

토폴로지·트랜잭션 경계·블랙보드 규약을 **베끼지 않는다.** 본문을 검증해
`api.create_swarm` 에 넘기고 결과를 그대로 돌려준다. 재구현하면 Hermes 가 v2 로
바뀔 때 조용히 어긋난다.
"""

from aiohttp import web

from .common import (
    RequestError,
    guarded,
    log_event,
    parse_board_slug,
    read_json_object,
    require_int,
    require_str,
    require_str_list,
    run_blocking,
)
from .kanban_common import actor_from_request, open_board


def _worker_specs(api, raw_workers):
    """본문의 워커 배열을 `SwarmWorkerSpec` 목록으로. 프로필 존재까지 여기서 본다."""
    if not isinstance(raw_workers, list) or not raw_workers:
        raise RequestError(400, "workers_required", "워커가 최소 한 명 필요하다")
    specs = []
    for index, raw in enumerate(raw_workers):
        if not isinstance(raw, dict):
            raise RequestError(400, "invalid_field", f"workers[{index}] 는 객체여야 한다")
        profile = require_str(raw, "profile")
        title = require_str(raw, "title")
        _require_profile(api, profile)
        specs.append(
            api.SwarmWorkerSpec(
                profile=profile,
                title=title,
                # body 를 안 주면 title 을 쓴다 — `parse_worker_arg` 와 같은 규칙.
                body=require_str(raw, "body", required=False, default=title),
                skills=require_str_list(raw, "skills", required=False, default=[]),
                priority=require_int(raw, "priority", required=False, default=0),
            )
        )
    return specs


def _require_profile(api, name: str) -> str:
    """없는 프로필은 400. Hermes 에 넘기면 500 으로 흘러 원인을 못 본다."""
    if not api.profile_exists(name):
        raise RequestError(400, "profile_not_found", name)
    return name


def create_swarm_handler(api):
    @guarded
    async def handler(request):
        slug = parse_board_slug(request)
        body = await read_json_object(request)
        goal = require_str(body, "goal")
        workers = _worker_specs(api, body.get("workers"))
        verifier = _require_profile(api, require_str(body, "verifier"))
        synthesizer = _require_profile(api, require_str(body, "synthesizer"))
        tenant = require_str(body, "tenant", required=False, default=None)
        priority = require_int(body, "priority", required=False, default=0)
        idempotency_key = require_str(body, "idempotency_key", required=False, default=None)
        created_by = actor_from_request(request)

        def work():
            with open_board(api, slug) as conn:
                return api.create_swarm(
                    conn,
                    goal=goal,
                    workers=workers,
                    verifier_assignee=verifier,
                    synthesizer_assignee=synthesizer,
                    tenant=tenant,
                    created_by=created_by,
                    priority=priority,
                    idempotency_key=idempotency_key,
                )

        created = await run_blocking(work)
        payload = created.as_dict()
        # 목표는 카드 본문이다 — 로그에 싣지 않는다.
        log_event(
            "kanban.swarm",
            board=slug,
            root_id=payload.get("root_id"),
            workers=len(workers),
        )
        return web.json_response(payload)

    return handler


def blackboard_handler(api):
    """카드(root_id) 존재는 여기서 확인하지 않는다 — `api.latest_blackboard` 자체가

    Hermes 의 검증·조회 규약을 따른다. 여기서 `require_task` 로 먼저 걸러 버리면
    Hermes 쪽 규칙(예: 아카이브된 루트도 블랙보드는 읽힌다)이 조용히 더 좁아진다.
    """

    @guarded
    async def handler(request):
        slug = parse_board_slug(request)
        task_id = request.match_info["id"]

        def work():
            with open_board(api, slug) as conn:
                return api.latest_blackboard(conn, task_id)

        return web.json_response({"blackboard": await run_blocking(work)})

    return handler

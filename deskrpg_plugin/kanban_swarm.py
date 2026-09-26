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
from .contract_fields import has_review_policy, has_swarm_policy_symbols
from .kanban_common import actor_from_request, open_board, require_task
from .review_state import hooks_enabled, parse_policy, require_approval_store
from .kanban_swarm_policy import SwarmPolicyConflict, create_swarm_with_policy


def _worker_specs(api, raw_workers):
    """본문의 워커 배열을 `SwarmWorkerSpec` 목록으로. 프로필 존재까지 여기서 본다."""
    if not isinstance(raw_workers, list) or not raw_workers:
        raise RequestError(400, "workers_required", "at least one worker is required")
    specs = []
    for index, raw in enumerate(raw_workers):
        if not isinstance(raw, dict):
            raise RequestError(400, "invalid_field", f"workers[{index}] must be an object")
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


def _worker_policy(api, body):
    """The workers' approval policy, or None on a Hermes that has no approval policies at all.

    Fail-closed: on a Hermes with approval policies, a swarm without policies would let its result cards finish
    unapproved, so it is refused — 428 when this build cannot attach them (`swarm_review_policy` missing), 400
    when the caller did not send one."""
    if not has_review_policy(api):
        if "review_policy" in body and not hooks_enabled(api):
            raise RequestError(428, "review_policy_required", "Hermes approval policy support is required")
        return None
    if not has_swarm_policy_symbols(api):
        raise RequestError(
            428, "swarm_review_policy_unsupported", "This Hermes build cannot create a swarm with approval policies"
        )
    policy = body.get("review_policy")
    if not isinstance(policy, dict):
        raise RequestError(400, "invalid_field", "review_policy is required for a swarm on an approval-policy board")
    return policy


def _hooks_worker_policy(api, raw, workers):
    """The workers' policy on upstream Hermes. An AI reviewer cannot be one of the workers it would review."""
    mode, reviewer = parse_policy(api, raw, None)
    if reviewer is not None and reviewer in {w.profile for w in workers}:
        raise RequestError(400, "invalid_swarm", "the reviewer cannot be one of the swarm's workers")
    return mode, reviewer


def _create_swarm_with_hooks_policy(api, conn, policy, **swarm):
    """Hermes' public `create_swarm`, then a policy per result card in the approval store.

    Workers get the requested policy; the verifier and the synthesizer are always reviewed by a person; the root is
    a structure card and gets none. Hermes makes the workers ready as it creates the swarm, so a worker may start
    before its row is written — the board default covers that window. Replaying an idempotency key rewrites the
    same rows."""
    from . import review_store

    mode, reviewer = policy
    store = require_approval_store(api)
    try:
        created = api.create_swarm(conn, **swarm)
        ids = created.as_dict()
        for task_id in ids["worker_ids"]:
            implementer = api.get_task(conn, task_id).assignee
            review_store.put_policy(store, review_store.Policy(task_id, mode, implementer, reviewer, "swarm"))
        for task_id, profile in ((ids["verifier_id"], swarm["verifier_assignee"]),
                                 (ids["synthesizer_id"], swarm["synthesizer_assignee"])):
            review_store.put_policy(store, review_store.Policy(task_id, "human", profile, None, "swarm"))
        return created
    finally:
        store.close()


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
        worker_policy = _worker_policy(api, body)
        hooks_policy = None
        if worker_policy is None and "review_policy" in body:
            hooks_policy = _hooks_worker_policy(api, body["review_policy"], workers)

        def work():
            if hooks_policy is not None:
                with open_board(api, slug) as conn:
                    return _create_swarm_with_hooks_policy(
                        api, conn, hooks_policy, goal=goal, workers=workers, verifier_assignee=verifier,
                        synthesizer_assignee=synthesizer, tenant=tenant, created_by=created_by, priority=priority,
                        idempotency_key=idempotency_key,
                    )
            with open_board(api, slug) as conn:
                if worker_policy is None:
                    # A Hermes without approval policies: nothing to bypass, Hermes builds the swarm as before.
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
                try:
                    return create_swarm_with_policy(
                        api, conn, goal=goal, workers=workers, verifier_assignee=verifier,
                        synthesizer_assignee=synthesizer, worker_policy=worker_policy, tenant=tenant,
                        created_by=created_by, priority=priority, idempotency_key=idempotency_key,
                    )
                except SwarmPolicyConflict as exc:
                    raise RequestError(409, "swarm_exists_without_policy", str(exc)) from None
                except ValueError as exc:  # ReviewPolicyError is a ValueError (reviewer = implementer, bad mode…)
                    raise RequestError(400, "invalid_swarm", str(exc)) from None

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
    @guarded
    async def handler(request):
        slug = parse_board_slug(request)
        task_id = request.match_info["id"]

        def work():
            with open_board(api, slug) as conn:
                # 모르는 id 를 빈 블랙보드로 보여주면 오타를 낸 사용자가 "그런 카드 없다" 대신
                # "블랙보드가 비었다" 로 오해한다 — fake-plugin-server.ts 계약과 같은 404 로 끊는다.
                require_task(api, conn, task_id)
                return api.latest_blackboard(conn, task_id)

        return web.json_response({"blackboard": await run_blocking(work)})

    return handler

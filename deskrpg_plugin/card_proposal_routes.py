"""`POST /deskrpg/card-proposals/{proposal_id}/resolve` — 제안을 한 번만 해소한다.
`…/unresolve` — 카드가 기록되지 않은 해소를 되돌린다(카드 생성이 실패했을 때의 롤백).
`…/task` — 만들어진 카드 id 를 사후에 적는다. 그 순간부터 `unresolve` 가 그 제안을 막는다.

소유자 키다(칸반과 같은 스코프) — 해소는 프로필이 아니라 DeskRPG 사용자가 하는 일이고,
제안 저장소도 호스트 공유 자원(게이트웨이당 하나)이기 때문이다.

`store.resolve` 는 없는 id 와 이미 해소된 id 를 똑같이 `False` 로 돌려준다. 그 둘은 호출자에게
뜻이 다르므로(하나는 잘못된 요청, 하나는 경합에서 진 것) `get` 으로 먼저 갈라 404 를 낸다.
`get` 과 `resolve` 사이에 남는 창은 경합이 아니다 — 그 사이에 해소되면 `resolve` 가 `False` 를
내고 409 가 된다. **한 번만 해소되는 보장은 언제나 UPDATE 의 rowcount 다.**

sqlite 는 블로킹이므로 `run_blocking` 안에서 부른다.
"""

from aiohttp import web

from . import card_proposal_store as store
from .common import RequestError, guarded, log_event, read_json_object, require_str, run_blocking

CHOICES = ("card", "inline")


def resolve_handler(api):
    @guarded
    async def handler(request):
        proposal_id = request.match_info["proposal_id"]
        body = await read_json_object(request)
        choice = require_str(body, "choice")
        if choice not in CHOICES:
            raise RequestError(400, "invalid_field", f"choice must be one of {', '.join(CHOICES)}")
        task_id = require_str(body, "task_id", required=False, default=None)

        def work():
            if store.get(api, proposal_id) is None:
                raise RequestError(404, "card_proposal_not_found", proposal_id)
            if not store.resolve(api, proposal_id, choice, task_id):
                raise RequestError(409, "card_proposal_already_resolved", proposal_id)
            return {"resolved": True}

        result = await run_blocking(work)
        log_event("card_proposal.resolved", proposal_id=proposal_id, choice=choice)
        return web.json_response(result)

    return handler


def unresolve_handler(api):
    """해소를 되돌린다. 본문 없음.

    `resolved_task_id` 가 있는 제안은 되돌리지 않는다 — 카드가 이미 있는 제안을 다시 열면 카드가 둘 생긴다.
    되돌릴 수 없는 두 경우(애초에 해소되지 않음 / 카드가 기록됨)를 코드로 가르지 않는다: 어느 쪽이든
    호출자가 할 일은 같고(이 제안은 되돌릴 수 없다), 가르려면 트랜잭션 밖에서 상태를 한 번 더 읽어
    그 사이에 바뀔 수 있는 값을 근거로 오류를 정하게 된다. 판정은 rowcount 하나에만 둔다.
    """

    @guarded
    async def handler(request):
        proposal_id = request.match_info["proposal_id"]

        def work():
            if store.get(api, proposal_id) is None:
                raise RequestError(404, "card_proposal_not_found", proposal_id)
            if not store.unresolve(api, proposal_id):
                raise RequestError(409, "card_proposal_not_unresolvable",
                                   "not resolved, or a card is already recorded")
            return {"resolved": False}

        result = await run_blocking(work)
        log_event("card_proposal.unresolved", proposal_id=proposal_id)
        return web.json_response(result)

    return handler


def record_task_handler(api):
    """만들어진 카드 id 를 해소된 제안에 적는다.

    이미 카드가 적힌 제안은 덮지 않는다 — 덮을 수 있으면 두 번째 카드가 첫 카드를 가려 이중 방어가 무너진다.
    `choice="inline"` 으로 해소된 제안도 받지 않는다(만들어진 카드가 없다).
    409 를 두 경우(미해소 / 이미 적힘)로 가르지 않는 이유는 `unresolve_handler` 와 같다.
    """

    @guarded
    async def handler(request):
        proposal_id = request.match_info["proposal_id"]
        body = await read_json_object(request)
        task_id = require_str(body, "task_id")

        def work():
            if store.get(api, proposal_id) is None:
                raise RequestError(404, "card_proposal_not_found", proposal_id)
            if not store.record_task(api, proposal_id, task_id):
                raise RequestError(409, "card_proposal_task_not_recordable",
                                   "not resolved, not resolved as a card, or a card is already recorded")
            return {"recorded": True}

        result = await run_blocking(work)
        log_event("card_proposal.task_recorded", proposal_id=proposal_id, task_id=task_id)
        return web.json_response(result)

    return handler

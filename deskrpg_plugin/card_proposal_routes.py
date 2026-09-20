"""`POST /deskrpg/card-proposals/{proposal_id}/resolve` — 제안을 한 번만 해소한다.

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
            raise RequestError(400, "invalid_field", f"choice 는 {', '.join(CHOICES)} 중 하나여야 한다")
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

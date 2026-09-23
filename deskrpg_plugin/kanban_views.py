"""칸반 뷰용 묶음 조회 — 링크 목록과 실행 기록(spec §5.6).

보드 응답은 카드마다 `link_counts` 와 시작·완료 시각만 준다. 하위 트리를 펼치거나 "누가 언제
일했는가" 를 그리려면 카드 수만큼 상세를 불러야 했다 — 카드가 늘면 조용히 무너지는 모양이다.
여기 두 라우트가 그 두 가지를 **한 번에** 준다.

쓰는 쪽은 DeskRPG 의 프로젝트 뷰(목록 트리·실적 타임라인)다. 둘 다 읽기 전용이고 Hermes 의
`task_links`·`task_runs` 를 그대로 옮긴다 — 판단은 하지 않는다.
"""

import time

from aiohttp import web

from .common import (
    RequestError,
    board_conn,
    guarded,
    parse_board_slug,
    run_blocking,
)
from .contract_fields import KANBAN_TIMELINE_RUN_KEYS
from .kanban_common import project

# 한 번에 돌려주는 실행 기록의 상한. 넘으면 **최근 것부터** 남기고 `truncated: true` 를 붙인다.
# 조용히 자르면 화면이 "그 시간대에 아무도 일하지 않았다" 로 읽는다.
RUNS_LIMIT_DEFAULT = 1000
RUNS_LIMIT_MAX = 5000

# `from`·`to` 를 안 주면 보는 창. 타임라인의 기본 화면이 "최근" 이라서다.
RUNS_WINDOW_DEFAULT_SECONDS = 7 * 24 * 3600


def _require_board(api, slug: str) -> str:
    if not api.board_exists(board=slug):
        raise RequestError(404, "board_not_found", slug)
    return slug


def _board_from_query(api, request) -> str:
    return _require_board(api, parse_board_slug(request))


def _query_int(request, key: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = request.query.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RequestError(400, "invalid_query", f"{key} must be an integer: {raw!r}")
    if minimum is not None and value < minimum:
        raise RequestError(400, "invalid_query", f"{key} must be {minimum} or greater: {value}")
    if maximum is not None and value > maximum:
        value = maximum
    return value


def links_handler(api):
    """`GET /deskrpg/kanban/links?board=` — 이 보드의 부모·자식 쌍 전부.

    한 카드의 링크만 필요하면 카드 상세에 이미 실려 있다. 이 라우트는 **트리를 한 번에** 그릴
    때 쓴다. 쌍만 주고 카드 본문은 주지 않는다 — 보드 응답이 카드의 정본이고, 여기서 사본을
    같이 내면 재조회 뒤 낡은 제목이 남는다.
    """

    @guarded
    async def handler(request):
        slug = _board_from_query(api, request)

        def work():
            with board_conn(api, slug) as conn:
                rows = conn.execute(
                    "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
                ).fetchall()
            return {
                "links": [{"parent_id": r["parent_id"], "child_id": r["child_id"]} for r in rows],
                "board": slug,
            }

        return web.json_response(await run_blocking(work))

    return handler


def runs_handler(api):
    """`GET /deskrpg/kanban/runs?board=&from=&to=&limit=` — 창 안의 실행 기록.

    `from`·`to` 는 epoch 초(칸반 시각 계약 그대로). 생략하면 최근 7일이다.

    **창에 걸치는 실행을 잘라내지 않는다.** 창 전에 시작해 창 안에서 끝난 일도, 창 안에서
    시작해 아직 안 끝난 일도 그 시간대의 사실이다. 겹치기만 하면 싣는다.

    카드의 `tenant`·`title` 과 보드 슬러그를 같이 싣는다. 타임라인이 서브프로젝트로 거를 때
    카드 목록과 다시 조인하지 않아도 되고, 보드가 여러 개일 때 "어느 프로젝트의 실적인가" 가
    응답만 보고 가려진다.
    """

    @guarded
    async def handler(request):
        slug = _board_from_query(api, request)
        now = int(time.time())
        to_ts = _query_int(request, "to", now)
        from_ts = _query_int(request, "from", to_ts - RUNS_WINDOW_DEFAULT_SECONDS)
        if from_ts > to_ts:
            raise RequestError(400, "invalid_query", f"from is after to: {from_ts} > {to_ts}")
        limit = _query_int(request, "limit", RUNS_LIMIT_DEFAULT, minimum=1, maximum=RUNS_LIMIT_MAX)

        def work():
            with board_conn(api, slug) as conn:
                # 겹침 판정: 시작이 창 끝보다 앞이고, (끝이 없거나) 끝이 창 시작보다 뒤.
                rows = conn.execute(
                    """
                    SELECT r.*, t.tenant AS tenant, t.title AS task_title
                      FROM task_runs r
                      LEFT JOIN tasks t ON t.id = r.task_id
                     WHERE r.started_at <= ?
                       AND (r.ended_at IS NULL OR r.ended_at >= ?)
                     ORDER BY r.started_at DESC, r.id DESC
                     LIMIT ?
                    """,
                    (to_ts, from_ts, limit + 1),
                ).fetchall()
            truncated = len(rows) > limit
            kept = rows[:limit]
            runs = []
            for r in kept:
                d = dict(r)
                d["board"] = slug
                runs.append(project(d, KANBAN_TIMELINE_RUN_KEYS))
            # 질의는 최신순으로 잘랐고(상한에 걸리면 최근 것을 남긴다), 응답은 시간순으로 낸다.
            runs.reverse()
            return {
                "runs": runs,
                "board": slug,
                "window": {"from": from_ts, "to": to_ts},
                "truncated": truncated,
            }

        return web.json_response(await run_blocking(work))

    return handler

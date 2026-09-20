"""묶음 조회 — 링크 목록과 타임라인용 실행 기록(§5.6)."""

import pytest

from deskrpg_plugin import contract_fields as cf
from deskrpg_plugin.kanban_views import RUNS_LIMIT_MAX, RUNS_WINDOW_DEFAULT_SECONDS
from tests.kanban_app import make_app

B = "?board=default"


def _client(aiohttp_client, fake_api):
    return aiohttp_client(make_app(fake_api))


def _conn(fake_api):
    return fake_api.kanban.connect(board="default")


def _run(fake_api, conn, task_id, *, started, ended=None, outcome=None, profile=None):
    """`start_run` 은 지금 시각을 쓰므로, 창 경계를 시험하려면 시각을 직접 넣는다."""
    run_id = fake_api.kanban.start_run(conn, task_id, profile=profile)
    run = fake_api.kanban._state(conn).runs[run_id]
    run.started_at = started
    run.ended_at = ended
    run.outcome = outcome
    if ended is not None:
        run.status = "done"
    return run_id


# ---------------------------------------------------------------------------
# 링크 목록
# ---------------------------------------------------------------------------


async def test_링크_목록은_보드의_모든_부모_자식_쌍을_한_번에_준다(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    parent = db.create_task(conn, title="p")
    child_a = db.create_task(conn, title="a", parents=[parent])
    child_b = db.create_task(conn, title="b", parents=[parent])

    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get(f"/deskrpg/kanban/links{B}")).json()

    assert body["board"] == "default"
    pairs = {(l["parent_id"], l["child_id"]) for l in body["links"]}
    assert pairs == {(parent, child_a), (parent, child_b)}


async def test_링크_목록은_카드_본문을_싣지_않는다(aiohttp_client, fake_api):
    # 카드의 정본은 보드 응답이다. 여기서 제목 사본을 같이 내면 재조회 뒤 낡은 값이 남는다.
    db = fake_api.kanban
    conn = _conn(fake_api)
    parent = db.create_task(conn, title="제목이 여기 실리면 안 된다")
    db.create_task(conn, title="c", parents=[parent])

    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get(f"/deskrpg/kanban/links{B}")).json()

    assert set(body["links"][0]) == {"parent_id", "child_id"}


async def test_링크가_없으면_빈_배열이다(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get(f"/deskrpg/kanban/links{B}")).json()
    assert body["links"] == []


async def test_없는_보드는_404(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    for path in ("links", "runs"):
        resp = await client.get(f"/deskrpg/kanban/{path}?board=ghost")
        assert resp.status == 404
        assert (await resp.json())["error"] == "board_not_found"


# ---------------------------------------------------------------------------
# 실행 기록
# ---------------------------------------------------------------------------


async def test_실행_기록의_모양이_계약과_같고_카드_맥락을_싣는다(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    task = db.create_task(conn, title="랜딩 카피", tenant="web", assignee="sophie")
    _run(fake_api, conn, task, started=1_000, ended=1_060, outcome="completed", profile="sophie")

    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get(f"/deskrpg/kanban/runs{B}&from=0&to=2000")).json()

    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert set(run) <= cf.KANBAN_TIMELINE_RUN_KEYS
    assert cf.KANBAN_TIMELINE_RUN_REQUIRED <= set(run)
    # 타임라인이 서브프로젝트로 거를 때 카드 목록과 다시 조인하지 않아도 된다.
    assert run["task_id"] == task
    assert run["tenant"] == "web"
    assert run["task_title"] == "랜딩 카피"
    assert run["board"] == "default"
    assert run["outcome"] == "completed"
    assert body["window"] == {"from": 0, "to": 2000}
    assert body["truncated"] is False


async def test_창에_걸치기만_해도_싣는다(aiohttp_client, fake_api):
    """창 전에 시작했거나 창 안에서 시작해 아직 안 끝난 일도 그 시간대의 사실이다."""
    db = fake_api.kanban
    conn = _conn(fake_api)
    t = db.create_task(conn, title="t")
    before_into = _run(fake_api, conn, t, started=500, ended=1_500)  # 창 전 시작, 창 안 종료
    inside = _run(fake_api, conn, t, started=1_100, ended=1_200)
    still_running = _run(fake_api, conn, t, started=1_400, ended=None)
    spans_whole = _run(fake_api, conn, t, started=100, ended=9_999)
    long_before = _run(fake_api, conn, t, started=10, ended=20)
    long_after = _run(fake_api, conn, t, started=8_000, ended=8_100)

    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get(f"/deskrpg/kanban/runs{B}&from=1000&to=2000")).json()

    ids = {r["id"] for r in body["runs"]}
    assert before_into in ids
    assert inside in ids
    assert still_running in ids
    assert spans_whole in ids
    assert long_before not in ids
    assert long_after not in ids


async def test_응답은_시간순이다(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = _conn(fake_api)
    t = db.create_task(conn, title="t")
    late = _run(fake_api, conn, t, started=1_800, ended=1_900)
    early = _run(fake_api, conn, t, started=1_100, ended=1_200)

    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get(f"/deskrpg/kanban/runs{B}&from=1000&to=2000")).json()

    assert [r["id"] for r in body["runs"]] == [early, late]


async def test_상한을_넘으면_최근_것을_남기고_잘렸다고_말한다(aiohttp_client, fake_api):
    # 조용히 자르면 화면이 "그 시간대에 아무도 일하지 않았다" 로 읽는다.
    db = fake_api.kanban
    conn = _conn(fake_api)
    t = db.create_task(conn, title="t")
    ids = [_run(fake_api, conn, t, started=1_000 + i, ended=1_000 + i) for i in range(5)]

    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get(f"/deskrpg/kanban/runs{B}&from=0&to=9999&limit=2")).json()

    assert body["truncated"] is True
    assert [r["id"] for r in body["runs"]] == ids[-2:]


async def test_창을_안_주면_최근_기본_기간을_본다(aiohttp_client, fake_api):
    import time

    db = fake_api.kanban
    conn = _conn(fake_api)
    t = db.create_task(conn, title="t")
    now = int(time.time())
    recent = _run(fake_api, conn, t, started=now - 60, ended=now - 30)
    ancient = _run(fake_api, conn, t, started=now - RUNS_WINDOW_DEFAULT_SECONDS - 3600,
                   ended=now - RUNS_WINDOW_DEFAULT_SECONDS - 3000)

    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get(f"/deskrpg/kanban/runs{B}")).json()

    ids = {r["id"] for r in body["runs"]}
    assert recent in ids
    assert ancient not in ids
    assert body["window"]["to"] - body["window"]["from"] == RUNS_WINDOW_DEFAULT_SECONDS


async def test_limit_은_상한을_넘지_못한다(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/runs{B}&limit={RUNS_LIMIT_MAX + 1000}")
    assert resp.status == 200  # 거절하지 않고 상한으로 깎는다


@pytest.mark.parametrize(
    "query",
    ["&from=abc", "&to=abc", "&limit=abc", "&limit=0", "&from=2000&to=1000"],
)
async def test_잘못된_쿼리는_400_invalid_query(aiohttp_client, fake_api, query):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get(f"/deskrpg/kanban/runs{B}{query}")
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_query"


async def test_카드가_지워진_실행도_사라지지_않는다(aiohttp_client, fake_api):
    """LEFT JOIN 이라 카드가 없어도 실행 기록은 남는다 — 일한 사실이 없어지지는 않는다."""
    db = fake_api.kanban
    conn = _conn(fake_api)
    t = db.create_task(conn, title="곧 지울 카드")
    run_id = _run(fake_api, conn, t, started=1_100, ended=1_200)
    del db._state(conn).tasks[t]

    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get(f"/deskrpg/kanban/runs{B}&from=1000&to=2000")).json()

    assert [r["id"] for r in body["runs"]] == [run_id]
    assert body["runs"][0].get("tenant") is None

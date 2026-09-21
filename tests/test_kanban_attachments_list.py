"""`GET /deskrpg/kanban/attachments?board=&limit=&cursor=` — 보드 전체의 카드 첨부, 최신순.

결과물 갤러리가 쓴다. 칸반 워커의 `scratch` 워크스페이스는 카드가 끝나면 지워지므로 워커가 만든 파일 중
남는 것은 첨부뿐이다 — 끝난 카드·보관한 카드의 첨부도 빼지 않는다. 카드마다 목록을 부르면 갤러리가
카드 수만큼 요청을 보내야 해서, 한 번에 주고 카드 id·제목을 함께 싣는다.
"""
import pytest
from aiohttp import web

from deskrpg_plugin import kanban_files
from deskrpg_plugin.auth import Scope, require_auth
from deskrpg_plugin.kanban_common import attachment_payload
from tests.conftest import FakeAdapter
from tests.fakes_kanban_actions import install_fake_kanban_actions

BOARD = "deskrpg-abc"
OTHER = "deskrpg-other"
URL = "/deskrpg/kanban/attachments"


@pytest.fixture
def kanban(fake_api, tmp_path):
    db = install_fake_kanban_actions(fake_api, tmp_path / "kanban")
    db.create_board(BOARD, name="DeskRPG")
    db.create_board(OTHER, name="다른 보드")
    return db


def _client(aiohttp_client, fake_api):
    app = web.Application()
    wrap = lambda h: require_auth(FakeAdapter(authorized=True), Scope.DEFAULT, h)  # noqa: E731
    app.router.add_route("GET", URL, wrap(kanban_files.list_board_attachments_handler(fake_api)))
    return aiohttp_client(app)


def _task(kanban, board=BOARD, **kw):
    kw.setdefault("title", "카드")
    conn = kanban.connect(board=board)
    return kanban.get_task(conn, kanban.create_task(conn, **kw))


def _attach(kanban, task, filename, created_at, board=BOARD):
    att = kanban.add_attachment(kanban.connect(board=board), task.id, filename=filename, size=10,
                                content_type="text/markdown")
    att.created_at = created_at
    return att


async def _get(client, query):
    resp = await client.get(f"{URL}?{query}")
    return resp.status, await resp.json()


async def test_보드의_첨부를_최신순으로_카드_id_와_제목과_함께_준다(aiohttp_client, fake_api, kanban):
    a = _task(kanban, title="기획 보고서")
    b = _task(kanban, title="퀵스타트")
    old = _attach(kanban, a, "report.md", 100)
    new = _attach(kanban, b, "quick.md", 200)
    client = await _client(aiohttp_client, fake_api)

    status, body = await _get(client, f"board={BOARD}")

    assert status == 200
    assert body["next_cursor"] is None
    assert [x["id"] for x in body["attachments"]] == [new.id, old.id]
    first = body["attachments"][0]
    # 카드별 목록·상세와 같은 모양의 **상위집합**이다 — 두 키만 더한다.
    assert first == {**attachment_payload(new), "task_id": b.id, "task_title": "퀵스타트"}


async def test_같은_시각이면_id_내림차순이라_순서가_안정적이다(aiohttp_client, fake_api, kanban):
    t = _task(kanban)
    first = _attach(kanban, t, "a.md", 100)
    second = _attach(kanban, t, "b.md", 100)
    client = await _client(aiohttp_client, fake_api)

    _status, body = await _get(client, f"board={BOARD}")

    assert [x["id"] for x in body["attachments"]] == [second.id, first.id]


async def test_보관한_카드와_끝난_카드의_첨부도_준다(aiohttp_client, fake_api, kanban):
    # scratch 가 지워진 뒤 남는 것이 첨부뿐이다 — 빼면 이 라우트의 의미가 없다.
    t = _task(kanban)
    att = _attach(kanban, t, "done.md", 100)
    kanban.archive_task(kanban.connect(board=BOARD), t.id)
    client = await _client(aiohttp_client, fake_api)

    _status, body = await _get(client, f"board={BOARD}")

    assert [x["id"] for x in body["attachments"]] == [att.id]


async def test_다른_보드의_첨부는_섞이지_않는다(aiohttp_client, fake_api, kanban):
    _attach(kanban, _task(kanban, board=OTHER), "other.md", 300, board=OTHER)
    mine = _attach(kanban, _task(kanban), "mine.md", 100)
    client = await _client(aiohttp_client, fake_api)

    _status, body = await _get(client, f"board={BOARD}")

    assert [x["id"] for x in body["attachments"]] == [mine.id]


async def test_첨부가_없으면_빈_목록이다(aiohttp_client, fake_api, kanban):
    _task(kanban)
    client = await _client(aiohttp_client, fake_api)
    assert await _get(client, f"board={BOARD}") == (200, {"attachments": [], "next_cursor": None})


async def test_커서로_두_쪽을_이어_받으면_빠짐도_겹침도_없다(aiohttp_client, fake_api, kanban):
    t = _task(kanban)
    made = [_attach(kanban, t, f"{i}.md", 100 + i // 2) for i in range(5)]  # 같은 시각이 섞인다
    client = await _client(aiohttp_client, fake_api)

    _s, page1 = await _get(client, f"board={BOARD}&limit=3")
    assert len(page1["attachments"]) == 3 and page1["next_cursor"]
    _s, page2 = await _get(client, f"board={BOARD}&limit=3&cursor={page1['next_cursor']}")

    got = [x["id"] for x in page1["attachments"] + page2["attachments"]]
    expected = [a.id for a in sorted(made, key=lambda a: (a.created_at, a.id), reverse=True)]
    assert got == expected
    assert page2["next_cursor"] is None


async def test_limit_는_200_을_넘으면_잘린다(aiohttp_client, fake_api, kanban):
    client = await _client(aiohttp_client, fake_api)
    status, _body = await _get(client, f"board={BOARD}&limit=999")
    assert status == 200


@pytest.mark.parametrize("query,code", [
    ("", "board_required"),
    (f"board={BOARD}&limit=0", "invalid_query"),
    (f"board={BOARD}&limit=abc", "invalid_query"),
    (f"board={BOARD}&cursor=garbage", "unknown_cursor"),
])
async def test_잘못된_요청은_400(aiohttp_client, fake_api, kanban, query, code):
    client = await _client(aiohttp_client, fake_api)
    status, body = await _get(client, query)
    assert status == 400
    assert body["error"] == code


async def test_다른_보드의_커서는_거절한다(aiohttp_client, fake_api, kanban):
    t = _task(kanban, board=OTHER)
    for i in range(3):
        _attach(kanban, t, f"{i}.md", 100 + i, board=OTHER)
    client = await _client(aiohttp_client, fake_api)
    _s, page = await _get(client, f"board={OTHER}&limit=1")

    status, body = await _get(client, f"board={BOARD}&cursor={page['next_cursor']}")

    assert status == 400 and body["error"] == "unknown_cursor"


async def test_없는_보드는_404(aiohttp_client, fake_api, kanban):
    client = await _client(aiohttp_client, fake_api)
    status, body = await _get(client, "board=deskrpg-ghost")
    assert status == 404 and body["error"] == "board_not_found"

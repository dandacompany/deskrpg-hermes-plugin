"""보드 목록·생성·수정과 보드 보기(§5.1, §5.2)."""

import pytest

from deskrpg_plugin import contract_fields as cf
from tests.kanban_app import make_app


def _client(aiohttp_client, fake_api):
    return aiohttp_client(make_app(fake_api))


# ---------------------------------------------------------------------------
# 보드 목록·생성·수정
# ---------------------------------------------------------------------------


async def test_보드_목록은_default_를_포함하고_현재_보드를_알려준다(aiohttp_client, fake_api):
    fake_api.create_board("deskrpg-aaaa", name="사무실 A")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/deskrpg/kanban/boards")
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == cf.ENVELOPES["boards"]
    assert body["current"] == "default"
    slugs = [b["slug"] for b in body["boards"]]
    assert slugs == ["default", "deskrpg-aaaa"]
    for board in body["boards"]:
        assert set(board) <= cf.BOARD_META_KEYS | {"counts"}
        assert "counts" in board and "total" in board
    by_slug = {b["slug"]: b for b in body["boards"]}
    assert by_slug["default"]["is_current"] is True
    assert by_slug["deskrpg-aaaa"]["is_current"] is False


async def test_보드_목록의_counts_는_상태별_개수이고_total_은_보관_제외다(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = db.connect(board="default")
    t1 = db.create_task(conn, title="a")
    t2 = db.create_task(conn, title="b")
    db.archive_task(conn, t2)
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/deskrpg/kanban/boards")).json()
    default = body["boards"][0]
    assert default["total"] == 1
    assert default["counts"] == {"ready": 1, "archived": 1}
    assert t1  # 사용됨을 명시


async def test_보드_생성은_201_이고_다시_만들면_200_에_기존_이름을_유지한다(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/kanban/boards", json={"slug": "deskrpg-abc", "name": "첫 이름"})
    assert resp.status == 201
    body = await resp.json()
    assert set(body) == cf.ENVELOPES["board"]
    assert body["board"]["slug"] == "deskrpg-abc"
    assert body["board"]["name"] == "첫 이름"
    assert set(body["board"]) <= cf.BOARD_META_KEYS

    again = await client.post("/deskrpg/kanban/boards", json={"slug": "deskrpg-abc", "name": "다른 이름"})
    assert again.status == 200
    assert (await again.json())["board"]["name"] == "첫 이름"
    # 새 보드를 현재 보드로 바꾸지 않는다.
    assert fake_api.get_current_board() == "default"


@pytest.mark.parametrize("slug", ["", "Upper", "has space", "-lead", "a" * 65, "ünï"])
async def test_보드_생성은_슬러그_형식을_검사한다(aiohttp_client, fake_api, slug):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/kanban/boards", json={"slug": slug, "name": "x"})
    assert resp.status == 400
    assert (await resp.json())["error"] in ("invalid_board", "missing_field", "invalid_field")


async def test_보드_생성의_default_workdir_는_존재하는_절대경로만_받는다(aiohttp_client, fake_api, tmp_path):
    client = await _client(aiohttp_client, fake_api)
    for bad in ["relative/dir", str(tmp_path / "nope")]:
        resp = await client.post(
            "/deskrpg/kanban/boards", json={"slug": "deskrpg-wd", "name": "x", "default_workdir": bad}
        )
        assert resp.status == 400, bad
        assert (await resp.json())["error"] == "invalid_workdir"
    good = tmp_path / "work"
    good.mkdir()
    resp = await client.post(
        "/deskrpg/kanban/boards", json={"slug": "deskrpg-wd", "name": "x", "default_workdir": str(good)}
    )
    assert resp.status == 201
    assert (await resp.json())["board"]["default_workdir"] == str(good)


async def test_보드_수정은_이름_설명_작업폴더를_바꾸고_빈_문자열은_비운다(aiohttp_client, fake_api, tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    fake_api.create_board("deskrpg-p", name="old", description="d", default_workdir=str(work))
    client = await _client(aiohttp_client, fake_api)
    resp = await client.patch("/deskrpg/kanban/boards/deskrpg-p", json={"name": "new", "description": "", "default_workdir": ""})
    assert resp.status == 200
    board = (await resp.json())["board"]
    assert board["name"] == "new"
    assert board["description"] == ""
    assert board["default_workdir"] is None


async def test_보드_수정은_없는_보드에_404_알_수_없는_키에_400(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.patch("/deskrpg/kanban/boards/deskrpg-none", json={"name": "x"})
    assert resp.status == 404
    assert (await resp.json())["error"] == "board_not_found"
    resp = await client.patch("/deskrpg/kanban/boards/default", json={"icon": "x"})
    assert resp.status == 400
    assert (await resp.json())["error"] == "unknown_field"


# ---------------------------------------------------------------------------
# 보드 보기
# ---------------------------------------------------------------------------


async def test_보드_보기는_board_없으면_400_모르는_보드면_404(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/deskrpg/kanban/board")
    assert resp.status == 400
    assert (await resp.json())["error"] == "board_required"
    resp = await client.get("/deskrpg/kanban/board?board=Bad!")
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_board"
    resp = await client.get("/deskrpg/kanban/board?board=deskrpg-none")
    assert resp.status == 404
    assert (await resp.json())["error"] == "board_not_found"


async def test_보드_보기_열_순서와_보관_토글(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = db.connect(board="default")
    t = db.create_task(conn, title="a")
    db.archive_task(conn, t)
    client = await _client(aiohttp_client, fake_api)

    body = await (await client.get("/deskrpg/kanban/board?board=default")).json()
    assert set(body) == cf.KANBAN_BOARD_KEYS
    assert [c["name"] for c in body["columns"]] == list(cf.BOARD_COLUMNS)
    assert all(set(c) == cf.KANBAN_COLUMN_KEYS for c in body["columns"])
    assert sum(len(c["tasks"]) for c in body["columns"]) == 0

    body = await (await client.get("/deskrpg/kanban/board?board=default&include_archived=true")).json()
    assert [c["name"] for c in body["columns"]] == list(cf.BOARD_COLUMNS) + ["archived"]
    assert [x["id"] for x in body["columns"][-1]["tasks"]] == [t]


async def test_보드_보기는_미지의_상태를_todo_열에_넣는다(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = db.connect(board="default")
    t = db.create_task(conn, title="weird")
    db.get_task(conn, t).status = "mystery"
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/deskrpg/kanban/board?board=default")).json()
    cols = {c["name"]: c["tasks"] for c in body["columns"]}
    assert [x["id"] for x in cols["todo"]] == [t]


async def test_보드_보기_롤업과_카드_요약_키(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = db.connect(board="default")
    parent = db.create_task(conn, title="parent", assignee="sophie", tenant="acme")
    c1 = db.create_task(conn, title="c1", parents=[parent])
    c2 = db.create_task(conn, title="c2", parents=[parent])
    db.add_comment(conn, parent, "deskrpg", "hi")
    db.add_comment(conn, parent, "deskrpg", "again")
    db.get_task(conn, c1).status = "done"
    db.start_run(conn, c2, profile="mia", summary="절반 했다")

    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/deskrpg/kanban/board?board=default")).json()
    tasks = {x["id"]: x for c in body["columns"] for x in c["tasks"]}
    for x in tasks.values():
        assert cf.KANBAN_TASK_REQUIRED <= set(x) <= cf.KANBAN_TASK_KEYS, x.keys()

    p = tasks[parent]
    assert p["comment_count"] == 2
    assert p["link_counts"] == {"parents": 0, "children": 2}
    assert p["progress"] == {"done": 1, "total": 2}
    assert p["warnings"] is None
    assert tasks[c1]["link_counts"] == {"parents": 1, "children": 0}
    assert tasks[c1]["progress"] is None
    assert tasks[c1]["comment_count"] == 0
    assert tasks[c2]["latest_summary"] == "절반 했다"
    assert tasks[c2]["worker_pid"] is not None

    assert body["tenants"] == ["acme"]
    assert body["assignees"] == ["mia", "sophie"]
    assert body["latest_event_id"] == max(e.id for e in db.boards["default"].events)
    assert isinstance(body["now"], int)


async def test_보드_보기_경고_요약은_진단에서_계산한다(aiohttp_client, fake_api):
    db = fake_api.kanban
    conn = db.connect(board="default")
    t = db.create_task(conn, title="sick")
    seen = {}

    class _Diag:
        def __init__(self, severity):
            self.severity = severity

        def to_dict(self):
            return {
                "kind": "stale", "severity": self.severity, "title": "t", "detail": "d",
                "actions": [], "count": 2, "last_seen_at": 5, "data": {},
            }

    def compute(task, events, runs, *, config=None, graph=None, **_):
        seen["config"] = config
        seen["graph"] = graph
        return [_Diag("warning"), _Diag("error")] if task["id"] == t else []

    fake_api.compute_task_diagnostics = compute
    fake_api.config_from_runtime_config = lambda raw: {"from": raw.get("timezone")}
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/deskrpg/kanban/board?board=default")).json()
    card = [x for c in body["columns"] for x in c["tasks"] if x["id"] == t][0]
    assert card["warnings"] == {"count": 4, "highest_severity": "error"}
    assert seen["config"] == {"from": "Asia/Seoul"}  # load_config() 를 거쳐 왔다
    assert seen["graph"] == {"parents": [], "children": []}


# ---------------------------------------------------------------------------
# 예상 못 한 예외 → 500 internal_error (타입 이름만)
# ---------------------------------------------------------------------------


async def test_Hermes_가_예상_못_한_예외를_던지면_500_internal_error_JSON(aiohttp_client, fake_api):
    def boom(**kw):
        raise RuntimeError("secret /path/to/board.db")

    fake_api.list_boards = boom
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/deskrpg/kanban/boards")
    assert resp.status == 500
    body = await resp.json()
    assert body == {"error": "internal_error", "detail": "RuntimeError"}
    assert "secret" not in await resp.text()

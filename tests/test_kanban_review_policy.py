"""혼합 승인 계약: 미지원 코어를 광고하지 않고 승인 주체를 본문에서 받지 않는다."""
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from deskrpg_plugin import contract_fields as cf
from tests.fakes_kanban_actions import install_fake_kanban_actions
from tests.kanban_app import make_app
from tests.test_kanban_actions import _client, _post, _task

BOARD = "deskrpg-abc"
POLICY = {"version": 1, "mode": "human", "reviewer_profile": None}


def enable(api):
    api.API_VERSION = 1
    api.get_review_state = Mock(return_value={"policy": POLICY, "submission": {"id": "s1"}})
    api.approve_task = Mock()
    api.update_review_policy = Mock()
    api.guard_task_mutation = Mock()
    api.patch_review_task = Mock()
    old = api.create_task

    def create(conn, *, review_policy=None, **kw):
        return old(conn, **kw)

    api.create_task = create


@pytest.fixture
def kanban(fake_api, tmp_path):
    db = install_fake_kanban_actions(fake_api, tmp_path / "kanban")
    db.create_board(BOARD, name="DeskRPG")
    return db


def test_정책_계약_전체가_있을_때만_기능을_광고한다(fake_api):
    enable(fake_api)
    assert "kanban_review_policy_v1" in cf.capabilities(fake_api)
    for key in ("get_review_state", "approve_task", "update_review_policy", "guard_task_mutation", "patch_review_task"):
        saved = getattr(fake_api, key)
        setattr(fake_api, key, None)
        assert "kanban_review_policy_v1" not in cf.capabilities(fake_api)
        setattr(fake_api, key, saved)
    fake_api.API_VERSION = 2
    assert "kanban_review_policy_v1" not in cf.capabilities(fake_api)


async def test_미지원_정책_생성은_쓰기_전에_428(fake_api, kanban, aiohttp_client):
    client = await aiohttp_client(make_app(fake_api))
    response = await client.post(f"/deskrpg/kanban/tasks?board={BOARD}", json={"title": "검토", "review_policy": POLICY})
    assert response.status == 428
    assert not kanban.calls.get("create_task")


async def test_정책과_검토상태가_생성_응답에_보존된다(fake_api, kanban, aiohttp_client):
    enable(fake_api)
    create = Mock(wraps=fake_api.create_task)
    # 기능 탐지는 실제 시그니처를 본다.
    create.__signature__ = __import__("inspect").signature(fake_api.create_task)
    fake_api.create_task = create
    client = await aiohttp_client(make_app(fake_api))
    response = await client.post(f"/deskrpg/kanban/tasks?board={BOARD}", json={"title": "검토", "review_policy": POLICY})
    assert response.status == 201, await response.text()
    assert create.call_args.kwargs["review_policy"] == POLICY
    assert (await response.json())["task"]["review"]["policy"] == POLICY


async def test_보호된_승인은_본문_actor를_신뢰하지_않는다(fake_api, kanban, aiohttp_client):
    task = _task(kanban)
    enable(fake_api)
    client = await _client(aiohttp_client, fake_api)
    response = await _post(client, task.id, "approve", {"submission_id": "s1", "request_id": "r1", "actor_id": "spoof"})
    assert response.status == 400
    fake_api.approve_task.assert_not_called()


async def test_보호된_승인은_서버_사용자_헤더와_제출_id를_전달한다(fake_api, kanban, aiohttp_client):
    task = _task(kanban)
    enable(fake_api)
    client = await _client(aiohttp_client, fake_api)
    response = await _post(client, task.id, "approve", {"submission_id": "s1", "request_id": "r1"}, headers={"X-DeskRPG-User-Id": "user-1", "X-DeskRPG-Actor": "이름"})
    assert response.status == 200, await response.text()
    assert fake_api.approve_task.call_args.kwargs == {"actor_id": "deskrpg:user-1", "submission_id": "s1", "request_id": "r1"}
    assert not kanban.calls.get("complete_task")


async def test_사용자_헤더_없는_보호_승인은_거절된다(fake_api, kanban, aiohttp_client):
    task = _task(kanban)
    enable(fake_api)
    client = await _client(aiohttp_client, fake_api)
    response = await _post(client, task.id, "approve", {"submission_id": "s1", "request_id": "r1"})
    assert response.status == 400
    fake_api.approve_task.assert_not_called()


async def test_보호_카드_편집은_native_원자적_함수로_위임한다(fake_api, kanban, aiohttp_client):
    task = _task(kanban)
    enable(fake_api)
    client = await aiohttp_client(make_app(fake_api))
    response = await client.patch(f"/deskrpg/kanban/tasks/{task.id}?board={BOARD}", json={"title":"새 제목", "body":"조건", "review_policy": POLICY, "expected_revision":1})
    assert response.status == 200, await response.text()
    assert fake_api.patch_review_task.call_args.kwargs == {"fields":{"title":"새 제목", "body":"조건"}, "policy":POLICY, "expected_revision":1}


async def test_보호_카드_상태와_조건_동시_편집은_부분_쓰기_없이_거절한다(fake_api, kanban, aiohttp_client):
    task = _task(kanban)
    enable(fake_api)
    client = await aiohttp_client(make_app(fake_api))
    response = await client.patch(f"/deskrpg/kanban/tasks/{task.id}?board={BOARD}", json={"title":"변경", "status":"done"})
    assert response.status == 400
    fake_api.patch_review_task.assert_not_called()
    assert not kanban.calls.get("complete_task")


@pytest.mark.parametrize("name", ["곽지호 + 운영자", "가" * 200])
async def test_승인_표시이름은_URL_헤더에서_복원한다(fake_api, kanban, aiohttp_client, name):
    task = _task(kanban)
    enable(fake_api)
    client = await _client(aiohttp_client, fake_api)
    response = await _post(client, task.id, "approve", {"submission_id": "s1", "request_id": "r1"}, headers={"X-DeskRPG-User-Id": "user-1", "X-DeskRPG-User-Name": quote(name, safe="")})
    assert response.status == 200, await response.text()
    assert fake_api.approve_task.call_args.kwargs["actor_name"] == name


@pytest.mark.parametrize("name", ["가" * 201, "", "%FF"])
async def test_잘못된_승인_표시이름은_쓰기_전에_거절한다(fake_api, kanban, aiohttp_client, name):
    task = _task(kanban)
    enable(fake_api)
    client = await _client(aiohttp_client, fake_api)
    response = await _post(client, task.id, "approve", {"submission_id": "s1", "request_id": "r1"}, headers={"X-DeskRPG-User-Id": "user-1", "X-DeskRPG-User-Name": quote(name, safe="%")})
    assert response.status == 400
    fake_api.approve_task.assert_not_called()


async def test_표시이름_본문_위조는_거절한다(fake_api, kanban, aiohttp_client):
    task = _task(kanban)
    enable(fake_api)
    client = await _client(aiohttp_client, fake_api)
    response = await _post(client, task.id, "approve", {"submission_id": "s1", "request_id": "r1", "actor_name": "위조"}, headers={"X-DeskRPG-User-Id": "user-1"})
    assert response.status == 400
    fake_api.approve_task.assert_not_called()


async def test_사용자_이름_헤더는_소유자_인증을_대체하지_않는다(fake_api, kanban, aiohttp_client):
    task = _task(kanban)
    enable(fake_api)
    client = await _client(aiohttp_client, fake_api, authorized=False)
    response = await _post(client, task.id, "approve", {"submission_id": "s1", "request_id": "r1"}, headers={"X-DeskRPG-User-Id": "user-1", "X-DeskRPG-User-Name": "Dante"})
    assert response.status == 401
    fake_api.approve_task.assert_not_called()

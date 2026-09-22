"""패치된 Hermes에서 HTTP→정본 승인 왕복. 구버전은 명시적 기능 부재 계약을 검사한다."""
import os

import pytest

from deskrpg_plugin.contract_fields import has_review_policy
from tests.integration.test_kanban_real import BOARD, B, _board, _task

pytestmark = pytest.mark.integration
POLICY = {"version": 1, "mode": "human", "reviewer_profile": None}
HEADERS = {"X-DeskRPG-User-Id": "channel-member"}


async def test_실제_코어의_사람_승인_수정_무효화_멱등_계약(client, api, profile):
    await _board(client)
    if not has_review_policy(api):
        assert os.environ.get("HERMES_REVIEW_POLICY_REQUIRED") != "1", "Patched core policy contract missing"
        response = await client.post(f"/deskrpg/kanban/tasks{B}", json={"title":"정책", "review_policy":POLICY})
        assert response.status == 428
        return
    task = await _task(client, review_policy=POLICY, assignee=profile)
    tid = task["id"]
    assert task["review"]["policy"] == POLICY
    assert (await client.patch(f"/deskrpg/kanban/tasks/{tid}{B}", json={"status":"done"})).status == 409
    with api.connect_closing(board=BOARD) as conn:
        assert api.request_review(conn, tid, summary="결과 버전 1")
    detail = await (await client.get(f"/deskrpg/kanban/tasks/{tid}{B}")).json()
    sid = detail["task"]["review"]["submission"]["id"]
    stale = {"submission_id":sid, "request_id":"old"}
    edit = await client.patch(f"/deskrpg/kanban/tasks/{tid}{B}", json={"body":"새 완료 조건", "title":"새 제목"})
    assert edit.status == 200, await edit.text()
    assert (await client.post(f"/deskrpg/kanban/tasks/{tid}/approve{B}", json=stale, headers=HEADERS)).status == 409
    # 변경된 결과는 명시적 재개 후 다시 제출한다.
    reopened = await client.patch(f"/deskrpg/kanban/tasks/{tid}{B}", json={"status":"ready"})
    assert reopened.status == 200, await reopened.text()
    with api.connect_closing(board=BOARD) as conn:
        assert api.request_review(conn, tid, summary="결과 버전 2")
    detail = await (await client.get(f"/deskrpg/kanban/tasks/{tid}{B}")).json()
    sid = detail["task"]["review"]["submission"]["id"]
    body = {"submission_id":sid, "request_id":"current"}
    assert (await client.post(f"/deskrpg/kanban/tasks/{tid}/approve{B}", json=body)).status == 400
    for _ in range(2):
        response = await client.post(f"/deskrpg/kanban/tasks/{tid}/approve{B}", json=body, headers=HEADERS)
        assert response.status == 200, await response.text()
        approved = (await response.json())["task"]
        assert approved["status"] == "done"
        assert approved["review"]["approval"]["actor_id"] == "deskrpg:channel-member"
    response = await client.patch(f"/deskrpg/kanban/tasks/{tid}{B}", json={"body":"승인 뒤 변경"})
    assert response.status == 409

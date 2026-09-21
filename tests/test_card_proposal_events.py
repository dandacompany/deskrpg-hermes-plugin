"""카드 제안 사건은 `include=card_proposals` 로만 나간다 — 옵트인하지 않은 DeskRPG 는 받지 않는다."""

import pytest

import deskrpg_plugin.card_proposal_store as store
from deskrpg_plugin import contract_fields, events
from tests.fakes_cron import install_fake_cron
from tests.fakes_events import events_client, install_fake_events


@pytest.fixture
def kanban(fake_api, tmp_path):
    return install_fake_events(fake_api, tmp_path / "kanban")


@pytest.fixture
def cron_store(fake_api, tmp_path):
    return install_fake_cron(fake_api, tmp_path)


def test_사건_종류_문자열이_세_곳에서_같다():
    """같은 문자열이 저장소·사건 tail·계약 목록에 따로 적혀 있다(아티팩트와 같은 구조).
    한 곳만 고치면 사건이 append 되고도 tail 이 거르거나 계약에서 탈락한다 — 이 단정이 그걸 막는다."""
    assert store.EVENT_CREATED == "card_proposal.created"
    assert events.CARD_PROPOSAL_EVENT_KINDS == (store.EVENT_CREATED,)
    assert store.EVENT_CREATED in contract_fields.EVENT_KINDS


def test_옵트인_토큰은_서로_독립이다():
    assert events.wants_card_proposals("card_proposals") is True
    assert events.wants_card_proposals("artifacts") is False
    assert events.wants_artifacts("card_proposals") is False
    assert events.wants_card_proposals("artifacts,card_proposals") is True
    assert events.wants_card_proposals(None) is False


def test_종류_필터는_옵트인한_출처만_담는다():
    only_cards = events.artifact_kind_filter(include_artifacts=False, include_card_proposals=True)
    assert only_cards == ("card_proposal.created",)
    assert events.artifact_kind_filter(include_artifacts=False, include_card_proposals=False) == ()
    both = events.artifact_kind_filter(include_artifacts=True, include_card_proposals=True)
    assert "artifact.created" in both and "card_proposal.created" in both


def test_아티팩트만_옵트인하면_제안_사건이_실리지_않는다(tmp_api):
    store.create(tmp_api, profile="noah", title="t", summary="s", body=None, acceptance=None)
    artifact_only = events.artifact_kind_filter(include_artifacts=True, include_card_proposals=False)
    assert events.read_artifact_events(tmp_api, 0, 10, artifact_only) == []
    cards = events.read_artifact_events(tmp_api, 0, 10, ("card_proposal.created",))
    assert [e["kind"] for e in cards] == ["card_proposal.created"]
    assert cards[0]["id"].startswith("a:") and cards[0]["profile"] == "noah"


# ---------------------------------------------------------------------------
# 커서 — 제안 사건은 `a` 를 담은 커서로 다시 불려도 실려 온다


async def test_받아_둔_커서로_다시_부르면_그_뒤에_난_제안이_실려_온다(
    aiohttp_client, fake_api, kanban, cron_store
):
    """DeskRPG 가 꺼져 있던 동안 난 제안이 사라지지 않는 근거. 커서는 플러그인이 아니라
    DeskRPG DB 에 살고 토큰 자체가 `a` 를 담으므로, 껐다 켠 뒤 같은 토큰으로 부르면 그 사이의
    제안이 그대로 온다. 이 단정이 없으면 `a` 전진 규칙이 바뀌어도 아무것도 실패하지 않는다."""
    client = await events_client(aiohttp_client, fake_api)
    start = await (
        await client.get("/deskrpg/events?board=default&include=artifacts,card_proposals")
    ).json()
    assert events.decode_cursor(start["cursor"])["a"] == 0

    # 여기서부터 DeskRPG 는 꺼져 있다.
    proposal_id = store.create(fake_api, profile="noah", title="주간 보고 정리", summary="금요일마다")

    body = await (
        await client.get(
            "/deskrpg/events?board=default&include=artifacts,card_proposals"
            f"&cursor={start['cursor']}"
        )
    ).json()
    assert [e["kind"] for e in body["events"]] == ["card_proposal.created"]
    assert body["events"][0]["payload"]["proposal_id"] == proposal_id
    assert events.decode_cursor(body["cursor"])["a"] == 1

    # 커서가 전진했으니 같은 제안이 두 번 오지 않는다 — 방 알림이 둘 생기는 것을 막는 쪽 성질이다.
    again = await (
        await client.get(
            "/deskrpg/events?board=default&include=artifacts,card_proposals"
            f"&cursor={body['cursor']}"
        )
    ).json()
    assert again["events"] == []

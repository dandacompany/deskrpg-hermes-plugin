"""카드 제안 사건은 `include=card_proposals` 로만 나간다 — 옵트인하지 않은 DeskRPG 는 받지 않는다."""

import deskrpg_plugin.card_proposal_store as store
from deskrpg_plugin import contract_fields, events


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

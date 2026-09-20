"""`propose_kanban_card` 도구 — 계약, 실패 문자열, 그리고 "카드를 만들지 않는다"."""

import json

import deskrpg_plugin.card_proposal_tool as tool


def test_schema_requires_title_and_summary():
    assert tool.TOOL_NAME == "propose_kanban_card"
    assert tool.TOOLSET == "deskrpg"
    assert tool.TOOL_SCHEMA["parameters"]["required"] == ["title", "summary"]
    assert tool.TOOL_SCHEMA["parameters"]["additionalProperties"] is False
    # 담당은 모델이 고르지 않는다 — 스키마에 담당 필드가 없다.
    assert "assignee" not in tool.TOOL_SCHEMA["parameters"]["properties"]


def test_missing_title_returns_error_string_not_exception(tmp_api):
    handler = tool.make_handler(tmp_api)
    out = handler({"summary": "요약만 있다"}, profile="noah")
    assert isinstance(out, str)
    assert json.loads(out)["error"] == "invalid_arguments"
    assert "title" in json.loads(out)["detail"]


def test_success_creates_proposal_and_no_card(tmp_api):
    handler = tool.make_handler(tmp_api)
    out = handler({"title": "주간 보고 정리", "summary": "금요일마다 모은다"}, profile="noah")
    body = json.loads(out)
    assert body["proposed"] is True
    assert body["proposal_id"]
    # 정본에 카드가 생기지 않았다 — 이 플러그인은 칸반에 쓰지 않았다
    assert tmp_api.kanban_writes() == []


def test_ignores_model_supplied_session_id(tmp_api):
    handler = tool.make_handler(tmp_api)
    out = handler({"title": "t", "summary": "s", "session_id": "거짓"}, profile="noah",
                  session_id="진짜")
    assert json.loads(out)["proposed"] is True


def test_profile_falls_back_to_running_home(tmp_api):
    """Hermes 가 profile kwarg 을 주지 않아도 모델 인자가 아니라 실행 중인 홈에서 읽는다."""
    handler = tool.make_handler(tmp_api)
    out = handler({"title": "t", "summary": "s", "profile": "거짓"})
    assert json.loads(out)["proposed"] is True
    assert tmp_api.events()[0]["profile"] == "noah"


def test_등록은_도구와_프롬프트_절을_올리고_아티팩트가_죽어도_산다(monkeypatch):
    """`_register_card_proposal` 은 아티팩트 등록과 독립이다 — 한쪽의 import 실패가 다른 쪽을 끌어내리지 않는다."""
    import sys
    import types

    import deskrpg_plugin

    from deskrpg_plugin import card_proposal_prompt

    calls = []

    class _Ctx:
        def __getattr__(self, name):
            if not name.startswith("register_"):
                raise AttributeError(name)
            return lambda *a, **k: calls.append((name, a, k))

    monkeypatch.setattr(deskrpg_plugin, "load", lambda: types.SimpleNamespace())
    monkeypatch.delattr(deskrpg_plugin, "artifacts_hook", raising=False)
    monkeypatch.setitem(sys.modules, "deskrpg_plugin.artifacts_hook", None)
    deskrpg_plugin.register(_Ctx())

    registered = {c[1][0] for c in calls if c[0] == "register_tool"}
    assert tool.TOOL_NAME in registered
    sections = {c[1][0] for c in calls if c[0] == "register_system_prompt_section"}
    assert card_proposal_prompt.SECTION_ID in sections

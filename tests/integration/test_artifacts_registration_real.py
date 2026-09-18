"""고정 커밋 Hermes 위에서 플러그인 컨텍스트가 도구·훅·프롬프트 섹션 등록을 받는지 실증한다."""
import inspect

import pytest

pytest.importorskip("hermes_cli")


def test_플러그인_컨텍스트가_도구_훅_프롬프트_섹션_등록_메서드를_가진다():
    from hermes_cli import plugins as hp

    ctx_cls = getattr(hp, "PluginContext", None)
    assert ctx_cls is not None, "PluginContext 이름이 바뀌었다 — plugins.py 를 다시 읽는다"
    for name in ("register_tool", "register_hook", "register_system_prompt_section", "register_skill"):
        assert hasattr(ctx_cls, name), f"{name} 이 없다"
    sig = inspect.signature(ctx_cls.register_tool)
    assert "toolset" in sig.parameters and "schema" in sig.parameters and "handler" in sig.parameters


def test_post_tool_call_이_훅_이름_목록에_있다():
    from hermes_cli import plugins as hp

    names = set()
    for attr in dir(hp):
        value = getattr(hp, attr)
        if isinstance(value, (set, frozenset, tuple, list)) and "post_tool_call" in value:
            names |= set(value)
    assert {"post_tool_call", "on_session_end", "pre_tool_call"} <= names

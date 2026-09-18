"""register(ctx) 가 라우트 외에 도구·훅·프롬프트 섹션·스킬을 등록하고, 하나가 실패해도 나머지는 산다."""
import types

import deskrpg_plugin
from deskrpg_plugin import artifacts_prompt, artifacts_tool


class _Ctx:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def _rec(self, name):
        def f(*a, **k):
            self.calls.append((name, a, k))
            if name in self.fail:
                raise RuntimeError(name)
        return f

    def __getattr__(self, name):
        if name.startswith("register_"):
            return self._rec(name)
        raise AttributeError(name)


def test_도구_훅_섹션_스킬_라우트가_모두_등록된다(monkeypatch):
    monkeypatch.setattr(deskrpg_plugin, "load", lambda: types.SimpleNamespace())
    ctx = _Ctx()
    deskrpg_plugin.register(ctx)
    names = [c[0] for c in ctx.calls]
    assert names.count("register_platform_handler") == 1
    tool = next(c for c in ctx.calls if c[0] == "register_tool")
    assert tool[1][0] == artifacts_tool.TOOL_NAME and tool[1][1] == artifacts_tool.TOOLSET
    assert tool[1][2] is artifacts_tool.TOOL_SCHEMA and callable(tool[1][3])
    hook = next(c for c in ctx.calls if c[0] == "register_hook")
    assert hook[1][0] == "post_tool_call" and callable(hook[1][1])
    section = next(c for c in ctx.calls if c[0] == "register_system_prompt_section")
    assert section[1][0] == artifacts_prompt.SECTION_ID and "artifact_save" in section[1][1]
    skill = next(c for c in ctx.calls if c[0] == "register_skill")
    assert skill[1][0] == "artifact" and skill[1][1].name == "SKILL.md"


def test_도구_등록이_실패해도_라우트는_등록된다(monkeypatch):
    monkeypatch.setattr(deskrpg_plugin, "load", lambda: types.SimpleNamespace())
    ctx = _Ctx(fail={"register_tool", "register_hook"})
    deskrpg_plugin.register(ctx)
    assert any(c[0] == "register_platform_handler" for c in ctx.calls)


def test_스킬_파일이_존재하고_섹션_텍스트가_짧다():
    assert artifacts_prompt.SKILL_PATH.is_file()
    assert len(artifacts_prompt.SECTION_TEXT) < 900

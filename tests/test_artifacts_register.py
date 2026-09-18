"""register(ctx) 가 라우트 외에 도구·훅·프롬프트 섹션·스킬을 등록하고, 하나가 실패해도 나머지는 산다."""
import sys
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


def test_아티팩트_모듈_import_가_실패해도_라우트는_등록되고_예외가_새지_않는다(monkeypatch):
    monkeypatch.setattr(deskrpg_plugin, "load", lambda: types.SimpleNamespace())
    # deskrpg_plugin.artifacts_hook 을 None 으로 만들면 `from . import artifacts_hook` 이
    # ImportError 를 던진다(파이썬은 sys.modules 값이 None 이면 그렇게 취급한다) — 단, 패키지
    # 객체에 이미 그 이름의 속성이 남아 있으면(다른 테스트가 먼저 성공 임포트했을 때) 파이썬이
    # sys.modules 를 다시 보지 않고 그 속성을 그대로 쓴다. 그래서 속성도 함께 지운다.
    monkeypatch.delattr(deskrpg_plugin, "artifacts_hook", raising=False)
    monkeypatch.setitem(sys.modules, "deskrpg_plugin.artifacts_hook", None)
    ctx = _Ctx()
    deskrpg_plugin.register(ctx)  # 던지지 않아야 한다
    names = [c[0] for c in ctx.calls]
    assert names.count("register_platform_handler") == 1
    assert "register_tool" not in names


def test_스킬_파일이_존재하고_섹션_텍스트가_짧다():
    assert artifacts_prompt.SKILL_PATH.is_file()
    assert len(artifacts_prompt.SECTION_TEXT) < 900


def test_응답_훅이_post_llm_call_로_따로_등록된다(monkeypatch):
    monkeypatch.setattr(deskrpg_plugin, "load", lambda: types.SimpleNamespace())
    ctx = _Ctx()
    deskrpg_plugin.register(ctx)
    hooks = {c[1][0]: c[1][1] for c in ctx.calls if c[0] == "register_hook"}
    assert set(hooks) == {"post_tool_call", "post_llm_call"}
    assert callable(hooks["post_llm_call"]) and hooks["post_llm_call"] is not hooks["post_tool_call"]


def test_응답_훅_등록이_실패해도_도구와_라우트는_산다(monkeypatch):
    monkeypatch.setattr(deskrpg_plugin, "load", lambda: types.SimpleNamespace())

    class _FailLlmHook(_Ctx):
        def __getattr__(self, name):
            if name == "register_hook":
                def f(hook_name, cb):
                    self.calls.append((name, (hook_name, cb), {}))
                    if hook_name == "post_llm_call":
                        raise RuntimeError("no such hook")
                return f
            return super().__getattr__(name)

    ctx = _FailLlmHook()
    deskrpg_plugin.register(ctx)
    names = [c[0] for c in ctx.calls]
    assert "register_platform_handler" in names and "register_tool" in names and "register_skill" in names

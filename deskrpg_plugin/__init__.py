"""DeskRPG 전용 라우트·도구·훅을 Hermes 에 등록하는 플러그인."""

import logging

from ._hermes_api import MissingHermesApi, load

logger = logging.getLogger(__name__)

__all__ = ["register"]


def register(ctx) -> None:
    """플러그인 진입점.

    라우트 등록은 어댑터의 connect() 시점에 일어난다 — 여기서는 팩토리만 넘긴다.
    아티팩트의 도구·훅·프롬프트 섹션·스킬은 여기서 바로 등록한다. **각각 따로 감싼다** — 하나가
    실패해도(구버전 Hermes 에 메서드가 없거나 이름 충돌) 나머지와 라우트는 살아야 한다.
    """
    api = load()  # 없으면 MissingHermesApi 를 던진다

    def _wire(native, adapter) -> None:
        if native is None:
            logger.warning("[deskrpg] api_server 가 native app 을 주지 않았다 — 등록을 건너뛴다")
            return
        from .routes import attach

        attach(native, adapter, api)

    ctx.register_platform_handler("api_server", _wire)
    _register_artifacts(ctx, api)


def _register_artifacts(ctx, api) -> None:
    try:
        from . import artifacts_hook, artifacts_prompt, artifacts_tool
    except Exception as exc:  # noqa: BLE001 — 이 임포트 실패로 라우트까지 끌려 내려가면 안 된다
        # 로더(plugins_loader.py)는 register(ctx) 가 던지면 이 호출로 만든 등록을 전부(라우트
        # 포함) 폐기한다 — 그래서 이 import 도 개별 단계와 똑같이 감싸고 그냥 돌아간다.
        logger.warning("[deskrpg] 아티팩트 모듈 import 실패: %s", type(exc).__name__)
        return

    steps = (
        ("tool", lambda: ctx.register_tool(
            artifacts_tool.TOOL_NAME, artifacts_tool.TOOLSET, artifacts_tool.TOOL_SCHEMA,
            artifacts_tool.make_handler(api), description=artifacts_tool.TOOL_SCHEMA["description"], emoji="🗂️")),
        ("hook", lambda: ctx.register_hook("post_tool_call", artifacts_hook.make_hook(api))),
        ("prompt", lambda: ctx.register_system_prompt_section(
            artifacts_prompt.SECTION_ID, artifacts_prompt.SECTION_TEXT, position="after_memory")),
        ("skill", lambda: ctx.register_skill("artifact", artifacts_prompt.SKILL_PATH,
                                              description="결과물을 DeskRPG 아티팩트로 저장하는 규칙")),
    )
    for name, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 — 한 등록의 실패가 다른 등록을 막지 않는다
            logger.warning("[deskrpg] 아티팩트 %s 등록 실패: %s", name, type(exc).__name__)

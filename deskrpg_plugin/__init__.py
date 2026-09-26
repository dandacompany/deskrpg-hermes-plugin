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
            logger.warning("[deskrpg] api_server did not provide a native app — skipping registration")
            return
        from .routes import attach

        attach(native, adapter, api)

    ctx.register_platform_handler("api_server", _wire)
    _register_artifacts(ctx, api)
    _register_card_proposal(ctx, api)
    _register_approval_blocked(ctx, api)
    _register_ask_user(ctx, api)
    _register_review_hooks(ctx, api)


def _register_artifacts(ctx, api) -> None:
    try:
        from . import artifacts_hook, artifacts_prompt, artifacts_tool
    except Exception as exc:  # noqa: BLE001 — 이 임포트 실패로 라우트까지 끌려 내려가면 안 된다
        # 로더(plugins_loader.py)는 register(ctx) 가 던지면 이 호출로 만든 등록을 전부(라우트
        # 포함) 폐기한다 — 그래서 이 import 도 개별 단계와 똑같이 감싸고 그냥 돌아간다.
        logger.warning("[deskrpg] artifact module import failed: %s", type(exc).__name__)
        return

    steps = (
        ("tool", lambda: ctx.register_tool(
            artifacts_tool.TOOL_NAME, artifacts_tool.TOOLSET, artifacts_tool.TOOL_SCHEMA,
            artifacts_tool.make_handler(api), description=artifacts_tool.TOOL_SCHEMA["description"], emoji="🗂️")),
        ("hook", lambda: ctx.register_hook("post_tool_call", artifacts_hook.make_hook(api))),
        ("response_hook", lambda: ctx.register_hook("post_llm_call", artifacts_hook.make_response_hook(api))),
        ("prompt", lambda: ctx.register_system_prompt_section(
            artifacts_prompt.SECTION_ID, artifacts_prompt.SECTION_TEXT, position="after_memory")),
        ("skill", lambda: ctx.register_skill("artifact", artifacts_prompt.SKILL_PATH,
                                              description="Rules for saving results as DeskRPG artifacts")),
    )
    for name, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 — 한 등록의 실패가 다른 등록을 막지 않는다
            logger.warning("[deskrpg] artifact %s registration failed: %s", name, type(exc).__name__)


def _register_card_proposal(ctx, api) -> None:
    """카드 제안 도구와 프롬프트 절. `_register_artifacts` 와 같은 모양 — import 도, 각 단계도
    따로 감싼다. 하나가 실패해도 라우트와 아티팩트는 살아야 한다."""
    try:
        from . import card_proposal_prompt, card_proposal_tool
    except Exception as exc:  # noqa: BLE001 — 이 임포트 실패로 라우트까지 끌려 내려가면 안 된다
        logger.warning("[deskrpg] card proposal module import failed: %s", type(exc).__name__)
        return

    steps = (
        ("tool", lambda: ctx.register_tool(
            card_proposal_tool.TOOL_NAME, card_proposal_tool.TOOLSET, card_proposal_tool.TOOL_SCHEMA,
            card_proposal_tool.make_handler(api),
            description=card_proposal_tool.TOOL_SCHEMA["description"], emoji="📋")),
        ("prompt", lambda: ctx.register_system_prompt_section(
            card_proposal_prompt.SECTION_ID, card_proposal_prompt.SECTION_TEXT,
            position="after_memory")),
    )
    for name, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 — 한 등록의 실패가 다른 등록을 막지 않는다
            logger.warning("[deskrpg] card proposal %s registration failed: %s", name, type(exc).__name__)


def _register_ask_user(ctx, api) -> None:
    """대화 중 묻기 도구와 프롬프트 절. 각각 따로 감싼다 — 실패해도 라우트와 다른 도구는 산다.

    프롬프트 절이 반드시 있어야 한다: Hermes 의 Tool Search 가 플러그인 도구를 tool_search 뒤로 미뤄서,
    도구 설명만으로는 모델이 이 도구가 있는 줄 모른다."""
    try:
        from . import ask_user, ask_user_prompt
    except Exception as exc:  # noqa: BLE001
        logger.warning("[deskrpg] ask_user module import failed: %s", type(exc).__name__)
        return

    steps = (
        ("tool", lambda: ctx.register_tool(
            ask_user.TOOL_NAME, ask_user.TOOLSET, ask_user.TOOL_SCHEMA, ask_user.make_handler(api),
            description=ask_user.TOOL_SCHEMA["description"], emoji="❓")),
        ("prompt", lambda: ctx.register_system_prompt_section(
            ask_user_prompt.SECTION_ID, ask_user_prompt.SECTION_TEXT, position="after_memory")),
    )
    for name, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 — 한 등록의 실패가 다른 등록을 막지 않는다
            logger.warning("[deskrpg] ask_user %s registration failed: %s", name, type(exc).__name__)


def _register_approval_blocked(ctx, api) -> None:
    """무인 실행 막힘 사건 훅(0.18.0). 다른 등록과 따로 감싼다 — 실패해도 라우트·아티팩트는 살아야 한다.
    크론·칸반 워커에서는 그 프로필 홈에 이 플러그인이 있을 때만 돈다(워커 전파, `worker_plugin`)."""
    try:
        from . import approval_blocked

        ctx.register_hook("post_tool_call", approval_blocked.make_hook(api))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[deskrpg] approval-blocked hook registration failed: %s", type(exc).__name__)


def _register_review_hooks(ctx, api) -> None:
    """Per-card approval hooks. They act only inside kanban workers (`HERMES_KANBAN_TASK`), so in the gateway they
    are registered but idle. Each hook is wrapped separately, like the other registrations."""
    try:
        from . import review_hooks
    except Exception as exc:  # noqa: BLE001 — must not take the routes down with it
        logger.warning("[deskrpg] review hooks module import failed: %s", type(exc).__name__)
        return

    registered = 0
    for name, factory in (("pre_tool_call", review_hooks.make_pre_hook), ("post_tool_call", review_hooks.make_post_hook)):
        try:
            ctx.register_hook(name, factory(api))
            registered += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[deskrpg] review %s registration failed: %s", name, type(exc).__name__)
    review_hooks.HOOKS_REGISTERED = registered == 2

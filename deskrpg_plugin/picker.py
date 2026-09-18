"""직원 설정 피커 — 툴셋·스킬 목록.

**목록을 우리가 만들지 않는다.** Hermes 의 `hermes tools`·`hermes skills` 가 쓰는 함수를 그대로 부른다.
Hermes 0.21.3 의 `/v1/toolsets` 는 되지만 `/v1/skills` 는 500 이라(2026-09-18 실측) 둘 다 여기서 낸다 —
DeskRPG 가 능력 하나로 폴백을 판단할 수 있게.

**프로필 홈에서 읽는다.** 게이트웨이 프로세스 하나가 모든 `/p/{profile}` 요청을 받으므로, 홈을 갈아 끼우지
않으면 default 의 스킬 폴더·.env 를 본다(`catalog.py` 의 2026-09-17 정정과 같은 함정).
"""

from __future__ import annotations

import contextlib
import logging

from aiohttp import web

from . import config as _config
from .common import RequestError, guarded, run_blocking
from .cron import resolve_profile_home

logger = logging.getLogger(__name__)

PLATFORM = "api_server"


@contextlib.contextmanager
def _home_scope(api, home):
    token = api.set_hermes_home_override(str(home))
    try:
        yield
    finally:
        api.reset_hermes_home_override(token)


def _load_config(home) -> dict:
    try:
        return _config._load(home / _config.CONFIG_FILENAME)
    except _config.ConfigUnreadable as exc:
        # 사유 문자열을 싣지 않는다 — PyYAML 오류는 망가진 줄을 인용하므로 `api_key: "sk-…` 같은
        # 값이 그대로 나간다. 응답은 고정 문구, 로그는 타입 이름, 체인은 끊는다(`from None`).
        logger.warning("[deskrpg] 피커 config 읽기 실패: %s",
                       type(exc.__cause__ or exc).__name__)
        raise RequestError(409, "config_unreadable", "config.yaml 을 해석할 수 없다") from None


def _configurable(api):
    return [
        row for row in api._get_effective_configurable_toolsets()
        if api._toolset_allowed_for_platform(row[0], PLATFORM)
    ]


def toolset_rows(api, home) -> list[dict]:
    cfg = _load_config(home)
    with _home_scope(api, home):
        enabled = api._get_platform_tools(cfg, PLATFORM, include_default_mcp_servers=False)
        rows = []
        for name, label, description in _configurable(api):
            try:
                configured = bool(api._toolset_has_keys(name, cfg))
            except Exception as exc:  # noqa: BLE001 — 한 툴셋의 판정 실패가 목록을 죽이면 안 된다
                logger.warning("[deskrpg] 툴셋 키 판정 실패: %s — %s", name, type(exc).__name__)
                configured = None
            rows.append({"name": name, "label": label, "description": description,
                         "enabled": name in enabled, "configured": configured})
    return rows


def known_toolset_names(api, home) -> set[str]:
    with _home_scope(api, home):
        return {row[0] for row in _configurable(api)}


def _disabled(cfg) -> set[str]:
    skills = cfg.get("skills")
    raw = skills.get("disabled") if isinstance(skills, dict) else None
    return {str(x) for x in raw} if isinstance(raw, list) else set()


def skill_rows(api, home) -> list[dict]:
    cfg = _load_config(home)
    disabled = _disabled(cfg)
    essential = set(getattr(api, "ESSENTIAL_SKILLS", None) or ())
    with _home_scope(api, home):
        found = api._sort_skills(api._find_all_skills(skip_disabled=True))
    return [
        {"name": s["name"], "category": s.get("category") or "", "description": s.get("description") or "",
         "disabled": s["name"] in disabled and s["name"] not in essential,
         "essential": s["name"] in essential}
        for s in found
    ]


def known_skill_names(api, home) -> set[str]:
    with _home_scope(api, home):
        return {s["name"] for s in api._find_all_skills(skip_disabled=True)}


def toolsets_handler(api):
    """`GET /p/{profile}/deskrpg/toolsets`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        rows = await run_blocking(toolset_rows, api, home)
        return web.json_response({"platform": PLATFORM, "toolsets": rows})

    return handler


def skills_handler(api):
    """`GET /p/{profile}/deskrpg/skills`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        return web.json_response({"skills": await run_blocking(skill_rows, api, home)})

    return handler

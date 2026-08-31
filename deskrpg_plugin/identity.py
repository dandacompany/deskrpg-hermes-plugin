"""SOUL.md 읽기/쓰기.

인격의 소유자는 프로필의 SOUL.md 하나다. Hermes 는 `instructions` 를 기존
시스템 프롬프트 **뒤에 이어 붙일** 뿐 대체하지 못하므로(agent/conversation_loop.py),
게이트웨이 너머에서 인격을 바꾸는 길은 이 파일을 쓰는 것뿐이다.
"""

import hashlib

from aiohttp import web

SOUL_FILENAME = "SOUL.md"


def revision_of(body: str) -> str:
    """본문의 지문. PUT 이 이 값을 요구해 '읽지 않으면 못 쓰게' 만든다."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿").strip()


def is_default_template(body: str, api) -> bool:
    """손대지 않은 기본 템플릿인가.

    판정 규칙을 우리가 흉내 내지 않는다 — Hermes 자신의 DEFAULT_SOUL_MD 비교와
    is_legacy_template_soul() 을 그대로 쓴다. 그 주석이 원칙을 못박는다:
    사용자가 의도적으로 썼을 만한 것은 절대 템플릿으로 보지 말 것.
    """
    if _normalize(body) == _normalize(api.DEFAULT_SOUL_MD):
        return True
    return bool(api.is_legacy_template_soul(body))


def _resolve(request, api):
    """프로필 이름을 검증하고 SOUL.md 경로를 돌려준다.

    이름을 문자열로 이어 붙이지 않는다 — validate_profile_name 을 통과한 이름만
    get_profile_dir 에 넘긴다.
    """
    name = request.match_info["profile"]
    try:
        api.validate_profile_name(name)
    except Exception as exc:
        raise web.HTTPBadRequest(reason=f"invalid profile name: {exc}") from exc
    if not api.profile_exists(name):
        raise web.HTTPNotFound(reason=f"no such profile: {name}")
    return name, api.get_profile_dir(name) / SOUL_FILENAME


def get_handler(api):
    async def handler(request):
        _name, path = _resolve(request, api)
        body = path.read_text(encoding="utf-8") if path.is_file() else ""
        return web.json_response(
            {
                "body": body,
                "isDefaultTemplate": is_default_template(body, api),
                "revision": revision_of(body),
            }
        )

    return handler


def put_handler(api):
    """Task 4 에서 채운다."""

    async def handler(request):
        return web.json_response({"error": "not implemented"}, status=501)

    return handler

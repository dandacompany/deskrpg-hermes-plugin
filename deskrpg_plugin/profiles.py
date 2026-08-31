"""프로필 목록·생성·삭제.

전부 프리픽스 없는 경로라 **default(리스너 소유자) 키**로 인증된다. 즉 이
라우트들을 쓰는 클라이언트는 게이트웨이 전체를 쥐는 자격을 들고 있다는 뜻이다 —
그 사실을 README 와 등록 화면이 말해야 한다.
"""

import logging

from aiohttp import web

from .identity import SOUL_FILENAME, is_default_template

logger = logging.getLogger(__name__)


def _validated_name(raw, api):
    try:
        api.validate_profile_name(raw)
    except Exception as exc:
        raise web.HTTPBadRequest(reason=f"invalid profile name: {exc}") from exc
    return raw


def list_handler(api):
    async def handler(request):
        out = []
        for info in api.list_profiles():
            name = getattr(info, "name", str(info))
            soul = api.get_profile_dir(name) / SOUL_FILENAME
            body = soul.read_text(encoding="utf-8") if soul.is_file() else ""
            meta = api.read_profile_meta(api.get_profile_dir(name)) or {}
            out.append(
                {
                    "name": name,
                    "description": meta.get("description") or "",
                    "hasCustomPersona": not is_default_template(body, api),
                }
            )
        return web.json_response({"profiles": out})

    return handler


def create_handler(api):
    async def handler(request):
        try:
            payload = await request.json()
        except Exception:
            raise web.HTTPBadRequest(reason="body must be JSON")

        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(reason="body must be a JSON object")

        name = _validated_name(payload.get("name") or "", api)
        if api.profile_exists(name):
            return web.json_response({"error": "already_exists", "name": name}, status=409)
        api.create_profile(name)
        logger.info("[deskrpg] 프로필 생성: %s", name)
        return web.json_response({"name": name}, status=201)

    return handler


def delete_handler(api):
    """프로필을 지운다.

    `?confirm={name}` 이 경로와 정확히 일치해야 한다. Hermes 의
    `delete_profile(name, yes=True)` 는 CLI 의 대화형 확인을 건너뛰라는 뜻이므로,
    확인 책임이 온전히 이 가드로 넘어온다.

    DeskRPG 의 "해고" 는 이 라우트를 부르지 않는다 — 같은 프로필을 다른 채널이
    쓸 수 있고, 해고는 NPC 를 지우는 것이지 프로필을 지우는 게 아니다.
    """

    async def handler(request):
        name = _validated_name(request.match_info["name"], api)
        if name == "default":
            raise web.HTTPBadRequest(reason="default profile cannot be deleted")
        if request.query.get("confirm") != name:
            raise web.HTTPBadRequest(
                reason="confirm query parameter must equal the profile name"
            )
        if not api.profile_exists(name):
            raise web.HTTPNotFound(reason=f"no such profile: {name}")

        api.delete_profile(name, yes=True)
        wrapper_removed = bool(api.remove_wrapper_script(name))
        logger.warning("[deskrpg] 프로필 삭제: %s (wrapper=%s)", name, wrapper_removed)
        return web.json_response(
            {"name": name, "removed": {"profileDir": True, "wrapperScript": wrapper_removed}}
        )

    return handler

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

            # 프로필 하나의 SOUL.md 를 못 읽어도 (깨진 인코딩, 권한 없음) 목록 전체를
            # 500 으로 죽이지 않는다 — 이 라우트는 마법사가 프로필을 고르는 첫 화면이라,
            # 프로필 하나의 손상이 나머지를 못 보이게 만들면 안 된다. 다만 조용히
            # 넘기지도 않는다: 판정 불가는 hasCustomPersona 를 False(=기본 템플릿)로
            # 둔갑시키지 않고 null 로 남겨, UI 가 실수로 덮어쓰기 확인을 건너뛰지 않게 한다.
            has_custom_persona = True
            if soul.is_file():
                try:
                    body = soul.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    logger.warning(
                        "[deskrpg] SOUL.md 읽기 실패 — 프로필 %s 의 hasCustomPersona 판정 불가: %s",
                        name,
                        exc,
                    )
                    has_custom_persona = None
                else:
                    has_custom_persona = not is_default_template(body, api)
            else:
                has_custom_persona = not is_default_template("", api)

            meta = api.read_profile_meta(api.get_profile_dir(name)) or {}
            out.append(
                {
                    "name": name,
                    "description": meta.get("description") or "",
                    "hasCustomPersona": has_custom_persona,
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

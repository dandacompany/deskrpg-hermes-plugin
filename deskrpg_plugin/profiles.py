"""프로필 목록·생성·삭제.

전부 프리픽스 없는 경로라 **default(리스너 소유자) 키**로 인증된다. 즉 이
라우트들을 쓰는 클라이언트는 게이트웨이 전체를 쥐는 자격을 들고 있다는 뜻이다 —
그 사실을 README 와 등록 화면이 말해야 한다.
"""

import asyncio
import logging

from aiohttp import web

from . import cloneprofile, keyissue, safedelete, worker_plugin
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
        # 복제 원본은 default 뿐이다 — 다른 직원의 프로필을 원본으로 열면 그 직원의 키가
        # 새 프로필로 번진다. 거절은 프로필을 만들기 **전에** 한다.
        clone_from = payload.get("cloneFrom")
        if clone_from is not None and clone_from != cloneprofile.SOURCE_PROFILE:
            raise web.HTTPBadRequest(reason="cloneFrom must be 'default'")
        if clone_from and getattr(api, "PROVIDER_REGISTRY", None) is None:
            raise web.HTTPBadRequest(reason="cloneFrom is not supported by this Hermes build")
        clone_keys = payload.get("cloneKeys")
        if clone_keys is not None:
            if not clone_from:
                raise web.HTTPBadRequest(reason="cloneKeys requires cloneFrom")
            if clone_keys not in cloneprofile.KEY_SCOPES:
                raise web.HTTPBadRequest(reason=f"cloneKeys must be one of: {', '.join(cloneprofile.KEY_SCOPES)}")
        if api.profile_exists(name):
            return web.json_response({"error": "already_exists", "name": name}, status=409)
        api.create_profile(name)
        logger.info("[deskrpg] 프로필 생성: %s", name)

        body = {"name": name}
        # 복제는 키 발급 **앞**에 한다 — 복제가 `.env` 에 모델 키를 쓰고, 키 발급이 그 위에
        # API_SERVER_KEY 한 줄을 더한다. 복제가 실패해도 프로필은 이미 있으므로 201 로
        # 말하고 cloneError 에 (값 없는) 사유를 싣는다. 응답에는 키 **이름**만 나간다.
        if clone_from:
            try:
                cloned = await asyncio.to_thread(cloneprofile.clone_from_default, api, name,
                                                 key_scope=clone_keys or "referenced")
                body["cloned"] = {"configKeys": cloned["configKeys"], "envKeys": cloned["envKeys"],
                                  "keyScope": cloned["keyScope"]}
                body["needsLogin"] = cloned["needsLogin"]
            except cloneprofile.CloneFailed as exc:
                body["cloneError"] = exc.reason
                logger.warning("[deskrpg] 프로필 복제 실패: %s — %s", name, exc.reason)

        # 칸반 워커·크론은 이 프로필 홈으로 뜬다 — 여기에 플러그인이 없으면 워커가 만든 결과물이
        # 하나도 안 쌓인다(worker_plugin 모듈 주석). 실패해도 프로필은 이미 있으므로 201 로 말한다.
        try:
            body["workerPlugin"] = await asyncio.to_thread(worker_plugin.ensure, api, name)
        except worker_plugin.EnsureFailed as exc:
            body["workerPluginError"] = exc.reason
            logger.warning("[deskrpg] 워커 플러그인 준비 실패: %s — %s", name, exc.reason)
        except OSError as exc:
            body["workerPluginError"] = type(exc).__name__
            logger.warning("[deskrpg] 워커 플러그인 준비 실패: %s — %s", name, type(exc).__name__)

        # Hermes 는 빈 .env 를 씨딩할 뿐이라, 키를 발급하지 않으면 이 프로필은
        # 아무도 말을 걸 수 없는 상태로 태어난다(keyissue 모듈 주석 참조).
        # 키 발급이 실패해도 **프로필은 이미 존재한다** — 500 으로 덮으면
        # 사용자는 만들어진 프로필을 모른 채 같은 이름으로 다시 시도하고 409 를
        # 만난다. 만들어졌다는 사실(201)과 키가 없다는 사실을 함께 말한다.
        try:
            body["apiKey"] = keyissue.issue(api.get_profile_dir(name))
            body["keyIssued"] = True
        except keyissue.KeyIssueFailed as exc:
            body["keyIssued"] = False
            body["keyError"] = exc.reason
            logger.warning("[deskrpg] 키 발급 실패: %s — %s", name, exc.reason)
        return web.json_response(body, status=201)

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

        # Hermes 의 delete_profile 을 **부르지 않는다.** 그것은 멀티플렉스
        # 게이트웨이 안에서 자기 자신에게 `systemctl stop` 을 걸어 게이트웨이를
        # 죽인다(실측 2026-09-01, v0.21.0 — safedelete 모듈 주석에 근인과 로그).
        # 대신 전용 유닛 유무를 먼저 보고, 없을 때만 디렉토리를 지운다.
        #
        # wrapper 는 우리가 직접 지운다. 그 함수는 파일 하나를 지울 뿐
        # 서비스 관리자를 건드리지 않아 안전하다. 순서는 wrapper 가 먼저다 —
        # 디렉토리가 사라진 뒤에는 판정할 근거가 없어질 수 있다.
        #
        # 둘 다 동기 파일 I/O 라 별도 스레드로 옮긴다(I-5). 핸들러 안에서 직접
        # 부르면 그동안 게이트웨이의 이벤트 루프가 멎어 다른 모든 프로필의
        # HTTP·Slack 응답이 함께 멈춘다.
        wrapper_removed = bool(await asyncio.to_thread(api.remove_wrapper_script, name))
        try:
            await asyncio.to_thread(
                safedelete.delete_profile_tree,
                name,
                api.get_profile_dir(name),
            )
        except safedelete.ProfileHasService as exc:
            # 아무것도 지우지 않았다. 지웠다면 고아 유닛이 남고, Hermes 에게
            # 맡겼다면 게이트웨이가 죽었을 것이다 — 사람에게 넘긴다.
            logger.warning("[deskrpg] 삭제 거절 — 전용 서비스 있음: %s (%s)", name, exc.unit)
            return web.json_response(
                {
                    "error": "profile_has_service",
                    "name": name,
                    "unit": exc.unit,
                    "reason": (
                        f"프로필 '{name}' 은 자기 서비스({exc.unit})를 갖고 있어 "
                        f"여기서 지울 수 없습니다. 셸에서 정리하세요: "
                        f"hermes profile delete {name}"
                    ),
                },
                status=409,
            )
        logger.warning("[deskrpg] 프로필 삭제: %s (wrapper=%s)", name, wrapper_removed)
        return web.json_response(
            {"name": name, "removed": {"profileDir": True, "wrapperScript": wrapper_removed}}
        )

    return handler

"""SOUL.md 읽기/쓰기.

인격의 소유자는 프로필의 SOUL.md 하나다. Hermes 는 `instructions` 를 기존
시스템 프롬프트 **뒤에 이어 붙일** 뿐 대체하지 못하므로(agent/conversation_loop.py),
게이트웨이 너머에서 인격을 바꾸는 길은 이 파일을 쓰는 것뿐이다.
"""

import hashlib
import time

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
    """SOUL.md 를 쓴다.

    `ifRevision` 을 요구한다. 없거나 어긋나면 409 + 현재 본문을 돌려준다.
    효과가 둘이다 — 그 사이 서버에서 누가 고쳤으면 덮어쓰지 않고, UI 가 반드시
    먼저 읽게 강제되어 "지금 이 성격을 이걸로 바꿉니다" 를 보여줄 수 있다.

    예외 하나: 파일이 아직 없는 첫 생성이다. 이때 `revision_of("")` 는 상수라
    GET 없이도 계산할 수 있으므로 엄밀히는 "읽지 않고 쓴다" 지만, 지울 인격이
    없으니 데이터 유실 위험도 없다 — 그래서 그대로 허용한다.
    """

    async def handler(request):
        _name, path = _resolve(request, api)

        try:
            payload = await request.json()
        except Exception:
            raise web.HTTPBadRequest(reason="body must be JSON")

        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(reason="body must be a JSON object")

        new_body = payload.get("body")
        if not isinstance(new_body, str):
            raise web.HTTPBadRequest(reason="body (string) is required")

        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        current_rev = revision_of(current)

        if payload.get("ifRevision") != current_rev:
            return web.json_response(
                {
                    "error": "revision_mismatch",
                    "body": current,
                    "revision": current_rev,
                },
                status=409,
            )

        if path.is_file():
            # 같은 초에 두 번 써도 백업이 서로 덮어쓰지 않도록 마이크로초까지 찍는다 —
            # 백업의 존재 이유가 옛 내용 보존인데 충돌로 지워지면 목적이 무색해진다.
            backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}")
            backup.write_text(current, encoding="utf-8")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_body, encoding="utf-8")
        return web.json_response({"revision": revision_of(new_body)})

    return handler

"""인증 강제.

**Hermes 의 API Server 는 인증을 미들웨어로 걸지 않는다.** 앱 미들웨어는 프로필
프리픽스·CORS·바디 제한·보안 헤더 넷뿐이고, `_check_auth()` 는 핸들러마다 개별
호출된다. 한 곳만 빠뜨리면 무인증 프로필 CRUD 가 열린다.

그래서 핸들러를 직접 등록하지 않고 이 래퍼를 통해서만 등록한다.
"""

import enum
import functools


class Scope(enum.Enum):
    """어느 자격으로 인증하는가.

    Hermes 의 `_expected_api_key()` 가 프로필 프리픽스를 보고 키를 고른다 —
    `/p/<name>/` 는 그 프로필의 키를, 프리픽스 없는 경로는 리스너 소유자(default)의
    키를 요구한다. 우리가 만드는 규칙이 아니라 물려받는 규칙이다.
    """

    DEFAULT = "default"
    PROFILE = "profile"


def require_auth(adapter, scope: Scope, handler):
    """핸들러를 인증 검사로 감싼다.

    scope 는 문서화 목적이다 — 실제 키 선택은 Hermes 의 미들웨어가 심은
    요청 컨텍스트를 보고 `_check_auth()` 가 한다.
    """

    @functools.wraps(handler)
    async def _wrapped(request):
        err = adapter._check_auth(request)
        if err is not None:
            return err
        return await handler(request)

    _wrapped.__deskrpg_scope__ = scope
    return _wrapped

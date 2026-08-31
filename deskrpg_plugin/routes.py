"""라우트 선언 테이블과 등록.

라우트를 흩어 놓지 않고 테이블 하나에 모은다. 등록은 테이블을 돌며 전부
require_auth 로 감싸므로, 핸들러를 빠뜨릴 자리가 없다.
"""

from .auth import Scope, require_auth
from . import identity as _identity
from . import profiles as _profiles
from . import config as _config

PLUGIN_VERSION = "0.1.0"

# (method, path, handler_name, scope)
ROUTES = [
    ("GET", "/deskrpg/info", "info", Scope.DEFAULT),
    ("GET", "/deskrpg/profiles", "list_profiles", Scope.DEFAULT),
    ("POST", "/deskrpg/profiles", "create_profile", Scope.DEFAULT),
    ("DELETE", "/deskrpg/profiles/{name}", "delete_profile", Scope.DEFAULT),
    ("GET", "/p/{profile}/deskrpg/identity", "get_identity", Scope.PROFILE),
    ("PUT", "/p/{profile}/deskrpg/identity", "put_identity", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/config", "get_config", Scope.PROFILE),
    ("PUT", "/p/{profile}/deskrpg/config", "put_config", Scope.PROFILE),
]

# Hermes 의 프로필 프리픽스 미들웨어는 `request.match_info.get("profile")` 로
# 인증 스코프를 정한다(gateway/platforms/api_server.py) — 우리가 만든 규칙이
# 아니라 물려받는 규칙이다(auth.py 의 Scope 주석과 같은 얘기). 그래서 경로의
# `{profile}` 이라는 정확한 이름이 Scope.PROFILE 과 실제로 맞물려 있는지는
# 매직 문자열에 의존한다(I-4) — 누가 `{profile_name}` 으로 바꾸면 라우트는
# 계속 200 을 내면서 인증만 조용히 default 키로 내려앉는다. import 시점에
# 바로 걸러 그 어긋남이 리뷰를 안 거치고 살아남을 수 없게 한다.
PROFILE_PATH_VAR = "{profile}"


def _assert_scope_matches_path():
    for method, path, handler_name, scope in ROUTES:
        has_profile_var = PROFILE_PATH_VAR in path
        if scope is Scope.PROFILE and not has_profile_var:
            raise AssertionError(
                f"{method} {path} ({handler_name}) 은 Scope.PROFILE 인데 "
                f"경로에 {PROFILE_PATH_VAR} 가 없다 — 인증이 default 키로 내려앉는다"
            )
        if scope is Scope.DEFAULT and has_profile_var:
            raise AssertionError(
                f"{method} {path} ({handler_name}) 은 Scope.DEFAULT 인데 "
                f"경로에 {PROFILE_PATH_VAR} 가 있다 — 프로필 키로 인증되어야 할 수 있다"
            )


_assert_scope_matches_path()

_HANDLERS = {
    "info": lambda api: _make_info(api),
    "list_profiles": lambda api: _profiles.list_handler(api),
    "create_profile": lambda api: _profiles.create_handler(api),
    "delete_profile": lambda api: _profiles.delete_handler(api),
    "get_identity": lambda api: _identity.get_handler(api),
    "put_identity": lambda api: _identity.put_handler(api),
    "get_config": lambda api: _config.get_handler(api),
    "put_config": lambda api: _config.put_handler(api),
}


def _make_info(api):
    from aiohttp import web

    async def handler(request):
        return web.json_response(
            {
                "plugin": "deskrpg",
                "version": PLUGIN_VERSION,
                "routes": [f"{m} {p}" for m, p, _h, _s in ROUTES],
            }
        )

    return handler


def handler_for(name, api):
    return _HANDLERS[name](api)


def attach(app, adapter, api) -> None:
    """테이블을 돌며 전부 인증으로 감싸 등록한다.

    app.router.add_* 를 여기 말고 어디서도 부르지 않는다 — 그래야 감싸지 않은
    핸들러가 생길 수 없다.
    """
    for method, path, handler_name, scope in ROUTES:
        handler = require_auth(adapter, scope, handler_for(handler_name, api))
        app.router.add_route(method, path, handler)
        # 프로필 프리픽스 미러는 Hermes 가 자기 라우트에만 만들어 주므로,
        # /p/{profile}/ 경로는 우리가 테이블에 그대로 적어 등록한다.

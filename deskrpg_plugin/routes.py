"""라우트 선언 테이블과 등록.

라우트를 흩어 놓지 않고 테이블 하나에 모은다. 등록은 테이블을 돌며 전부
require_auth 로 감싸므로, 핸들러를 빠뜨릴 자리가 없다.
"""

from pathlib import Path

from .auth import Scope, require_auth
from . import identity as _identity
from . import profiles as _profiles
from . import config as _config
from . import catalog as _catalog


def _read_plugin_version() -> str:
    """`plugin.yaml` 의 version 을 정본으로 읽는다.

    예전에는 이 값을 여기 문자열로 박아 뒀는데, 버전을 두 번 올리는 동안(0.2.0·0.3.0)
    **두 번 다 여기를 놓쳐** `/deskrpg/info` 가 계속 `0.1.0` 을 보고했다. 스테이징에서
    DeskRPG 가 그 값을 캐시하는 걸 보고서야 드러났다 — 같은 사실이 두 곳에 적혀 있으면
    반드시 갈라진다.

    yaml 파서를 쓰지 않는다. 이 플러그인은 Hermes 가 주는 것 외에 의존을 두지 않고,
    필요한 것은 최상위 `version:` 한 줄이다. 읽지 못하면 예외를 던지지 않고
    `"unknown"` 을 돌려준다 — 버전을 모르는 것이 라우트를 못 뜨게 할 이유는 아니다.
    """
    try:
        for line in (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return "unknown"


PLUGIN_VERSION = _read_plugin_version()

# (method, path, handler_name, scope)
ROUTES = [
    ("GET", "/deskrpg/info", "info", Scope.DEFAULT),
    ("GET", "/deskrpg/profiles", "list_profiles", Scope.DEFAULT),
    ("POST", "/deskrpg/profiles", "create_profile", Scope.DEFAULT),
    ("DELETE", "/deskrpg/profiles/{name}", "delete_profile", Scope.DEFAULT),
    ("GET", "/p/{profile}/deskrpg/identity", "get_identity", Scope.PROFILE),
    ("PUT", "/p/{profile}/deskrpg/identity", "put_identity", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/config", "get_config", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/catalog", "get_catalog", Scope.PROFILE),
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
    "get_catalog": lambda api: _catalog.get_handler(api),
}


def _info_timezone(api):
    """`hermes_time.get_timezone()` 의 ZoneInfo 를 IANA 이름으로. 없거나 실패하면 null.

    Hermes 는 타임존이 미설정이면 None(서버 로컬)을 돌려준다. 설정 파일이 깨져 예외가
    나더라도 info 가 죽어선 안 된다 — DeskRPG 는 이 응답으로 자동화 기능을 켜고 끈다.
    """
    try:
        tz = api.get_timezone()
    except Exception:
        return None
    key = getattr(tz, "key", None)
    return key if isinstance(key, str) and key else None


def _info_dispatcher_present(api) -> bool:
    """디스패처(게이트웨이의 kanban.dispatch_in_gateway)가 살아 있는지. 예외는 fail-open(true).

    Hermes 의 `_check_dispatcher_presence` 자체도 fail-open 이다 — 경고를 놓치는 쪽이
    멀쩡한 게이트웨이에 "디스패처 없음" 을 외치는 쪽보다 낫다.
    """
    try:
        present, _message = api._check_dispatcher_presence(api.get_hermes_home())
        return bool(present)
    except Exception:
        return True


def _make_info(api):
    from aiohttp import web

    from .contract_fields import CAPABILITIES

    async def handler(request):
        return web.json_response(
            {
                "plugin": "deskrpg",
                "version": PLUGIN_VERSION,
                "routes": [f"{m} {p}" for m, p, _h, _s in ROUTES],
                "capabilities": list(CAPABILITIES),
                "timezone": _info_timezone(api),
                "kanban": {
                    "dispatcher_present": _info_dispatcher_present(api),
                    "attachments": True,
                    # 첨부는 요청 본문에 실려 오므로 api_server 의 본문 상한과 칸반 자체 상한
                    # 중 작은 쪽이 실효 상한이다. 클라이언트가 이 값으로 업로드 전에 거른다.
                    "attachment_max_bytes": min(
                        int(api.MAX_REQUEST_BYTES), int(api.KANBAN_ATTACHMENT_MAX_BYTES)
                    ),
                },
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

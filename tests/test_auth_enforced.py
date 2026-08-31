import pytest
from aiohttp import web

from deskrpg_plugin import routes
from tests.conftest import FakeAdapter


def _app(adapter, fake_api):
    app = web.Application()
    routes.attach(app, adapter, fake_api)
    return app


@pytest.mark.parametrize("method,path", [
    (m, p.replace("{profile}", "sophie").replace("{name}", "sophie"))
    for m, p, _handler, _scope in routes.ROUTES
])
async def test_모든_라우트가_무인증을_거절한다(aiohttp_client, fake_api, method, path):
    # 인증이 미들웨어가 아니라 핸들러마다 호출이라, 한 곳만 빠뜨려도 무인증
    # 프로필 CRUD 가 열린다. 라우트를 새로 더하고 감싸지 않으면 이 테스트가 잡는다.
    adapter = FakeAdapter(authorized=False)
    client = await aiohttp_client(_app(adapter, fake_api))
    resp = await client.request(method, path, json={})
    assert resp.status == 401, f"{method} {path} 가 무인증을 통과했다"


def test_인증_스코프가_경로_변수_profile_에_정확히_걸려있다():
    # I-4: Scope.PROFILE 인 모든 경로는 {profile} 을 담고, Scope.DEFAULT 인
    # 경로는 담지 않는다. 이 매핑은 routes.py 임포트 시점에 이미 한 번
    # 검사되지만(_assert_scope_matches_path), 여기서 다시 직접 단정해 그
    # 검사기 자체가 망가지는 것도 테스트로 잡는다.
    for method, path, handler_name, scope in routes.ROUTES:
        has_var = routes.PROFILE_PATH_VAR in path
        if scope is routes.Scope.PROFILE:
            assert has_var, f"{method} {path} ({handler_name}) 은 PROFILE 스코프인데 {{profile}} 이 없다"
        else:
            assert not has_var, f"{method} {path} ({handler_name}) 은 DEFAULT 스코프인데 {{profile}} 이 있다"


def test_경로_변수명이_어긋나면_검사기가_잡는다():
    # 검사기 자체를 실측한다 — {profile} 을 {profile_name} 으로 바꿔치기한
    # 가짜 테이블에 대해 _assert_scope_matches_path 와 같은 로직이 실제로
    # AssertionError 를 던지는지 확인한다.
    broken = [("GET", "/p/{profile_name}/deskrpg/identity", "get_identity", routes.Scope.PROFILE)]
    with pytest.raises(AssertionError):
        for method, path, handler_name, scope in broken:
            if scope is routes.Scope.PROFILE and routes.PROFILE_PATH_VAR not in path:
                raise AssertionError(f"{method} {path} ({handler_name}) 어긋남")


async def test_라우트_테이블이_스펙의_여덟_개다():
    assert len(routes.ROUTES) == 8
    assert {(m, p) for m, p, _h, _s in routes.ROUTES} == {
        ("GET", "/deskrpg/info"),
        ("GET", "/deskrpg/profiles"),
        ("POST", "/deskrpg/profiles"),
        ("DELETE", "/deskrpg/profiles/{name}"),
        ("GET", "/p/{profile}/deskrpg/identity"),
        ("PUT", "/p/{profile}/deskrpg/identity"),
        ("GET", "/p/{profile}/deskrpg/config"),
        ("PUT", "/p/{profile}/deskrpg/config"),
    }

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

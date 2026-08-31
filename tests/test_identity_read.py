import pytest
from aiohttp import web

from deskrpg_plugin import identity, routes
from tests.conftest import FakeAdapter


def _client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return aiohttp_client(app)


def test_revision_은_본문이_같으면_같고_다르면_다르다():
    assert identity.revision_of("가") == identity.revision_of("가")
    assert identity.revision_of("가") != identity.revision_of("나")


def test_기본_템플릿_판정은_hermes_규칙을_쓴다(fake_api):
    # DEFAULT_SOUL_MD 와 정확히 같으면 손대지 않은 것이다.
    assert identity.is_default_template("You are Hermes Agent", fake_api) is True
    assert identity.is_default_template("나는 소피다", fake_api) is False


def test_앞뒤_공백만_다른_기본_템플릿도_기본으로_본다(fake_api):
    assert identity.is_default_template("  You are Hermes Agent\n", fake_api) is True


async def test_읽으면_본문과_판정과_revision_을_준다(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    d = fake_api.get_profile_dir("sophie")
    (d / "SOUL.md").write_text("나는 소피다", encoding="utf-8")

    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/identity")
    assert resp.status == 200
    body = await resp.json()
    assert body["body"] == "나는 소피다"
    assert body["isDefaultTemplate"] is False
    assert body["revision"] == identity.revision_of("나는 소피다")


async def test_없는_프로필은_404(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/nobody/deskrpg/identity")
    assert resp.status == 404


async def test_이름이_이상하면_400(aiohttp_client, fake_api):
    # 경로 탈출은 이름 검증에서 막는다. get_profile_dir 에 넘기기 전에 거른다.
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/..%2F..%2Fetc/deskrpg/identity")
    assert resp.status in (400, 404)

from aiohttp import web

from deskrpg_plugin import identity, routes
from tests.conftest import FakeAdapter


def _client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return aiohttp_client(app)


async def _seed(fake_api, body):
    fake_api.create_profile("sophie")
    (fake_api.get_profile_dir("sophie") / "SOUL.md").write_text(body, encoding="utf-8")


async def test_ifRevision_이_없으면_409(aiohttp_client, fake_api):
    # 읽지 않고 쓰는 것을 막는다. UI 가 반드시 먼저 읽어야 '이걸로 바꿉니다' 를
    # 보여줄 수 있다.
    await _seed(fake_api, "나는 소피다")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/identity", json={"body": "새 인격"})
    assert resp.status == 409


async def test_ifRevision_이_어긋나면_409_와_현재_본문(aiohttp_client, fake_api):
    await _seed(fake_api, "나는 소피다")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put(
        "/p/sophie/deskrpg/identity",
        json={"body": "새 인격", "ifRevision": "deadbeefdeadbeef"},
    )
    assert resp.status == 409
    payload = await resp.json()
    assert payload["body"] == "나는 소피다"
    assert payload["revision"] == identity.revision_of("나는 소피다")


async def test_ifRevision_이_맞으면_쓰고_새_revision_을_준다(aiohttp_client, fake_api):
    await _seed(fake_api, "나는 소피다")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put(
        "/p/sophie/deskrpg/identity",
        json={"body": "나는 새 소피다", "ifRevision": identity.revision_of("나는 소피다")},
    )
    assert resp.status == 200
    assert (await resp.json())["revision"] == identity.revision_of("나는 새 소피다")
    saved = (fake_api.get_profile_dir("sophie") / "SOUL.md").read_text(encoding="utf-8")
    assert saved == "나는 새 소피다"


async def test_body_가_없으면_400(aiohttp_client, fake_api):
    await _seed(fake_api, "나는 소피다")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put(
        "/p/sophie/deskrpg/identity",
        json={"ifRevision": identity.revision_of("나는 소피다")},
    )
    assert resp.status == 400


async def test_쓰기_전에_백업이_남는다(aiohttp_client, fake_api):
    await _seed(fake_api, "나는 소피다")
    client = await _client(aiohttp_client, fake_api)
    await client.put(
        "/p/sophie/deskrpg/identity",
        json={"body": "새 인격", "ifRevision": identity.revision_of("나는 소피다")},
    )
    d = fake_api.get_profile_dir("sophie")
    backups = list(d.glob("SOUL.md.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "나는 소피다"

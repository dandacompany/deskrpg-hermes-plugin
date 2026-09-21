import os

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


async def test_json_최상위가_객체가_아니면_400(aiohttp_client, fake_api):
    # request.json() 이 성공해도 payload 가 list/str/int/null 이면
    # payload.get(...) 이 AttributeError 를 던져 500 으로 새면 안 된다.
    await _seed(fake_api, "나는 소피다")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put(
        "/p/sophie/deskrpg/identity",
        json=[1, 2, 3],
    )
    assert resp.status == 400


async def test_파일이_없으면_빈_본문_revision으로_첫_생성이_허용된다(aiohttp_client, fake_api):
    # revision_of("") 는 상수라 GET 없이도 계산 가능하지만, 지울 인격이 없으므로
    # "읽지 않으면 못 쓴다" 의 의도된 예외다.
    fake_api.create_profile("sophie")
    (fake_api.get_profile_dir("sophie") / "SOUL.md").unlink()
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put(
        "/p/sophie/deskrpg/identity",
        json={"body": "처음 쓰는 인격", "ifRevision": identity.revision_of("")},
    )
    assert resp.status == 200
    assert (await resp.json())["revision"] == identity.revision_of("처음 쓰는 인격")
    saved = (fake_api.get_profile_dir("sophie") / "SOUL.md").read_text(encoding="utf-8")
    assert saved == "처음 쓰는 인격"


async def test_SOUL_MD_가_깨진_인코딩이면_PUT은_409이고_원본을_보존한다(aiohttp_client, fake_api):
    # I-2: put_handler 도 get 과 같은 500 이었다. 여기서는 revision 을 계산할
    # 방법이 없으니 config.put_handler 와 같은 논리로 거절한다 — 백업-후-
    # 덮어쓰기로 원본을 잃느니 409 로 멈춘다.
    fake_api.create_profile("sophie")
    d = fake_api.get_profile_dir("sophie")
    (d / "SOUL.md").write_bytes(b"\xff\xfe\x00broken")

    client = await _client(aiohttp_client, fake_api)
    resp = await client.put(
        "/p/sophie/deskrpg/identity",
        json={"body": "새 인격", "ifRevision": "whatever"},
    )
    assert resp.status == 409
    assert (d / "SOUL.md").read_bytes() == b"\xff\xfe\x00broken"
    assert list(d.glob("SOUL.md.bak-*")) == []


async def test_같은_초에_두_번_써도_백업_둘_다_남는다(aiohttp_client, fake_api):
    await _seed(fake_api, "첫 인격")
    client = await _client(aiohttp_client, fake_api)

    resp1 = await client.put(
        "/p/sophie/deskrpg/identity",
        json={"body": "둘째 인격", "ifRevision": identity.revision_of("첫 인격")},
    )
    assert resp1.status == 200
    resp2 = await client.put(
        "/p/sophie/deskrpg/identity",
        json={"body": "셋째 인격", "ifRevision": identity.revision_of("둘째 인격")},
    )
    assert resp2.status == 200

    d = fake_api.get_profile_dir("sophie")
    backups = list(d.glob("SOUL.md.bak-*"))
    assert len(backups) == 2
    contents = {b.read_text(encoding="utf-8") for b in backups}
    assert contents == {"첫 인격", "둘째 인격"}


async def test_SOUL_쓰기가_실패하면_원본이_온전하다(aiohttp_client, fake_api, monkeypatch):
    # SOUL.md 는 비밀이 아니지만, `write_text` 로 자리에서 잘라 쓰면 도중에 끊길 때
    # 반쯤 쓴 인격이 남는다 — revision 은 바뀐 것처럼 보이고 내용은 잘린 상태다.
    # 원자적 교체면 실패해도 원본이 손대지 않은 채 남는다.
    body = "나는 소피다"
    await _seed(fake_api, body)
    d = fake_api.get_profile_dir("sophie")
    path = d / "SOUL.md"
    revision = identity.revision_of(body)

    real_replace = os.replace

    def boom(src, dst):
        if str(dst) == str(path):
            raise OSError("디스크가 꽉 찼다")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", boom)

    client = await _client(aiohttp_client, fake_api)
    resp = await client.put(
        "/p/sophie/deskrpg/identity", json={"body": "새 인격", "ifRevision": revision}
    )
    assert resp.status >= 500

    assert path.read_text(encoding="utf-8") == body
    assert [p.name for p in d.iterdir() if p.name.startswith(".SOUL.md.")] == []

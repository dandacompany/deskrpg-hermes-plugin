"""`GET /deskrpg/info` — 0.6.0 이 더한 capabilities·timezone·kanban 필드."""

from aiohttp import web

from deskrpg_plugin import _hermes_api, routes
from deskrpg_plugin.contract_fields import PLUGIN_INFO_KANBAN_KEYS, PLUGIN_INFO_KEYS
from tests.conftest import FakeAdapter


async def _info(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    client = await aiohttp_client(app)
    resp = await client.get("/deskrpg/info")
    assert resp.status == 200
    return await resp.json()


def test_fake_api_가_REQUIRED_의_모든_심볼을_갖는다(fake_api):
    # 핸들러가 `api.<name>` 으로 부르는 이름이 실제 Hermes 에는 있는데 fake 에 없으면
    # 테스트에서만 AttributeError 로 죽어 회귀를 못 잡는다. 목록을 넓히면 여기서 걸린다.
    missing = [name for name in _hermes_api.REQUIRED if not hasattr(fake_api, name)]
    assert not missing, f"fake_api 에 없는 심볼: {missing}"


async def test_info_가_계약_필드를_전부_낸다(aiohttp_client, fake_api):
    body = await _info(aiohttp_client, fake_api)
    assert set(body) == PLUGIN_INFO_KEYS
    assert body["plugin"] == "deskrpg"
    assert body["version"] == routes.PLUGIN_VERSION
    # fake_api 는 스웜 심볼을 갖춘 빌드를 흉내 낸다 — capability 에 "swarm" 이 붙는다.
    assert body["capabilities"] == ["kanban", "cron", "events", "swarm"]
    assert body["timezone"] == "Asia/Seoul"
    assert set(body["kanban"]) == PLUGIN_INFO_KANBAN_KEYS
    assert body["kanban"] == {
        "dispatcher_present": True,
        "attachments": True,
        "attachment_max_bytes": 10_000_000,
    }
    # 기존 필드는 그대로다 — DeskRPG 구버전 파서가 routes 를 읽는다.
    assert "GET /deskrpg/info" in body["routes"]


async def test_타임존이_없으면_서버_로컬_이름으로_폴백한다(aiohttp_client, fake_api, monkeypatch):
    # Hermes 는 타임존 미설정이면 None(서버 로컬)을 돌려준다. 그래도 서버가 어느 지역
    # 시각으로 도는지는 알 수 있으므로 null 대신 그 이름을 낸다.
    fake_api.get_timezone = lambda: None
    monkeypatch.setenv("TZ", "Europe/Berlin")
    body = await _info(aiohttp_client, fake_api)
    assert body["timezone"] == "Europe/Berlin"


async def test_타임존_조회가_던져도_폴백한다(aiohttp_client, fake_api, monkeypatch):
    # 설정 파일이 깨졌다고 info 가 500 이면 DeskRPG 는 자동화를 통째로 끈다.
    def boom():
        raise RuntimeError("config broken")

    fake_api.get_timezone = boom
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    body = await _info(aiohttp_client, fake_api)
    assert body["timezone"] == "Asia/Tokyo"


async def test_서버_로컬_이름도_못_알아내면_null(aiohttp_client, fake_api, monkeypatch):
    fake_api.get_timezone = lambda: None
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(routes.os, "readlink", _raise_oserror)
    body = await _info(aiohttp_client, fake_api)
    assert body["timezone"] is None


async def test_TZ_가_IANA_이름이_아니면_무시한다(aiohttp_client, fake_api, monkeypatch):
    # `TZ=KST-9` 같은 POSIX 표기는 IANA 이름이 아니다 — 계약은 IANA 이름만 받는다.
    fake_api.get_timezone = lambda: None
    monkeypatch.setenv("TZ", "KST-9")
    monkeypatch.setattr(routes.os, "readlink", lambda path: "/usr/share/zoneinfo/Asia/Seoul")
    body = await _info(aiohttp_client, fake_api)
    assert body["timezone"] == "Asia/Seoul"


def _raise_oserror(path):
    raise OSError("no symlink")


async def test_첨부_상한은_두_상한_중_작은_쪽이다(aiohttp_client, fake_api):
    fake_api.MAX_REQUEST_BYTES = 100
    fake_api.KANBAN_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
    body = await _info(aiohttp_client, fake_api)
    assert body["kanban"]["attachment_max_bytes"] == 100

    fake_api.MAX_REQUEST_BYTES = 10_000_000
    fake_api.KANBAN_ATTACHMENT_MAX_BYTES = 50
    body = await _info(aiohttp_client, fake_api)
    assert body["kanban"]["attachment_max_bytes"] == 50


async def test_디스패처가_없으면_false(aiohttp_client, fake_api):
    fake_api._check_dispatcher_presence = lambda hermes_home=None: (False, "no gateway is running")
    body = await _info(aiohttp_client, fake_api)
    assert body["kanban"]["dispatcher_present"] is False


async def test_디스패처_확인이_던지면_fail_open_true(aiohttp_client, fake_api):
    def boom(hermes_home=None):
        raise OSError("probe failed")

    fake_api._check_dispatcher_presence = boom
    body = await _info(aiohttp_client, fake_api)
    assert body["kanban"]["dispatcher_present"] is True


async def test_디스패처_확인에_hermes_home_을_넘긴다(aiohttp_client, fake_api):
    # 프로필별 HERMES_HOME 으로 스코프하지 않으면 멀쩡한 프로필 게이트웨이에 "없음" 을 외친다.
    seen = []
    fake_api._check_dispatcher_presence = lambda hermes_home=None: (seen.append(hermes_home), (True, ""))[1]
    await _info(aiohttp_client, fake_api)
    assert seen == [fake_api.get_hermes_home()]

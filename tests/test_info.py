"""`GET /deskrpg/info` — 0.6.0 이 더한 capabilities·timezone·kanban 필드."""

import asyncio
import time

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


def _client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return aiohttp_client(app)


def test_fake_api_가_REQUIRED_의_모든_심볼을_갖는다(fake_api):
    # 핸들러가 `api.<name>` 으로 부르는 이름이 실제 Hermes 에는 있는데 fake 에 없으면
    # 테스트에서만 AttributeError 로 죽어 회귀를 못 잡는다. 목록을 넓히면 여기서 걸린다.
    missing = [name for name in _hermes_api.REQUIRED if not hasattr(fake_api, name)]
    assert not missing, f"fake_api 에 없는 심볼: {missing}"


async def test_info_가_계약_필드를_전부_낸다(aiohttp_client, fake_api, monkeypatch):
    from deskrpg_plugin import worker_launch

    launch = {"ok": True, "reason": None, "hermes_bin": None, "launcher": None}
    monkeypatch.setattr(worker_launch, "report", lambda: dict(launch))
    body = await _info(aiohttp_client, fake_api)
    assert set(body) == PLUGIN_INFO_KEYS
    assert body["plugin"] == "deskrpg"
    assert body["version"] == routes.PLUGIN_VERSION
    # fake_api 는 스웜·피커 심볼을 모두 갖춘 빌드를 흉내 낸다 — capability 에 다 붙는다.
    assert body["capabilities"] == [
        "kanban", "cron", "events", "event_cursor_handoff", "artifacts", "kanban_views", "card_proposals", "worker_plugin", "kanban_attachment_list", "board_archive", "kanban_task_events", "kanban_run_events", "profile_key_issue", "session_sources", "ask_user", "swarm",
        "profile_toolsets", "profile_skills", "profile_skill_admin", "profile_skill_read", "profile_skill_edit", "profile_skill_hub", "profile_curator", "profile_learning_graph", "profile_mcp_admin", "profile_approval_policy", "profile_clone", "profile_provider_keys",
        "profile_oauth", "profile_tool_providers", "initial_status",
    ]
    assert "artifacts" in body["capabilities"] and isinstance(body["artifact_max_bytes"], int)
    # 카드 제안은 Hermes 빌드와 무관하게 이 플러그인이 늘 싣는다 — 구버전 플러그인에는 없으므로
    # DeskRPG 는 이 문자열의 유무로 기능을 판별한다.
    assert "card_proposals" in body["capabilities"]
    assert body["timezone"] == "Asia/Seoul"
    # The running plugin commit, or None when the checkout cannot be read.
    assert set(body["install"]) == {"commit"}
    assert body["install"]["commit"] is None or len(body["install"]["commit"]) == 40
    assert set(body["kanban"]) == PLUGIN_INFO_KANBAN_KEYS
    assert body["kanban"] == {
        "dispatcher_present": True,
        "attachments": True,
        "attachment_max_bytes": 10_000_000,
        # No profiles yet, propagation off by default.
        "review_hooks": {"propagation": False, "profiles_without_plugin": []},
        "worker_launch": launch,
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


async def test_대시보드_공개_주소가_있으면_dashboard_url_로_낸다(aiohttp_client, fake_api, monkeypatch):
    monkeypatch.delenv("HERMES_DASHBOARD", raising=False)
    fake_api.resolve_public_url = lambda: "https://deskrpg-hermes.srv1.hstgr.cloud"
    body = await _info(aiohttp_client, fake_api)
    assert body["dashboard_url"] == "https://deskrpg-hermes.srv1.hstgr.cloud"


async def test_컨테이너가_대시보드를_끄면_주소가_있어도_null(aiohttp_client, fake_api, monkeypatch):
    # compose 는 비밀번호가 없으면 HERMES_DASHBOARD 를 빈 값으로 둔다 — 공개 주소 환경변수는 남아 있어도
    # 대시보드는 뜨지 않는다. 죽은 링크를 보내지 않는다.
    monkeypatch.setenv("HERMES_DASHBOARD", "")
    fake_api.resolve_public_url = lambda: "https://deskrpg-hermes.srv1.hstgr.cloud"
    body = await _info(aiohttp_client, fake_api)
    assert body["dashboard_url"] is None


async def test_컨테이너가_대시보드를_켜면_주소를_낸다(aiohttp_client, fake_api, monkeypatch):
    monkeypatch.setenv("HERMES_DASHBOARD", "true")
    fake_api.resolve_public_url = lambda: "https://h.example"
    body = await _info(aiohttp_client, fake_api)
    assert body["dashboard_url"] == "https://h.example"


async def test_공개_주소가_없으면_null(aiohttp_client, fake_api, monkeypatch):
    monkeypatch.delenv("HERMES_DASHBOARD", raising=False)
    fake_api.resolve_public_url = lambda: ""
    body = await _info(aiohttp_client, fake_api)
    assert body["dashboard_url"] is None


async def test_구버전_Hermes_라_주소_해석기가_없으면_null(aiohttp_client, fake_api, monkeypatch):
    monkeypatch.delenv("HERMES_DASHBOARD", raising=False)
    fake_api.resolve_public_url = None
    body = await _info(aiohttp_client, fake_api)
    assert body["dashboard_url"] is None


async def test_주소_해석이_던져도_info_는_200_이고_null(aiohttp_client, fake_api, monkeypatch):
    monkeypatch.delenv("HERMES_DASHBOARD", raising=False)

    def boom():
        raise RuntimeError("config broken")

    fake_api.resolve_public_url = boom
    body = await _info(aiohttp_client, fake_api)
    assert body["dashboard_url"] is None


async def test_info_의_artifact_max_bytes_는_업로드_상한이라_요청_본문_상한에_묶인다(aiohttp_client, fake_api, monkeypatch):
    monkeypatch.delenv("HERMES_DESKRPG_ARTIFACT_MAX_BYTES", raising=False)
    fake_api.MAX_REQUEST_BYTES = 10
    body = await _info(aiohttp_client, fake_api)
    assert body["artifact_max_bytes"] == 10


# ---------------------------------------------------------------------------
# 0.9.0 — 직원 설정 피커 capability: 심볼이 있을 때만 광고한다
# ---------------------------------------------------------------------------


async def test_피커_능력은_심볼이_있을_때만_광고한다(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    caps = (await (await client.get("/deskrpg/info")).json())["capabilities"]
    assert {"profile_toolsets", "profile_skills", "profile_clone"} <= set(caps)

    fake_api._find_all_skills = None
    fake_api._get_platform_tools = None
    fake_api.PROVIDER_REGISTRY = None
    client = await _client(aiohttp_client, fake_api)
    caps = (await (await client.get("/deskrpg/info")).json())["capabilities"]
    assert not {"profile_toolsets", "profile_skills", "profile_clone"} & set(caps)


async def test_initial_status_능력은_Hermes_가_받을_때만_광고한다(aiohttp_client, fake_api):
    """버전이 아니라 `create_task` 시그니처를 본다 — 받지 못하는 빌드에서 광고하면
    화면이 승인 관문을 켜고, 카드가 `running` 으로 생겨 관문이 통째로 빠진다."""
    client = await _client(aiohttp_client, fake_api)
    caps = (await (await client.get("/deskrpg/info")).json())["capabilities"]
    assert "initial_status" in caps

    def old_create_task(conn, *, title, body=None, **kwargs):  # initial_status 없음
        raise AssertionError("불리면 안 된다")

    # 플러그인은 `api.create_task` 를 본다 — kanban_db 심볼이 api 로 위임된 이름이다.
    fake_api.create_task = old_create_task
    client = await _client(aiohttp_client, fake_api)
    caps = (await (await client.get("/deskrpg/info")).json())["capabilities"]
    assert "initial_status" not in caps


def test_스킬_관리_심볼이_다_있으면_profile_skill_admin_을_광고한다(fake_api):
    from deskrpg_plugin.contract_fields import capabilities

    assert "profile_skill_admin" in capabilities(fake_api)


def test_스킬_관리_심볼이_하나라도_없으면_광고하지_않는다(fake_api):
    from deskrpg_plugin.contract_fields import capabilities

    fake_api.build_learning_graph = None
    assert "profile_skill_admin" not in capabilities(fake_api)


async def test_info_carries_a_capability_fingerprint_and_start_time(aiohttp_client, fake_api):
    from deskrpg_plugin.contract_fields import capabilities_fingerprint

    body = await _info(aiohttp_client, fake_api)
    assert body["capabilities_fingerprint"] == capabilities_fingerprint(body["capabilities"])
    assert isinstance(body["started_at"], int) and body["started_at"] > 1_700_000_000


def test_fingerprint_ignores_order_and_changes_with_the_set():
    from deskrpg_plugin.contract_fields import capabilities_fingerprint

    base = capabilities_fingerprint(["kanban", "swarm", "cron"])
    assert base == capabilities_fingerprint(["cron", "kanban", "swarm"])
    assert len(base) == 16
    assert base != capabilities_fingerprint(["kanban", "cron"])


async def test_info_fingerprint_follows_a_core_that_loses_a_symbol(aiohttp_client, fake_api, monkeypatch):
    # Same plugin version, different core: the fingerprint must move so DeskRPG can refresh its cache.
    before = (await _info(aiohttp_client, fake_api))["capabilities_fingerprint"]
    monkeypatch.setattr(fake_api, "create_swarm", None, raising=False)
    after = await _info(aiohttp_client, fake_api)
    assert "swarm" not in after["capabilities"]
    assert after["capabilities_fingerprint"] != before


async def _with_loop_lag(coro, interval=0.02):
    """Run `coro` while a ticker measures how late the event loop wakes it — the loop is blocked by that much."""
    lags, done = [], asyncio.Event()

    async def tick():
        while not done.is_set():
            start = time.monotonic()
            await asyncio.sleep(interval)
            lags.append(time.monotonic() - start - interval)

    ticker = asyncio.create_task(tick())
    try:
        result = await coro
    finally:
        done.set()
        await ticker
    return result, max(lags, default=0.0)


async def test_dispatcher_presence_is_answered_in_process_without_querying_the_gateway(aiohttp_client, fake_api):
    # The route is served by this gateway, so it is alive; asking its own control pipe from its own event loop
    # deadlocked a Windows gateway (the pipe read has no timeout there) until the loop watchdog killed it.
    calls = []
    fake_api._check_dispatcher_presence = lambda hermes_home=None: calls.append(hermes_home) or (False, "")
    body = await _info(aiohttp_client, fake_api)
    assert body["kanban"]["dispatcher_present"] is True
    assert calls == []


async def test_dispatcher_is_absent_when_the_gateway_does_not_run_it(aiohttp_client, fake_api):
    fake_api.load_config = lambda *a, **k: {"kanban": {"dispatch_in_gateway": False}}
    body = await _info(aiohttp_client, fake_api)
    assert body["kanban"]["dispatcher_present"] is False


async def test_a_slow_probe_neither_blocks_the_loop_nor_holds_info_past_its_cutoff(aiohttp_client, fake_api, monkeypatch):
    from deskrpg_plugin import worker_launch

    monkeypatch.setattr(routes, "INFO_PROBE_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(worker_launch, "report", lambda: time.sleep(1.5) or {"ok": True})
    client = await _client(aiohttp_client, fake_api)

    started = time.monotonic()
    resp, lag = await _with_loop_lag(client.get("/deskrpg/info"))
    body = await resp.json()
    assert time.monotonic() - started < 1.0, "info waits for a slow probe only up to its cutoff"
    assert body["kanban"]["worker_launch"] is None, "not known yet"
    assert lag < 0.2, f"event loop blocked for {lag:.2f}s"


async def test_a_probe_that_finishes_late_fills_the_cache_for_the_next_call(aiohttp_client, fake_api, monkeypatch):
    from deskrpg_plugin import worker_launch

    calls = []
    monkeypatch.setattr(routes, "INFO_PROBE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(worker_launch, "report", lambda: calls.append(1) or time.sleep(0.3) or {"ok": True})
    client = await _client(aiohttp_client, fake_api)

    first = await (await client.get("/deskrpg/info")).json()
    assert first["kanban"]["worker_launch"] is None
    await asyncio.sleep(0.4)
    second = await (await client.get("/deskrpg/info")).json()
    assert second["kanban"]["worker_launch"] == {"ok": True}
    assert calls == [1], "a cached answer is reused, not recomputed"


async def test_a_blocking_cheap_part_of_info_does_not_block_the_loop(aiohttp_client, fake_api):
    # Timezone, dashboard URL and the rest read files and config — they run off the loop too.
    fake_api.get_timezone = lambda: time.sleep(0.5)
    client = await _client(aiohttp_client, fake_api)
    resp, lag = await _with_loop_lag(client.get("/deskrpg/info"))
    assert resp.status == 200
    assert lag < 0.2, f"event loop blocked for {lag:.2f}s"

import os
import stat

import pytest
import yaml
from aiohttp import web

from deskrpg_plugin import config, routes
from tests.conftest import FakeAdapter


def _client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return aiohttp_client(app)


def _seed(fake_api, data):
    fake_api.create_profile("sophie")
    p = fake_api.get_profile_dir("sophie") / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


async def test_읽으면_허용된_키만_준다(aiohttp_client, fake_api):
    _seed(fake_api, {"model": {"provider": "openai-codex", "default": "gpt-5.6-sol"},
                     "memory": {"provider": "agentmemory"}})
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/config")).json()
    assert body["model"] == "gpt-5.6-sol"
    assert body["provider"] == "openai-codex"
    assert "memory" not in body


async def test_허용목록_밖의_키는_거절한다(aiohttp_client, fake_api):
    # 임의 YAML 을 쓰지 않는다 — 잘못된 키 하나가 프로필을 못 뜨게 만든다.
    _seed(fake_api, {"model": {"provider": "openai-codex", "default": "gpt-5.6-sol"}})
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json={"memory": {"provider": "x"}})
    assert resp.status == 400


async def test_쓰면_백업이_남고_재시작_가능성을_알린다(aiohttp_client, fake_api):
    path = _seed(fake_api, {"model": {"provider": "openai-codex", "default": "gpt-5.6-sol"}})
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json={"model": "gpt-5.6-terra"})
    assert resp.status == 200
    payload = await resp.json()
    assert payload["applied"] == ["model"]
    assert payload["restartMayBeRequired"] is True

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["model"]["default"] == "gpt-5.6-terra"
    assert len(list(path.parent.glob("config.yaml.bak-*"))) == 1


async def test_다른_키는_보존된다(aiohttp_client, fake_api):
    path = _seed(fake_api, {"model": {"provider": "openai-codex", "default": "a"},
                            "memory": {"provider": "agentmemory"}})
    client = await _client(aiohttp_client, fake_api)
    await client.put("/p/sophie/deskrpg/config", json={"model": "b"})
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["memory"] == {"provider": "agentmemory"}


async def test_설정이_없는_프로필은_전부_null(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/config")
    assert resp.status == 200
    body = await resp.json()
    assert body["model"] is None
    assert body["provider"] is None
    assert body["toolsets"] is None
    assert "unreadable" not in body


async def test_PUT_본문이_객체가_아니면_400(aiohttp_client, fake_api):
    # request.json() 이 성공해도 payload 가 list/str/int/null 이면
    # payload 를 set() 하거나 in 연산할 때 500 으로 새면 안 된다.
    _seed(fake_api, {"model": {"provider": "openai-codex", "default": "a"}})
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json=["model", "b"])
    assert resp.status == 400


async def test_PUT_빈_객체는_400(aiohttp_client, fake_api):
    _seed(fake_api, {"model": {"provider": "openai-codex", "default": "a"}})
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json={})
    assert resp.status == 400


async def test_YAML_문법이_깨졌으면_GET은_500이_아니라_unreadable_플래그(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    p = fake_api.get_profile_dir("sophie") / "config.yaml"
    p.write_text("model: [unterminated\n  - broken", encoding="utf-8")

    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/config")
    assert resp.status == 200
    body = await resp.json()
    assert body["model"] is None
    assert body["provider"] is None
    assert body["toolsets"] is None
    assert body["unreadable"] is True


async def test_YAML_최상위가_dict가_아니면_GET은_unreadable_플래그(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    p = fake_api.get_profile_dir("sophie") / "config.yaml"
    p.write_text(yaml.safe_dump(["not", "a", "mapping"]), encoding="utf-8")

    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/config")
    assert resp.status == 200
    body = await resp.json()
    assert body["unreadable"] is True


async def test_YAML이_깨졌으면_PUT은_거절하고_원본을_보존한다(aiohttp_client, fake_api):
    # 백업-후-덮어쓰기를 하면 "성공"을 보고하면서 망가진 원본을 영영 잃는다 —
    # 여기서는 쓰기를 아예 거절해야 한다.
    fake_api.create_profile("sophie")
    p = fake_api.get_profile_dir("sophie") / "config.yaml"
    broken = "model: [unterminated\n  - broken"
    p.write_text(broken, encoding="utf-8")

    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json={"model": "gpt-5.6-terra"})
    assert resp.status == 409

    # 원본이 그대로다 — 백업도, 덮어쓰기도 일어나지 않았다.
    assert p.read_text(encoding="utf-8") == broken
    assert list(p.parent.glob("config.yaml.bak-*")) == []


async def test_인코딩이_깨진_config는_GET에서_unreadable(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    p = fake_api.get_profile_dir("sophie") / "config.yaml"
    p.write_bytes(b"\xff\xfe\x00broken")

    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/config")
    assert resp.status == 200
    body = await resp.json()
    assert body["unreadable"] is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root 는 파일 권한을 무시하므로 이 테스트가 성립하지 않는다")
async def test_읽기_권한이_없는_config는_GET에서_unreadable(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    p = fake_api.get_profile_dir("sophie") / "config.yaml"
    p.write_text(yaml.safe_dump({"model": {"default": "a"}}), encoding="utf-8")
    p.chmod(0)
    try:
        client = await _client(aiohttp_client, fake_api)
        resp = await client.get("/p/sophie/deskrpg/config")
        assert resp.status == 200
        body = await resp.json()
        assert body["unreadable"] is True
    finally:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)  # tmp_path 정리가 지울 수 있도록 복구


async def test_ALLOWED_KEYS_는_문서화된_일곱_키다():
    assert config.ALLOWED_KEYS == frozenset(
        {
            "model",
            "provider",
            "toolsets",
            "reasoning_effort",
            "enabledToolsets",
            "disabledSkills",
            "clearBaseUrl",
        }
    )


# 허용목록은 키만 막고 값의 타입은 열어 두면 목적이 무색해진다 — model.default 에
# dict/list/int 가 그대로 들어가면 잘못된 키를 막은 것과 같은 방식으로 Hermes 가
# 그 프로필을 못 띄운다. 아래 네 입력은 모두 400 이고, config.yaml 은 원본 바이트
# 그대로 남아야 한다(백업도 쓰기도 일어나지 않는다).
@pytest.mark.parametrize(
    "payload",
    [
        {"model": {"evil": 1}},
        {"model": [1, 2]},
        {"model": 123},
        {"toolsets": "not-a-list"},
    ],
)
async def test_허용된_키여도_값_타입이_틀리면_400이고_원본이_그대로다(aiohttp_client, fake_api, payload):
    path = _seed(fake_api, {"model": {"provider": "openai-codex", "default": "gpt-5.6-sol"}})
    original = path.read_bytes()
    client = await _client(aiohttp_client, fake_api)

    resp = await client.put("/p/sophie/deskrpg/config", json=payload)
    assert resp.status == 400

    assert path.read_bytes() == original
    assert list(path.parent.glob("config.yaml.bak-*")) == []


async def test_toolsets_원소가_문자열이_아니면_400(aiohttp_client, fake_api):
    path = _seed(fake_api, {"model": {"provider": "openai-codex", "default": "gpt-5.6-sol"}})
    original = path.read_bytes()
    client = await _client(aiohttp_client, fake_api)

    resp = await client.put("/p/sophie/deskrpg/config", json={"toolsets": ["ok", 1]})
    assert resp.status == 400
    assert path.read_bytes() == original


async def test_빈_문자열_model은_400(aiohttp_client, fake_api):
    # 저장은 되지만 의미 없는 값(빈 문자열)을 허용하면, 다음 GET 이나 실제
    # 프로필 기동 시점에 더 알기 어려운 형태로 터진다 — 여기서 바로 거절한다.
    path = _seed(fake_api, {"model": {"provider": "openai-codex", "default": "gpt-5.6-sol"}})
    original = path.read_bytes()
    client = await _client(aiohttp_client, fake_api)

    resp = await client.put("/p/sophie/deskrpg/config", json={"model": "   "})
    assert resp.status == 400
    assert path.read_bytes() == original


async def test_기존_model이_스칼라면_PUT은_409이고_백업_찌꺼기가_없다(aiohttp_client, fake_api):
    # I-1: dict(data.get("model") or {}) 가 스칼라에서 ValueError 를 던져 500 이
    # 나가고, 그 전에 백업(:163-171)이 이미 만들어져 찌꺼기가 남았었다.
    # get_handler(:110-112) 는 이미 이 상황을 dict 로 치환해 방어하는데
    # put_handler 만 놓쳤다 — 이제는 병합 시도 전에 409 로 거절하고, 백업은
    # 아예 만들어지지 않는다(상태 코드뿐 아니라 백업 개수까지 고정한다).
    path = _seed(fake_api, {"model": "gpt-5", "memory": "on"})
    original = path.read_bytes()
    client = await _client(aiohttp_client, fake_api)

    resp = await client.put("/p/sophie/deskrpg/config", json={"model": "claude"})
    assert resp.status == 409

    assert path.read_bytes() == original
    assert list(path.parent.glob("config.yaml.bak-*")) == []  # 백업 찌꺼기 0개


async def test_기존_model이_리스트여도_PUT은_409이고_백업_찌꺼기가_없다(aiohttp_client, fake_api):
    path = _seed(fake_api, {"model": ["gpt-5", "claude"]})
    original = path.read_bytes()
    client = await _client(aiohttp_client, fake_api)

    resp = await client.put("/p/sophie/deskrpg/config", json={"provider": "openai-codex"})
    assert resp.status == 409

    assert path.read_bytes() == original
    assert list(path.parent.glob("config.yaml.bak-*")) == []


async def test_문자열_model과_문자열_리스트_toolsets는_여전히_200(aiohttp_client, fake_api):
    path = _seed(fake_api, {"model": {"provider": "openai-codex", "default": "a"}})
    client = await _client(aiohttp_client, fake_api)

    resp = await client.put(
        "/p/sophie/deskrpg/config",
        json={"model": "gpt-5.6-terra", "provider": "openai-codex", "toolsets": ["fs", "web"]},
    )
    assert resp.status == 200
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["model"]["default"] == "gpt-5.6-terra"
    assert saved["model"]["provider"] == "openai-codex"
    assert saved["toolsets"] == ["fs", "web"]


async def test_reasoning_effort_는_최상위_키로_저장된다(aiohttp_client, fake_api):
    # model 블록 안이 아니다 — agent/auxiliary_client.py:5555 가
    # config.get("reasoning_effort") 로 최상위에서 읽는다.
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/noah/deskrpg/config", json={"reasoning_effort": "high"})
    assert resp.status == 200

    import yaml

    data = yaml.safe_load((fake_api.get_profile_dir("noah") / "config.yaml").read_text())
    assert data["reasoning_effort"] == "high"
    assert "reasoning_effort" not in (data.get("model") or {})


async def test_빈_reasoning_effort_는_키를_지운다(aiohttp_client, fake_api):
    # 빈 값을 남기면 Hermes 가 "지정됨" 으로 읽을지 "미지정" 으로 읽을지 확실하지 않다.
    fake_api.create_profile("noah")
    (fake_api.get_profile_dir("noah") / "config.yaml").write_text(
        "reasoning_effort: high\nmemory: enabled\n", encoding="utf-8"
    )
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/noah/deskrpg/config", json={"reasoning_effort": ""})
    assert resp.status == 200

    import yaml

    data = yaml.safe_load((fake_api.get_profile_dir("noah") / "config.yaml").read_text())
    assert "reasoning_effort" not in data
    assert data["memory"] == "enabled"  # 남의 키를 건드리지 않았다


async def test_허용되지_않은_effort_값은_거절한다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/noah/deskrpg/config", json={"reasoning_effort": "turbo"})
    assert resp.status == 400


async def test_GET_은_reasoning_effort_를_최상위에서_읽어_돌려준다(aiohttp_client, fake_api):
    fake_api.create_profile("noah")
    (fake_api.get_profile_dir("noah") / "config.yaml").write_text(
        "reasoning_effort: xhigh\n", encoding="utf-8"
    )
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/noah/deskrpg/config")).json()
    assert body["reasoning_effort"] == "xhigh"


async def test_GET_은_읽기_실패시에도_reasoning_effort_필드를_준다(aiohttp_client, fake_api):
    # 화면이 필드 부재와 "값 없음" 을 구분하지 못하면 폼이 조용히 비어 버린다.
    fake_api.create_profile("noah")
    (fake_api.get_profile_dir("noah") / "config.yaml").write_text("[", encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/noah/deskrpg/config")).json()
    assert body["unreadable"] is True
    assert body["reasoning_effort"] is None


async def test_enabledToolsets_는_세_플랫폼에_같은_목록을_쓴다(aiohttp_client, fake_api):
    path = _seed(fake_api, {"platform_toolsets": {"telegram": ["web"], "cli": ["file"]}})
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json={"enabledToolsets": ["web", "file", "web"]})
    assert resp.status == 200
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["platform_toolsets"]
    assert saved["api_server"] == saved["cron"] == saved["cli"] == ["file", "web"]
    assert saved["telegram"] == ["web"]  # 다른 플랫폼은 건드리지 않는다


async def test_빈_enabledToolsets_는_명시적_없음으로_저장된다(aiohttp_client, fake_api):
    path = _seed(fake_api, {})
    client = await _client(aiohttp_client, fake_api)
    assert (await client.put("/p/sophie/deskrpg/config", json={"enabledToolsets": []})).status == 200
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["platform_toolsets"]["api_server"] == []


async def test_모르는_툴셋_이름은_거절하고_파일을_건드리지_않는다(aiohttp_client, fake_api):
    path = _seed(fake_api, {"model": {"default": "m"}})
    before = path.read_text(encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json={"enabledToolsets": ["web", "nope", "discord"]})
    assert resp.status == 400
    assert "nope" in resp.reason and "discord" in resp.reason
    assert path.read_text(encoding="utf-8") == before
    assert not list(path.parent.glob("config.yaml.bak-*"))


async def test_disabledSkills_는_skills_disabled_에_쓰고_다른_키를_보존한다(aiohttp_client, fake_api):
    path = _seed(fake_api, {"skills": {"platform_disabled": {"telegram": ["pdf"]}, "external_dirs": ["~/x"]}})
    client = await _client(aiohttp_client, fake_api)
    assert (await client.put("/p/sophie/deskrpg/config", json={"disabledSkills": ["xlsx", "pdf"]})).status == 200
    skills = yaml.safe_load(path.read_text(encoding="utf-8"))["skills"]
    assert skills["disabled"] == ["pdf", "xlsx"]
    assert skills["platform_disabled"] == {"telegram": ["pdf"]}
    assert skills["external_dirs"] == ["~/x"]


@pytest.mark.parametrize("names", [["hermes-agent"], ["ghost-skill"]])
async def test_필수_스킬과_모르는_스킬은_끌_수_없다(aiohttp_client, fake_api, names):
    _seed(fake_api, {})
    client = await _client(aiohttp_client, fake_api)
    assert (await client.put("/p/sophie/deskrpg/config", json={"disabledSkills": names})).status == 400


@pytest.mark.parametrize("key,bad", [("enabledToolsets", "web"), ("enabledToolsets", [1]), ("disabledSkills", {"a": 1})])
async def test_목록이_아니면_400(aiohttp_client, fake_api, key, bad):
    _seed(fake_api, {})
    client = await _client(aiohttp_client, fake_api)
    assert (await client.put("/p/sophie/deskrpg/config", json={key: bad})).status == 400


async def test_platform_toolsets_가_매핑이_아니면_409(aiohttp_client, fake_api):
    path = _seed(fake_api, {"platform_toolsets": ["web"]})
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json={"enabledToolsets": ["web"]})
    assert resp.status == 409
    assert (await resp.json())["error"] == "config_unreadable"
    assert not list(path.parent.glob("config.yaml.bak-*"))


async def test_읽으면_enabledToolsets_와_disabledSkills_를_준다(aiohttp_client, fake_api):
    _seed(fake_api, {"platform_toolsets": {"api_server": ["web"]}, "skills": {"disabled": ["pdf"]}})
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/config")).json()
    assert body["enabledToolsets"] == ["web"]
    assert body["disabledSkills"] == ["pdf"]


async def test_저장한_적_없으면_enabledToolsets_는_null_이다(aiohttp_client, fake_api):
    _seed(fake_api, {})
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/config")).json()
    assert body["enabledToolsets"] is None
    assert body["disabledSkills"] == []


async def test_피커_심볼이_없는_빌드는_두_키를_거절한다(aiohttp_client, fake_api):
    _seed(fake_api, {})
    fake_api._get_platform_tools = None
    client = await _client(aiohttp_client, fake_api)
    assert (await client.put("/p/sophie/deskrpg/config", json={"enabledToolsets": ["web"]})).status == 400


@pytest.mark.parametrize("key,value,symbol", [
    *[("enabledToolsets", ["web"], s) for s in (
        "_get_effective_configurable_toolsets", "_get_platform_tools", "_toolset_has_keys",
        "_toolset_allowed_for_platform")],
    *[("disabledSkills", ["pdf"], s) for s in ("_find_all_skills", "_sort_skills")],
])
async def test_capability_와_같은_심볼_집합이_하나라도_없으면_400(aiohttp_client, fake_api, key, value, symbol):
    path = _seed(fake_api, {"model": {"default": "m"}})
    before = path.read_text(encoding="utf-8")
    setattr(fake_api, symbol, None)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json={key: value})
    assert resp.status == 400
    assert path.read_text(encoding="utf-8") == before


# P4 — Hermes `hermes_cli/tools_config.py:_save_platform_tools`(680-716) 와 같은 쓰기.

async def test_enabledToolsets_는_MCP_같은_비설정_항목을_보존하고_기본_합성명과_no_mcp_는_버린다(aiohttp_client, fake_api):
    path = _seed(fake_api, {"platform_toolsets": {
        "api_server": ["web", "my-mcp", "hermes-api-server", "no_mcp"],
        "cli": ["file", "github-mcp"],
        "telegram": ["web", "tg-mcp"],
    }})
    client = await _client(aiohttp_client, fake_api)
    assert (await client.put("/p/sophie/deskrpg/config", json={"enabledToolsets": ["file"]})).status == 200
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    pt = saved["platform_toolsets"]
    assert pt["api_server"] == ["file", "my-mcp"]
    assert pt["cli"] == ["file", "github-mcp"]
    assert pt["cron"] == ["file"]
    assert pt["telegram"] == ["web", "tg-mcp"]  # 다른 플랫폼은 그대로
    for platform in ("api_server", "cron", "cli"):
        assert saved["known_builtin_toolsets"][platform] == ["discord", "file", "tts", "web"]
    assert "telegram" not in saved["known_builtin_toolsets"]


@pytest.mark.parametrize("disabled,expected", [
    (["web", "memory"], ["memory"]),
    ("['web', 'memory']", ["memory"]),
])
async def test_새로_켠_툴셋은_agent_disabled_toolsets_에서_뺀다(aiohttp_client, fake_api, disabled, expected):
    path = _seed(fake_api, {"agent": {"disabled_toolsets": disabled, "max_turns": 5}})
    client = await _client(aiohttp_client, fake_api)
    assert (await client.put("/p/sophie/deskrpg/config", json={"enabledToolsets": ["web", "file"]})).status == 200
    agent = yaml.safe_load(path.read_text(encoding="utf-8"))["agent"]
    assert agent == {"disabled_toolsets": expected, "max_turns": 5}


async def test_agent_disabled_toolsets_가_없으면_만들지_않는다(aiohttp_client, fake_api):
    path = _seed(fake_api, {})
    client = await _client(aiohttp_client, fake_api)
    assert (await client.put("/p/sophie/deskrpg/config", json={"enabledToolsets": ["web"]})).status == 200
    assert "agent" not in yaml.safe_load(path.read_text(encoding="utf-8"))


async def test_플러그인_툴셋이_있으면_known_plugin_toolsets_를_기록한다(aiohttp_client, fake_api):
    fake_api._get_plugin_toolset_keys = lambda: {"spotify"}
    path = _seed(fake_api, {"platform_toolsets": {"api_server": ["spotify", "my-mcp"]}})
    client = await _client(aiohttp_client, fake_api)
    assert (await client.put("/p/sophie/deskrpg/config", json={"enabledToolsets": ["web"]})).status == 200
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["platform_toolsets"]["api_server"] == ["my-mcp", "web"]
    assert saved["known_plugin_toolsets"] == {p: ["spotify"] for p in ("api_server", "cron", "cli")}


async def test_GET은_요청_주소를_함께_준다(aiohttp_client, fake_api):
    # 제공자를 바꿔도 이 값이 남아 있으면 요청은 옛 엔드포인트로 간다 — 화면이 알려면 값이 필요하다.
    _seed(fake_api, {"model": {"provider": "openrouter", "default": "x", "base_url": "https://old.example/v1"}})
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/config")).json()
    assert body["baseUrl"] == "https://old.example/v1"


async def test_주소가_없으면_null(aiohttp_client, fake_api):
    _seed(fake_api, {"model": {"provider": "openai-api", "default": "gpt-5"}})
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/config")).json()
    assert body["baseUrl"] is None


async def test_clearBaseUrl_은_주소와_별칭을_지우고_나머지는_남긴다(aiohttp_client, fake_api):
    path = _seed(
        fake_api,
        {
            "model": {
                "provider": "openrouter",
                "default": "x",
                "base_url": "https://old.example/v1",
                "api_base": "https://old.example/v1",
                "context_length": 128000,
            }
        },
    )
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put(
        "/p/sophie/deskrpg/config",
        json={"provider": "openai-api", "model": "gpt-5", "clearBaseUrl": True},
    )
    assert resp.status == 200
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["model"]
    assert "base_url" not in saved and "api_base" not in saved
    assert saved["provider"] == "openai-api"
    assert saved["default"] == "gpt-5"
    # 주소와 무관한 값은 건드리지 않는다.
    assert saved["context_length"] == 128000


async def test_clearBaseUrl_을_보내지_않으면_주소는_그대로다(aiohttp_client, fake_api):
    path = _seed(fake_api, {"model": {"provider": "openrouter", "default": "x", "base_url": "https://old.example/v1"}})
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json={"provider": "openai-api"})
    assert resp.status == 200
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["model"]["base_url"] == "https://old.example/v1"


async def test_clearBaseUrl_은_true_만_받는다(aiohttp_client, fake_api):
    _seed(fake_api, {"model": {"provider": "openrouter", "default": "x"}})
    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json={"clearBaseUrl": "yes"})
    assert resp.status == 400


# ── 비밀 위생 ────────────────────────────────────
#
# config.yaml 에는 인라인 API 키가 들어갈 수 있다(`providers.*.api_key`,
# `custom_providers`). 그래서 이 파일을 다루는 모든 경로는 두 가지를 지켜야 한다:
# 원문을 **밖으로 내지 않는 것**(응답·로그)과, 사본을 **넓은 권한으로 남기지
# 않는 것**(백업·임시 파일). 아래 테스트가 그 둘을 고정한다.

SEEDED_SECRET = "sk-SEEDED-abc123"

# 문법이 깨진 YAML 이고, 깨진 줄에 비밀이 있다 — YAML 파서의 오류 메시지는
# 문제가 난 줄을 그대로 인용하므로 이 배치가 유출의 최단 경로다.
BROKEN_WITH_SECRET = (
    "providers:\n"
    "  openai:\n"
    f'    api_key: "{SEEDED_SECRET}\n'  # 닫는 따옴표가 없다
)


def _seed_broken_with_secret(fake_api):
    fake_api.create_profile("sophie")
    p = fake_api.get_profile_dir("sophie") / "config.yaml"
    p.write_text(BROKEN_WITH_SECRET, encoding="utf-8")
    return p


async def test_GET_은_깨진_config_의_비밀을_응답에도_로그에도_싣지_않는다(
    aiohttp_client, fake_api, caplog
):
    _seed_broken_with_secret(fake_api)
    client = await _client(aiohttp_client, fake_api)

    with caplog.at_level("WARNING"):
        resp = await client.get("/p/sophie/deskrpg/config")

    assert resp.status == 200
    raw = await resp.text()
    assert SEEDED_SECRET not in raw
    assert SEEDED_SECRET not in caplog.text
    # 유출을 막느라 "왜" 를 잃지 않았는지도 본다 — 플래그는 그대로 있어야 한다.
    assert (await resp.json())["unreadable"] is True


async def test_PUT_은_깨진_config_의_비밀을_409_본문에도_로그에도_싣지_않는다(
    aiohttp_client, fake_api, caplog
):
    path = _seed_broken_with_secret(fake_api)
    client = await _client(aiohttp_client, fake_api)

    with caplog.at_level("WARNING"):
        resp = await client.put("/p/sophie/deskrpg/config", json={"model": "gpt-5.6-terra"})

    assert resp.status == 409
    raw = await resp.text()
    assert SEEDED_SECRET not in raw
    assert SEEDED_SECRET not in caplog.text
    # 거절이 사유를 잃지는 않는다 — 코드로 구분할 수 있어야 화면이 안내한다.
    assert (await resp.json())["error"] == "config_unreadable"
    # 원본은 그대로다.
    assert path.read_text(encoding="utf-8") == BROKEN_WITH_SECRET


async def test_기존_model_이_문자열이면_그_값을_로그에_싣지_않는다(
    aiohttp_client, fake_api, caplog
):
    # `model:` 이 스칼라인 config 는 409 로 거절되는데, 그 로그가 값을 %r 로
    # 찍었다. 사용자가 거기에 무엇을 적어 두었는지는 알 수 없다.
    _seed(fake_api, {"model": SEEDED_SECRET})
    client = await _client(aiohttp_client, fake_api)

    with caplog.at_level("WARNING"):
        resp = await client.put("/p/sophie/deskrpg/config", json={"model": "gpt-5.6-terra"})

    assert resp.status == 409
    assert SEEDED_SECRET not in caplog.text
    assert SEEDED_SECRET not in await resp.text()


@pytest.mark.skipif(os.geteuid() == 0, reason="root 는 umask·권한을 무시한다")
async def test_PUT_은_백업과_본_파일을_0600_으로_남긴다(aiohttp_client, fake_api):
    path = _seed(fake_api, {"model": {"provider": "openai-codex", "default": "a"},
                            "providers": {"openai": {"api_key": SEEDED_SECRET}}})
    os.chmod(path, 0o600)

    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json={"model": "gpt-5.6-terra"})
    assert resp.status == 200

    backups = list(path.parent.glob("config.yaml.bak-*"))
    assert len(backups) == 1
    # 백업은 원본의 완전한 사본이다 — 인라인 키가 그 안에 들어 있다.
    assert SEEDED_SECRET in backups[0].read_text(encoding="utf-8")
    for target in (path, backups[0]):
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"{target.name} 의 권한이 {oct(mode)}"


@pytest.mark.skipif(os.geteuid() == 0, reason="root 는 umask·권한을 무시한다")
async def test_남은_임시_파일이_없다(aiohttp_client, fake_api):
    # 원자적 교체는 임시 파일을 쓴다 — 성공 경로에서 그것이 남으면 키 사본이
    # 하나 더 생기는 셈이다.
    path = _seed(fake_api, {"model": {"provider": "openai-codex", "default": "a"}})
    client = await _client(aiohttp_client, fake_api)
    assert (await client.put("/p/sophie/deskrpg/config", json={"model": "b"})).status == 200

    leftovers = [p.name for p in path.parent.iterdir() if p.name.startswith(".config.yaml.")]
    assert leftovers == []


async def test_본_파일_쓰기가_실패하면_원본이_온전하다(aiohttp_client, fake_api, monkeypatch):
    # `write_text` 는 자리에서 잘라 쓴다 — 쓰는 도중 끊기면 반쯤 쓴 YAML 이
    # 남아 프로필이 뜨지 않는다. 원자적 교체면 원본은 손대지 않은 채로 남는다.
    original = {"model": {"provider": "openai-codex", "default": "a"},
                "providers": {"openai": {"api_key": SEEDED_SECRET}}}
    path = _seed(fake_api, original)
    before = path.read_text(encoding="utf-8")

    real_replace = os.replace

    def boom(src, dst):
        if str(dst) == str(path):
            raise OSError("디스크가 꽉 찼다")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", boom)

    client = await _client(aiohttp_client, fake_api)
    resp = await client.put("/p/sophie/deskrpg/config", json={"model": "gpt-5.6-terra"})
    assert resp.status >= 500

    # 원본이 한 글자도 바뀌지 않았다.
    assert path.read_text(encoding="utf-8") == before
    # 임시 파일도 남지 않았다.
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(".config.yaml.")] == []

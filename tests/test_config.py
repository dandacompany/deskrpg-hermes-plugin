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


async def test_ALLOWED_KEYS_는_문서화된_네_키다():
    assert config.ALLOWED_KEYS == frozenset({"model", "provider", "toolsets", "reasoning_effort"})


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

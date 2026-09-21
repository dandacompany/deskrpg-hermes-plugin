"""워커(칸반·크론)에서도 deskrpg 가 뜨도록 프로필 홈마다 링크와 활성화 항목을 둔다.

Hermes 는 사용자 플러그인을 **로드하는 홈의** `plugins/` 에서만 찾고 활성화도 그 홈의 `config.yaml` 에서
읽는다. 칸반 워커는 `hermes -p <담당 프로필>`, 크론 실행은 `HERMES_HOME=<프로필 홈>` 으로 뜨므로, 루트에만
설치하면 워커에는 훅도 `artifact_save` 도 없다. 오류도 로그도 없이 결과물이 쌓이지 않는다.
"""
import os

import pytest
import yaml
from aiohttp import web

from deskrpg_plugin import routes, worker_plugin
from tests.conftest import FakeAdapter


def _client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return aiohttp_client(app)


def _profile(fake_api, name, config=None):
    d = fake_api.create_profile(name)
    if config is not None:
        (d / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return d


def _link(fake_api, name):
    return fake_api.get_profile_dir(name) / "plugins" / worker_plugin.PLUGIN_KEY


def _config(fake_api, name):
    return yaml.safe_load((fake_api.get_profile_dir(name) / "config.yaml").read_text(encoding="utf-8"))


# ── 판정 ────────────────────────────────────────────────────────────────────


def test_새_프로필은_링크도_활성화도_없다(fake_api):
    _profile(fake_api, "sophie")
    st = worker_plugin.status(fake_api, "sophie")
    assert st == {"profile": "sophie", "link": "missing", "enabled": False, "disabled": False}


def test_plugin_root_는_plugin_yaml_을_가진_설치_디렉터리다():
    root = worker_plugin.plugin_root()
    assert (root / "plugin.yaml").is_file()
    assert yaml.safe_load((root / "plugin.yaml").read_text(encoding="utf-8"))["name"] == worker_plugin.PLUGIN_KEY


# ── 적용 ────────────────────────────────────────────────────────────────────


def test_ensure_는_루트로_가는_링크와_활성화를_만든다(fake_api):
    _profile(fake_api, "sophie", {"model": {"default": "m"}, "plugins": {"enabled": ["danteterm-activity"]}})

    out = worker_plugin.ensure(fake_api, "sophie")

    assert out == {"profile": "sophie", "link": "created", "enabled": "added"}
    link = _link(fake_api, "sophie")
    # 사본이 아니라 링크다 — 루트를 올리면 모든 워커가 같은 버전을 쓴다.
    assert link.is_symlink()
    assert link.resolve() == worker_plugin.plugin_root()
    cfg = _config(fake_api, "sophie")
    assert cfg["plugins"]["enabled"] == ["danteterm-activity", "deskrpg"]  # 기존 항목·순서 보존
    assert cfg["model"] == {"default": "m"}  # 다른 키 보존


def test_ensure_는_멱등이다(fake_api):
    d = _profile(fake_api, "sophie", {"plugins": {"enabled": []}})
    worker_plugin.ensure(fake_api, "sophie")
    backups_after_first = sorted(p.name for p in d.glob("config.yaml.bak-*"))

    out = worker_plugin.ensure(fake_api, "sophie")

    assert out == {"profile": "sophie", "link": "present", "enabled": "present"}
    # 바뀐 것이 없으면 쓰지 않는다 — 백업도 늘지 않는다.
    assert sorted(p.name for p in d.glob("config.yaml.bak-*")) == backups_after_first
    assert _config(fake_api, "sophie")["plugins"]["enabled"] == ["deskrpg"]


def test_config_가_없거나_plugins_키가_없어도_만든다(fake_api):
    _profile(fake_api, "noconfig")
    _profile(fake_api, "nokey", {"model": {"default": "m"}})

    assert worker_plugin.ensure(fake_api, "noconfig")["enabled"] == "added"
    assert worker_plugin.ensure(fake_api, "nokey")["enabled"] == "added"
    assert _config(fake_api, "noconfig")["plugins"]["enabled"] == ["deskrpg"]
    assert _config(fake_api, "nokey") == {"model": {"default": "m"}, "plugins": {"enabled": ["deskrpg"]}}


def test_운영자가_끈_플러그인은_켜지_않는다(fake_api):
    # `plugins.disabled` 는 운영자의 명시적 의사다 — 링크는 두되 활성화는 하지 않고 그 사실을 말한다.
    _profile(fake_api, "sophie", {"plugins": {"enabled": [], "disabled": ["deskrpg"]}})

    out = worker_plugin.ensure(fake_api, "sophie")

    assert out["enabled"] == "disabled_by_operator"
    cfg = _config(fake_api, "sophie")
    assert cfg["plugins"] == {"enabled": [], "disabled": ["deskrpg"]}
    assert worker_plugin.status(fake_api, "sophie")["disabled"] is True


def test_이미_있는_실제_디렉터리는_건드리지_않는다(fake_api):
    # 누군가 사본을 넣어 둔 경우 — 지우면 그 사람의 설치를 망가뜨린다. 알리기만 한다.
    _profile(fake_api, "sophie")
    copy = _link(fake_api, "sophie")
    copy.mkdir(parents=True)
    (copy / "plugin.yaml").write_text("name: deskrpg\n", encoding="utf-8")

    out = worker_plugin.ensure(fake_api, "sophie")

    assert out["link"] == "other"
    assert not copy.is_symlink() and (copy / "plugin.yaml").is_file()
    assert worker_plugin.status(fake_api, "sophie")["link"] == "other"


def test_끊어진_링크는_다시_건다(fake_api, tmp_path):
    # 루트 설치를 옮긴 뒤 남은 링크 — 가리키는 곳이 없으면 워커는 로드하지 못한다.
    _profile(fake_api, "sophie")
    link = _link(fake_api, "sophie")
    link.parent.mkdir(parents=True)
    os.symlink(tmp_path / "gone", link, target_is_directory=True)
    assert worker_plugin.status(fake_api, "sophie")["link"] == "missing"

    out = worker_plugin.ensure(fake_api, "sophie")

    assert out["link"] == "created"
    assert link.resolve() == worker_plugin.plugin_root()


def test_해석할_수_없는_config_는_덮어쓰지_않는다(fake_api):
    d = _profile(fake_api, "sophie")
    broken = "plugins: [unterminated\n"
    (d / "config.yaml").write_text(broken, encoding="utf-8")

    with pytest.raises(worker_plugin.EnsureFailed) as exc:
        worker_plugin.ensure(fake_api, "sophie")

    assert exc.value.reason == "config_unreadable"
    assert (d / "config.yaml").read_text(encoding="utf-8") == broken
    assert list(d.glob("config.yaml.bak-*")) == []


def test_plugins_가_매핑이_아니면_덮어쓰지_않는다(fake_api):
    d = _profile(fake_api, "sophie", {"plugins": ["deskrpg"]})
    before = (d / "config.yaml").read_text(encoding="utf-8")

    with pytest.raises(worker_plugin.EnsureFailed) as exc:
        worker_plugin.ensure(fake_api, "sophie")

    assert exc.value.reason == "config_unreadable"
    assert (d / "config.yaml").read_text(encoding="utf-8") == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root 는 umask·권한을 무시한다")
def test_config_는_원자적으로_0600_으로_쓰고_백업을_남긴다(fake_api):
    d = _profile(fake_api, "sophie", {"model": {"default": "m"}})
    os.chmod(d / "config.yaml", 0o600)

    worker_plugin.ensure(fake_api, "sophie")

    backups = list(d.glob("config.yaml.bak-*"))
    assert len(backups) == 1
    for target in (d / "config.yaml", backups[0]):
        assert (target.stat().st_mode & 0o777) == 0o600


# ── 라우트 ──────────────────────────────────────────────────────────────────


async def test_info_는_워커에서_안_뜨는_프로필을_보고한다(aiohttp_client, fake_api):
    _profile(fake_api, "sophie")
    _profile(fake_api, "oliver")
    worker_plugin.ensure(fake_api, "oliver")
    client = await _client(aiohttp_client, fake_api)

    body = await (await client.get("/deskrpg/info")).json()

    assert "worker_plugin" in body["capabilities"]
    assert body["worker_plugin"] == {
        "missing": [{"profile": "sophie", "link": "missing", "enabled": False, "disabled": False}],
    }


async def test_info_는_판정이_실패해도_500_이_아니다(aiohttp_client, fake_api, monkeypatch):
    def boom(_api):
        raise OSError("권한 없음")

    monkeypatch.setattr(worker_plugin, "report", boom)
    client = await _client(aiohttp_client, fake_api)

    resp = await client.get("/deskrpg/info")

    assert resp.status == 200
    assert (await resp.json())["worker_plugin"] is None


async def test_소유자_라우트는_지정한_프로필만_고친다(aiohttp_client, fake_api):
    _profile(fake_api, "sophie")
    _profile(fake_api, "oliver")
    client = await _client(aiohttp_client, fake_api)

    resp = await client.post("/deskrpg/worker-plugin", json={"profiles": ["sophie"]})

    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == [{"profile": "sophie", "link": "created", "enabled": "added"}]
    assert _link(fake_api, "sophie").is_symlink()
    assert not _link(fake_api, "oliver").exists()


async def test_소유자_라우트는_본문이_없으면_전부를_고치고_실패를_프로필별로_말한다(aiohttp_client, fake_api):
    _profile(fake_api, "sophie")
    broken = _profile(fake_api, "oliver")
    (broken / "config.yaml").write_text("plugins: [x\n", encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)

    resp = await client.post("/deskrpg/worker-plugin")

    assert resp.status == 200
    results = {r["profile"]: r for r in (await resp.json())["results"]}
    assert results["sophie"]["enabled"] == "added"
    # 한 프로필이 깨졌다고 나머지를 멈추지 않는다 — 그 프로필만 사유와 함께 실패로 말한다.
    assert results["oliver"] == {"profile": "oliver", "error": "config_unreadable"}


async def test_소유자_라우트는_없는_프로필을_프로필별_실패로_말한다(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/worker-plugin", json={"profiles": ["ghost"]})
    assert resp.status == 200
    assert (await resp.json())["results"] == [{"profile": "ghost", "error": "not_found"}]


@pytest.mark.parametrize("payload", [{"profiles": "sophie"}, {"profiles": [1]}, ["sophie"]])
async def test_소유자_라우트는_잘못된_본문을_400_으로_거절한다(aiohttp_client, fake_api, payload):
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/deskrpg/worker-plugin", json=payload)
    assert resp.status == 400


async def test_프로필을_만들면_워커에서도_뜨게_해_둔다(aiohttp_client, fake_api):
    client = await _client(aiohttp_client, fake_api)

    resp = await client.post("/deskrpg/profiles", json={"name": "mia"})

    assert resp.status == 201
    body = await resp.json()
    assert body["workerPlugin"] == {"profile": "mia", "link": "created", "enabled": "added"}
    assert _link(fake_api, "mia").is_symlink()


async def test_워커_준비가_실패해도_프로필_생성은_201_이다(aiohttp_client, fake_api, monkeypatch):
    def boom(_api, name):
        raise worker_plugin.EnsureFailed("config_unreadable")

    monkeypatch.setattr(worker_plugin, "ensure", boom)
    client = await _client(aiohttp_client, fake_api)

    resp = await client.post("/deskrpg/profiles", json={"name": "mia"})

    assert resp.status == 201
    body = await resp.json()
    assert body["workerPluginError"] == "config_unreadable"
    assert "workerPlugin" not in body

import types

import yaml
from aiohttp import web

from deskrpg_plugin import routes, skill_jobs
from tests.conftest import FakeAdapter
from tests.test_skill_jobs import FakeProc


async def _client(aiohttp_client, fake_api, monkeypatch, procs=None, on_spawn=None):
    calls = []

    async def spawn(*cmd, env=None, **kw):
        calls.append(list(cmd))
        if on_spawn is not None:
            on_spawn(list(cmd))
        return (procs or [FakeProc(b"ok", 0)]).pop(0)

    monkeypatch.setattr(skill_jobs, "TABLE", skill_jobs.JobTable(spawn=spawn))
    fake_api.create_profile("sophie")
    (fake_api.get_profile_dir("sophie") / "config.yaml").write_text(yaml.safe_dump({}), encoding="utf-8")
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return await aiohttp_client(app), calls


def _bundle(files):
    return types.SimpleNamespace(name="pdf-extract", identifier="skills-sh/x/pdf-extract", source="skills-sh",
                                 description="PDF", trust_level="community", files=files)


async def test_검색은_hermes_검색_결과를_옮긴다(aiohttp_client, fake_api, monkeypatch):
    meta = types.SimpleNamespace(identifier="skills-sh/x/pdf-extract", name="pdf-extract",
                                 description="PDF", source="skills-sh", trust_level="community")
    fake_api.parallel_search_sources = lambda sources, **kw: ([meta], {"skills-sh": 1}, [])
    client, _ = await _client(aiohttp_client, fake_api, monkeypatch)
    body = await (await client.get("/p/sophie/deskrpg/skills/hub/search?q=pdf")).json()
    assert body["results"] == [{"identifier": "skills-sh/x/pdf-extract", "name": "pdf-extract",
                                "description": "PDF", "source": "skills-sh", "trustLevel": "community"}]


async def test_미리보기는_스캔_판정과_실행_코드_포함_여부를_준다(aiohttp_client, fake_api, monkeypatch):
    bundle = _bundle({"SKILL.md": "---\nname: pdf-extract\n---\n", "scripts/run.py": b"print(1)"})
    fake_api._resolve_source_meta_and_bundle = lambda ident, sources: (None, bundle, None)
    fake_api.scan_skill = lambda path, source=None: types.SimpleNamespace(
        skill_name="pdf-extract", source="skills-sh", trust_level="community", verdict="caution",
        summary="", findings=[])
    fake_api.should_allow_install = lambda result, force=False: (None, "caution needs confirmation")
    client, _ = await _client(aiohttp_client, fake_api, monkeypatch)
    body = await (await client.get("/p/sophie/deskrpg/skills/hub/preview?identifier=skills-sh/x/pdf-extract")).json()
    assert body["verdict"] == "caution" and body["policy"] == "ask"
    assert body["hasScripts"] is True
    assert body["files"] == ["SKILL.md", "scripts/run.py"]
    assert body["skillMd"].startswith("---")


async def test_차단_판정이면_설치를_시작하지_않는다(aiohttp_client, fake_api, monkeypatch):
    fake_api._resolve_source_meta_and_bundle = lambda ident, sources: (None, _bundle({"SKILL.md": "x"}), None)
    fake_api.should_allow_install = lambda result, force=False: (False, "dangerous")
    client, calls = await _client(aiohttp_client, fake_api, monkeypatch)
    resp = await client.post("/p/sophie/deskrpg/skills/hub/installs",
                             json={"identifier": "skills-sh/x/pdf-extract", "force": True})
    assert resp.status == 400 and (await resp.json())["error"] == "install_blocked"
    assert calls == []


async def test_설치는_작업으로_돌고_조회된다(aiohttp_client, fake_api, monkeypatch):
    fake_api._resolve_source_meta_and_bundle = lambda ident, sources: (None, _bundle({"SKILL.md": "x"}), None)
    home = str(fake_api.get_profile_dir("sophie"))
    client, calls = await _client(aiohttp_client, fake_api, monkeypatch,
                                  on_spawn=lambda cmd: fake_api.skills.hub.setdefault(home, set()).add("pdf-extract"))
    resp = await client.post("/p/sophie/deskrpg/skills/hub/installs", json={"identifier": "https://example.com/s.md"})
    assert resp.status == 202
    job = (await resp.json())["jobId"]
    await skill_jobs.TABLE.wait(job)
    got = await (await client.get(f"/p/sophie/deskrpg/skills/hub/installs/{job}")).json()
    assert got["state"] == "succeeded"
    assert calls[0][-4:] == ["skills", "install", "https://example.com/s.md", "--yes"]


async def test_force_는_주의_판정에서만_붙는다(aiohttp_client, fake_api, monkeypatch):
    fake_api._resolve_source_meta_and_bundle = lambda ident, sources: (None, _bundle({"SKILL.md": "x"}), None)
    fake_api.should_allow_install = lambda result, force=False: (None, "ask")
    client, calls = await _client(aiohttp_client, fake_api, monkeypatch)
    resp = await client.post("/p/sophie/deskrpg/skills/hub/installs", json={"identifier": "a/b", "force": True})
    job = (await resp.json())["jobId"]
    await skill_jobs.TABLE.wait(job)
    assert calls[0][-2:] == ["--yes", "--force"]


async def test_잘못된_identifier_는_400(aiohttp_client, fake_api, monkeypatch):
    client, _ = await _client(aiohttp_client, fake_api, monkeypatch)
    for bad in ("", "a b", "x\n--force", "a" * 600, "--force", "-x", "--category=../x"):
        resp = await client.post("/p/sophie/deskrpg/skills/hub/installs", json={"identifier": bad})
        assert resp.status == 400, bad


async def test_hub_가_아닌_스킬은_삭제하지_않는다(aiohttp_client, fake_api, monkeypatch):
    client, calls = await _client(aiohttp_client, fake_api, monkeypatch)
    fake_api.skills.seed("sophie", "weekly")
    resp = await client.post("/p/sophie/deskrpg/skills/hub/uninstall", json={"name": "weekly"})
    assert resp.status == 400 and (await resp.json())["error"] == "skill_not_hub"
    fake_api.skills.seed("sophie", "pdf-tools", source="hub")
    ok = await client.post("/p/sophie/deskrpg/skills/hub/uninstall", json={"name": "pdf-tools"})
    assert ok.status == 202
    await skill_jobs.TABLE.wait((await ok.json())["jobId"])
    assert calls[0][-4:] == ["skills", "uninstall", "pdf-tools", "--yes"]


async def test_업데이트는_이름이_없으면_전체(aiohttp_client, fake_api, monkeypatch):
    client, calls = await _client(aiohttp_client, fake_api, monkeypatch)
    resp = await client.post("/p/sophie/deskrpg/skills/hub/update", json={})
    await skill_jobs.TABLE.wait((await resp.json())["jobId"])
    assert calls[0][-2:] == ["skills", "update"]


def _scan(verdict, trust):
    return lambda path, source=None: types.SimpleNamespace(
        skill_name="pdf-extract", source="skills-sh", trust_level=trust, verdict=verdict, summary="", findings=[])


async def test_판정은_hermes_정책을_force_유무로_두번_물어_가른다(aiohttp_client, fake_api, monkeypatch):

    def hermes_policy(result, force=False):
        # Hermes tools.skills_guard.should_allow_install 과 같은 규칙(community: safe 허용·caution 차단·dangerous 강제 불가).
        hard = result.verdict == "dangerous" and result.trust_level in ("community", "trusted")
        if result.verdict == "safe":
            return True, "Allowed"
        if force and not hard:
            return True, "Force-installed"
        return False, "Blocked"

    fake_api._resolve_source_meta_and_bundle = lambda ident, sources: (None, _bundle({"SKILL.md": "x"}), None)
    fake_api.should_allow_install = hermes_policy
    client, calls = await _client(aiohttp_client, fake_api, monkeypatch)
    url = "/p/sophie/deskrpg/skills/hub/preview?identifier=skills-sh/x/pdf-extract"
    for verdict, expected in (("safe", "allow"), ("caution", "ask"), ("dangerous", "block")):
        fake_api.scan_skill = _scan(verdict, "community")
        body = await (await client.get(url)).json()
        assert body["policy"] == expected, verdict
    fake_api.scan_skill = _scan("caution", "community")
    resp = await client.post("/p/sophie/deskrpg/skills/hub/installs",
                             json={"identifier": "skills-sh/x/pdf-extract", "force": True})
    assert resp.status == 202
    await skill_jobs.TABLE.wait((await resp.json())["jobId"])
    assert calls[-1][-2:] == ["--yes", "--force"]


async def test_hermes_가_0_으로_끝나도_설치되지_않았으면_실패다(aiohttp_client, fake_api, monkeypatch):
    fake_api._resolve_source_meta_and_bundle = lambda ident, sources: (None, _bundle({"SKILL.md": "x"}), None)
    client, _ = await _client(aiohttp_client, fake_api, monkeypatch,
                              procs=[FakeProc(b"Not installed: blocked", 0)])
    resp = await client.post("/p/sophie/deskrpg/skills/hub/installs", json={"identifier": "skills-sh/x/pdf-extract"})
    job = (await resp.json())["jobId"]
    await skill_jobs.TABLE.wait(job)
    got = await (await client.get(f"/p/sophie/deskrpg/skills/hub/installs/{job}")).json()
    assert got["state"] == "failed" and "Not installed" in got["outputTail"]


async def test_플래그처럼_보이는_이름은_하위_프로세스로_가지_않는다(aiohttp_client, fake_api, monkeypatch):
    client, calls = await _client(aiohttp_client, fake_api, monkeypatch)
    fake_api.skills.seed("sophie", "--force", source="hub")
    for path, body in (("uninstall", {"name": "--force"}), ("update", {"name": "--force"})):
        resp = await client.post(f"/p/sophie/deskrpg/skills/hub/{path}", json=body)
        assert resp.status == 400 and (await resp.json())["error"] == "invalid_name"
    assert calls == []

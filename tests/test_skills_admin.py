"""스킬 관리 — 조회(Task 2)."""

import yaml
from aiohttp import web

from deskrpg_plugin import routes
from tests.conftest import FakeAdapter


async def _client(aiohttp_client, fake_api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return await aiohttp_client(app)


def _profile(fake_api, cfg=None):
    fake_api.create_profile("sophie")
    (fake_api.get_profile_dir("sophie") / "config.yaml").write_text(
        yaml.safe_dump(cfg or {}), encoding="utf-8")


async def test_목록은_원산지와_사용량과_고정을_덧붙인다(aiohttp_client, fake_api):
    _profile(fake_api)
    fake_api.skills.seed("sophie", "weekly", files={"references/a.md": "a"})
    fake_api.skills.seed("sophie", "pdf-tools", source="hub")
    fake_api.skills.seed("sophie", "web-search", source="bundled")
    home = str(fake_api.get_profile_dir("sophie"))
    fake_api.skills.usage[home] = {"weekly": {"use_count": 3, "view_count": 2, "pinned": True,
                                              "created_by": "agent", "state": "stale",
                                              "last_used_at": "2026-09-20T00:00:00+00:00"}}
    client = await _client(aiohttp_client, fake_api)
    rows = {r["name"]: r for r in (await (await client.get("/p/sophie/deskrpg/skills")).json())["skills"]}
    assert rows["weekly"]["source"] == "local"
    assert rows["weekly"]["curatorManaged"] is True
    assert rows["weekly"]["state"] == "stale"
    assert rows["weekly"]["pinned"] is True
    assert rows["weekly"]["useCount"] == 3 and rows["weekly"]["viewCount"] == 2
    assert rows["weekly"]["lastUsedAt"] == "2026-09-20T00:00:00+00:00"
    assert rows["pdf-tools"]["source"] == "hub"
    assert rows["web-search"]["source"] == "bundled"
    assert rows["pdf-tools"]["curatorManaged"] is False and rows["pdf-tools"]["useCount"] == 0


async def test_상세는_파일_트리와_편집_가능_여부를_준다(aiohttp_client, fake_api):
    _profile(fake_api)
    fake_api.skills.seed("sophie", "weekly", files={
        "references/a.md": "a", "templates/t.md": "t", "scripts/run.py": "print(1)"})
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/skills/weekly")).json()
    files = {f["path"]: f["editable"] for f in body["files"]}
    assert files == {"SKILL.md": True, "references/a.md": True, "templates/t.md": True, "scripts/run.py": False}
    assert body["skill"]["source"] == "local"
    assert body["skill"]["frontmatter"]["name"] == "weekly"


async def test_hub_스킬의_파일은_모두_편집_불가다(aiohttp_client, fake_api):
    _profile(fake_api)
    fake_api.skills.seed("sophie", "pdf-tools", source="hub", files={"references/a.md": "a"})
    client = await _client(aiohttp_client, fake_api)
    body = await (await client.get("/p/sophie/deskrpg/skills/pdf-tools")).json()
    assert all(f["editable"] is False for f in body["files"])


async def test_폴더_이름이_달라도_같은_스킬을_찾는다(aiohttp_client, fake_api):
    _profile(fake_api)
    fake_api.skills.seed("sophie", "weekly-report", folder="wr", category="reports")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/skills/weekly-report/file?path=SKILL.md")
    assert resp.status == 200
    body = await resp.json()
    assert body["content"].startswith("---\nname: weekly-report")
    from deskrpg_plugin.skills_common import sha256_text
    assert body["hash"] == sha256_text(body["content"])


async def test_없는_스킬은_404(aiohttp_client, fake_api):
    _profile(fake_api)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/skills/ghost")
    assert resp.status == 404
    assert (await resp.json())["error"] == "skill_not_found"


async def test_스킬_폴더_밖과_상위_경로는_읽지_않는다(aiohttp_client, fake_api):
    _profile(fake_api)
    fake_api.skills.seed("sophie", "weekly")
    client = await _client(aiohttp_client, fake_api)
    for bad in ("../../config.yaml", "/etc/passwd", "references/../../x"):
        resp = await client.get(f"/p/sophie/deskrpg/skills/weekly/file?path={bad}")
        assert resp.status == 403, bad
        assert (await resp.json())["error"] == "path_not_editable"


async def test_심볼릭_링크는_읽지_않는다(aiohttp_client, fake_api, tmp_path):
    _profile(fake_api)
    base = fake_api.skills.seed("sophie", "weekly")
    (tmp_path / "secret.txt").write_text("sk-SECRET", encoding="utf-8")
    (base / "references").mkdir()
    (base / "references" / "link.md").symlink_to(tmp_path / "secret.txt")
    client = await _client(aiohttp_client, fake_api)
    resp = await client.get("/p/sophie/deskrpg/skills/weekly/file?path=references/link.md")
    assert resp.status == 403
    assert "sk-SECRET" not in await resp.text()


async def test_조회는_요청_프로필_홈에서_한다(aiohttp_client, fake_api):
    _profile(fake_api)
    fake_api.skills.seed("sophie", "weekly")
    seen = []
    original = fake_api._find_skill_dir

    def spy(name):
        seen.append(str(fake_api.get_hermes_home()))
        return original(name)

    fake_api._find_skill_dir = spy
    client = await _client(aiohttp_client, fake_api)
    await client.get("/p/sophie/deskrpg/skills/weekly")
    assert seen and all(s == str(fake_api.get_profile_dir("sophie")) for s in seen)

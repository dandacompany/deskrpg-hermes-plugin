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


async def _put(client, path, body, actor="u-1"):
    return await client.put(path, json=body, headers={"X-DeskRPG-Actor": actor})


async def test_SKILL_md_저장은_hermes_edit_skill_로_폴더_이름을_넘긴다(aiohttp_client, fake_api):
    _profile(fake_api)
    fake_api.skills.seed("sophie", "weekly-report", folder="wr")
    client = await _client(aiohttp_client, fake_api)
    cur = await (await client.get("/p/sophie/deskrpg/skills/weekly-report/file?path=SKILL.md")).json()
    new = "---\nname: weekly-report\ndescription: 새 설명\n---\n# 본문\n"
    resp = await _put(client, "/p/sophie/deskrpg/skills/weekly-report/file",
                      {"path": "SKILL.md", "content": new, "baseHash": cur["hash"]})
    assert resp.status == 200
    from deskrpg_plugin.skills_common import sha256_text
    assert (await resp.json())["hash"] == sha256_text(new)
    assert fake_api.skills.ledger[-1] == {"action": "edit", "skill": "wr"}
    assert fake_api.skills.cache_clears >= 1


async def test_baseHash_가_다르면_409_이고_파일은_그대로다(aiohttp_client, fake_api):
    _profile(fake_api)
    base = fake_api.skills.seed("sophie", "weekly")
    before = (base / "SKILL.md").read_text(encoding="utf-8")
    client = await _client(aiohttp_client, fake_api)
    resp = await _put(client, "/p/sophie/deskrpg/skills/weekly/file",
                      {"path": "SKILL.md", "content": "---\nname: weekly\n---\n", "baseHash": "0" * 64})
    assert resp.status == 409
    assert (await resp.json())["error"] == "skill_changed"
    assert (base / "SKILL.md").read_text(encoding="utf-8") == before


async def test_부속_파일은_write_file_로_쓰고_새_파일은_baseHash_null(aiohttp_client, fake_api):
    _profile(fake_api)
    base = fake_api.skills.seed("sophie", "weekly")
    client = await _client(aiohttp_client, fake_api)
    resp = await _put(client, "/p/sophie/deskrpg/skills/weekly/file",
                      {"path": "references/new.md", "content": "새 참고", "baseHash": None})
    assert resp.status == 200
    assert (base / "references" / "new.md").read_text(encoding="utf-8") == "새 참고"
    again = await _put(client, "/p/sophie/deskrpg/skills/weekly/file",
                       {"path": "references/new.md", "content": "덮기", "baseHash": None})
    assert again.status == 409
    assert (await again.json())["error"] == "file_exists"


async def test_scripts_와_hub_스킬은_쓰지_않는다(aiohttp_client, fake_api):
    _profile(fake_api)
    fake_api.skills.seed("sophie", "weekly")
    fake_api.skills.seed("sophie", "pdf-tools", source="hub")
    client = await _client(aiohttp_client, fake_api)
    r1 = await _put(client, "/p/sophie/deskrpg/skills/weekly/file",
                    {"path": "scripts/x.py", "content": "print(1)", "baseHash": None})
    r2 = await _put(client, "/p/sophie/deskrpg/skills/pdf-tools/file",
                    {"path": "SKILL.md", "content": "---\nname: pdf-tools\n---\n", "baseHash": None})
    assert (r1.status, r2.status) == (403, 403)


async def test_256KB_를_넘는_본문은_413(aiohttp_client, fake_api):
    _profile(fake_api)
    fake_api.skills.seed("sophie", "weekly")
    client = await _client(aiohttp_client, fake_api)
    resp = await _put(client, "/p/sophie/deskrpg/skills/weekly/file",
                      {"path": "references/big.md", "content": "가" * 100000, "baseHash": None})
    assert resp.status == 413


async def test_hermes_가_거절하면_사유를_400_으로_전한다(aiohttp_client, fake_api):
    _profile(fake_api)
    base = fake_api.skills.seed("sophie", "weekly")
    cur = (base / "SKILL.md").read_text(encoding="utf-8")
    from deskrpg_plugin.skills_common import sha256_text
    client = await _client(aiohttp_client, fake_api)
    resp = await _put(client, "/p/sophie/deskrpg/skills/weekly/file",
                      {"path": "SKILL.md", "content": "프론트매터 없음", "baseHash": sha256_text(cur)})
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "skill_write_rejected"
    assert "frontmatter" in body["detail"]


async def test_새_스킬은_hermes_create_skill_로_만든다(aiohttp_client, fake_api):
    _profile(fake_api)
    client = await _client(aiohttp_client, fake_api)
    resp = await client.post("/p/sophie/deskrpg/skills", json={
        "name": "invoice", "category": "office",
        "content": "---\nname: invoice\ndescription: 청구서\n---\n# 청구서\n"})
    assert resp.status == 201
    assert (fake_api.get_profile_dir("sophie") / "skills" / "office" / "invoice" / "SKILL.md").is_file()
    dup = await client.post("/p/sophie/deskrpg/skills", json={
        "name": "invoice", "content": "---\nname: invoice\n---\n"})
    assert dup.status == 400
    assert (await dup.json())["error"] == "skill_write_rejected"


async def test_켜기_끄기는_한_항목만_바꾼다(aiohttp_client, fake_api):
    _profile(fake_api, {"skills": {"disabled": ["a"]}, "other": 1})
    for n in ("a", "b"):
        fake_api.skills.seed("sophie", n)
    client = await _client(aiohttp_client, fake_api)
    resp = await _put(client, "/p/sophie/deskrpg/skills/b/enabled", {"enabled": False})
    assert resp.status == 200
    cfg = yaml.safe_load((fake_api.get_profile_dir("sophie") / "config.yaml").read_text(encoding="utf-8"))
    assert sorted(cfg["skills"]["disabled"]) == ["a", "b"]
    assert cfg["other"] == 1


async def test_일괄_켜기_끄기와_필수_스킬_거절(aiohttp_client, fake_api):
    _profile(fake_api, {"skills": {"disabled": ["a"]}})
    for n in ("a", "b", "c", "hermes-agent"):
        fake_api.skills.seed("sophie", n)
    client = await _client(aiohttp_client, fake_api)
    ok = await _put(client, "/p/sophie/deskrpg/skills/enabled", {"enable": ["a"], "disable": ["b", "c"]})
    assert ok.status == 200
    assert sorted((await ok.json())["disabled"]) == ["b", "c"]
    bad = await _put(client, "/p/sophie/deskrpg/skills/enabled", {"enable": [], "disable": ["hermes-agent", "a"]})
    assert bad.status == 400
    assert (await bad.json())["error"] == "essential_skill"
    cfg = yaml.safe_load((fake_api.get_profile_dir("sophie") / "config.yaml").read_text(encoding="utf-8"))
    assert sorted(cfg["skills"]["disabled"]) == ["b", "c"]  # 전체 거절 — 일부만 반영하지 않는다

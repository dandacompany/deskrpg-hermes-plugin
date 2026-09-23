import json

import yaml
from aiohttp import web

from deskrpg_plugin import routes, skill_jobs
from deskrpg_plugin.skills_common import sha256_text
from tests.conftest import FakeAdapter
from tests.test_skill_jobs import FakeProc


async def _client(aiohttp_client, fake_api, monkeypatch):
    async def spawn(*cmd, env=None, **kw):
        return FakeProc(b"curated", 0)

    monkeypatch.setattr(skill_jobs, "TABLE", skill_jobs.JobTable(spawn=spawn))
    fake_api.create_profile("sophie")
    (fake_api.get_profile_dir("sophie") / "config.yaml").write_text(yaml.safe_dump({}), encoding="utf-8")
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return await aiohttp_client(app)


def _home(fake_api):
    return str(fake_api.get_profile_dir("sophie"))


async def test_curator_상태와_일시정지(aiohttp_client, fake_api, monkeypatch):
    client = await _client(aiohttp_client, fake_api, monkeypatch)
    st = await (await client.get("/p/sophie/deskrpg/curator")).json()
    assert st == {"enabled": True, "paused": False, "intervalHours": 168,
                  "lastRunAt": "2026-09-20T00:00:00+00:00", "minIdleHours": 2.0,
                  "staleAfterDays": 14, "archiveAfterDays": 30}
    await client.put("/p/sophie/deskrpg/curator/paused", json={"paused": True})
    assert (await (await client.get("/p/sophie/deskrpg/curator")).json())["paused"] is True
    assert fake_api.skills.paused == {_home(fake_api): True}


async def test_curator_실행은_작업으로(aiohttp_client, fake_api, monkeypatch):
    client = await _client(aiohttp_client, fake_api, monkeypatch)
    resp = await client.post("/p/sophie/deskrpg/curator/runs")
    assert resp.status == 202
    job = (await resp.json())["jobId"]
    await skill_jobs.TABLE.wait(job)
    got = await (await client.get(f"/p/sophie/deskrpg/curator/runs/{job}")).json()
    assert got["kind"] == "curator_run" and got["state"] == "succeeded"


async def test_includeMemory_0_은_메모리_노드와_간선과_배열을_뺀다(aiohttp_client, fake_api, monkeypatch):
    client = await _client(aiohttp_client, fake_api, monkeypatch)
    fake_api.skills.seed("sophie", "a")
    fake_api.skills.seed("sophie", "b")
    fake_api.skills.memory[_home(fake_api)] = ["사용자는 월요일에 보고서를 받는다"]
    member = await (await client.get("/p/sophie/deskrpg/learning/graph?includeMemory=0")).json()
    assert {n["kind"] for n in member["nodes"]} == {"skill"}
    assert all(not e["source"].startswith("memory:") and not e["target"].startswith("memory:")
               for e in member["edges"])
    assert "memory" not in member
    assert "월요일" not in json.dumps(member, ensure_ascii=False)
    owner = await (await client.get("/p/sophie/deskrpg/learning/graph?includeMemory=1")).json()
    assert any(n["kind"] == "memory" for n in owner["nodes"])


async def test_메모리_노드_편집은_baseHash_를_확인한다(aiohttp_client, fake_api, monkeypatch):
    client = await _client(aiohttp_client, fake_api, monkeypatch)
    fake_api.skills.memory[_home(fake_api)] = ["옛 기억"]
    node = await (await client.get("/p/sophie/deskrpg/learning/node?id=memory:memory:0")).json()
    assert node["hash"] == sha256_text("옛 기억")
    stale = await client.put("/p/sophie/deskrpg/learning/node",
                             json={"id": "memory:memory:0", "content": "새", "baseHash": "0" * 64})
    assert stale.status == 409 and (await stale.json())["error"] == "node_changed"
    ok = await client.put("/p/sophie/deskrpg/learning/node",
                          json={"id": "memory:memory:0", "content": "새 기억", "baseHash": node["hash"]})
    assert ok.status == 200
    assert fake_api.skills.memory[_home(fake_api)] == ["새 기억"]


async def test_메모리_노드_삭제는_원문을_기록한_뒤_지운다(aiohttp_client, fake_api, monkeypatch):
    client = await _client(aiohttp_client, fake_api, monkeypatch)
    fake_api.skills.memory[_home(fake_api)] = ["지울 기억", "남을 기억"]
    resp = await client.delete("/p/sophie/deskrpg/learning/node",
                               json={"id": "memory:memory:0", "baseHash": sha256_text("지울 기억")},
                               headers={"X-DeskRPG-Actor": "u-9"})
    assert resp.status == 200 and (await resp.json())["result"] == "deleted"
    log = fake_api.get_profile_dir("sophie") / "plugin-data" / "deskrpg" / "memory_deleted.jsonl"
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["content"] == "지울 기억" and row["deskrpgUserId"] == "u-9" and row["nodeId"] == "memory:memory:0"
    assert fake_api.skills.memory[_home(fake_api)] == ["남을 기억"]


async def test_스킬_노드_삭제는_로컬_보관이고_hub_는_거절(aiohttp_client, fake_api, monkeypatch):
    client = await _client(aiohttp_client, fake_api, monkeypatch)
    base = fake_api.skills.seed("sophie", "weekly")
    fake_api.skills.seed("sophie", "pdf-tools", source="hub")
    h = sha256_text((base / "SKILL.md").read_text(encoding="utf-8"))
    ok = await client.delete("/p/sophie/deskrpg/learning/node", json={"id": "weekly", "baseHash": h})
    assert (await ok.json())["result"] == "archived"
    hub_md = (fake_api.get_profile_dir("sophie") / "skills" / "pdf-tools" / "SKILL.md").read_text(encoding="utf-8")
    bad = await client.delete("/p/sophie/deskrpg/learning/node",
                              json={"id": "pdf-tools", "baseHash": sha256_text(hub_md)})
    assert bad.status == 400 and (await bad.json())["error"] == "skill_not_local"


async def test_스킬_노드_편집은_edit_skill_경로(aiohttp_client, fake_api, monkeypatch):
    client = await _client(aiohttp_client, fake_api, monkeypatch)
    base = fake_api.skills.seed("sophie", "weekly")
    h = sha256_text((base / "SKILL.md").read_text(encoding="utf-8"))
    new = "---\nname: weekly\n---\n# 새\n"
    resp = await client.put("/p/sophie/deskrpg/learning/node", json={"id": "weekly", "content": new, "baseHash": h})
    assert resp.status == 200
    assert (base / "SKILL.md").read_text(encoding="utf-8") == new


async def test_폴더_이름이_달라도_스킬_노드를_프론트매터_이름으로_찾는다(aiohttp_client, fake_api, monkeypatch):
    client = await _client(aiohttp_client, fake_api, monkeypatch)
    base = fake_api.skills.seed("sophie", "weekly-report", folder="wr")
    # 실제 Hermes node_detail 은 스킬을 폴더 이름(_find_skill)으로 찾는다 — 프론트매터 이름으로는 못 찾는다.
    real_like = fake_api.node_detail

    def by_folder(node_id):
        if node_id.startswith("memory:"):
            return real_like(node_id)
        found = fake_api._find_skill(node_id)
        return {"ok": False, "message": "not found"} if not found else real_like(node_id)

    fake_api.node_detail = by_folder
    node = await (await client.get("/p/sophie/deskrpg/learning/node?id=weekly-report")).json()
    assert node["kind"] == "skill" and node["content"].startswith("---")
    new = "---\nname: weekly-report\n---\n# 고침\n"
    resp = await client.put("/p/sophie/deskrpg/learning/node",
                            json={"id": "weekly-report", "content": new, "baseHash": node["hash"]})
    assert resp.status == 200 and (await resp.json())["hash"] == sha256_text(new)
    assert (base / "SKILL.md").read_text(encoding="utf-8") == new


async def test_메모리_삭제_기록은_0600_이고_출처_파일을_적는다(aiohttp_client, fake_api, monkeypatch):
    client = await _client(aiohttp_client, fake_api, monkeypatch)
    fake_api.skills.memory[_home(fake_api)] = ["지울 기억"]
    await client.delete("/p/sophie/deskrpg/learning/node",
                        json={"id": "memory:memory:0", "baseHash": sha256_text("지울 기억")})
    log = fake_api.get_profile_dir("sophie") / "plugin-data" / "deskrpg" / "memory_deleted.jsonl"
    assert log.stat().st_mode & 0o777 == 0o600
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["sourceFile"] == "MEMORY.md" and row["deskrpgUserId"] is None


async def test_baseHash_가_다르면_메모리를_지우지도_기록하지도_않는다(aiohttp_client, fake_api, monkeypatch):
    client = await _client(aiohttp_client, fake_api, monkeypatch)
    fake_api.skills.memory[_home(fake_api)] = ["남을 기억"]
    resp = await client.delete("/p/sophie/deskrpg/learning/node", json={"id": "memory:memory:0", "baseHash": "0" * 64})
    assert resp.status == 409 and (await resp.json())["error"] == "node_changed"
    assert fake_api.skills.memory[_home(fake_api)] == ["남을 기억"]
    assert not (fake_api.get_profile_dir("sophie") / "plugin-data" / "deskrpg" / "memory_deleted.jsonl").exists()


async def test_멤버용_관계도는_메모리_수가_섞인_stats_를_싣지_않는다(aiohttp_client, fake_api, monkeypatch):
    client = await _client(aiohttp_client, fake_api, monkeypatch)
    fake_api.skills.seed("sophie", "a")
    fake_api.skills.memory[_home(fake_api)] = ["x", "y"]
    member = await (await client.get("/p/sophie/deskrpg/learning/graph")).json()
    assert member["stats"] == {"skill_nodes": 1}


async def test_없는_노드는_404(aiohttp_client, fake_api, monkeypatch):
    client = await _client(aiohttp_client, fake_api, monkeypatch)
    for node_id in ("nope", "memory:memory:5"):
        resp = await client.get(f"/p/sophie/deskrpg/learning/node?id={node_id}")
        assert resp.status == 404 and (await resp.json())["error"] == "node_not_found", node_id

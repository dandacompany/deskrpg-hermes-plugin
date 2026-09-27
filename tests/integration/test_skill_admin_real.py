"""실제 Hermes 위의 NPC 스킬 관리(0.15.0). 가짜로는 "Hermes 쓰기 함수·보관·ledger·메모리 파서가 실제로 그렇게 도는가" 를 못 잡는다.

Hub(네트워크)와 하위 프로세스 작업(설치·curator 실행)은 여기서 돌리지 않는다 — 스테이징 화면 확인에서 본다.
The job runner itself is exercised with the real Hermes CLI (`skills list`) and with a stand-in binary that prints
the environment it was given.
"""

import hashlib
from pathlib import Path

import pytest

from deskrpg_plugin import skill_jobs
from deskrpg_plugin.contract_fields import SKILL_CAPABILITIES, capabilities, has_skill_admin_symbols

pytestmark = pytest.mark.integration

SKILL = "---\nname: invoice-check\ndescription: 청구서 확인 절차\n---\n# 청구서 확인\n\n1. 금액을 대조한다.\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def test_설치된_Hermes_에_스킬_관리_심볼이_다_있다(api):
    assert has_skill_admin_symbols(api)
    assert set(SKILL_CAPABILITIES) <= set(capabilities(api))


async def test_로컬_스킬_생애주기_왕복(client, make_profile):
    make_profile("noah")
    make_profile("mia")
    base = "/p/noah/deskrpg/skills"
    created = await client.post(base, json={"name": "invoice-check", "content": SKILL},
                                headers={"X-DeskRPG-Actor": "u-1"})
    assert created.status == 201, await created.text()

    cur = await (await client.get(f"{base}/invoice-check/file?path=SKILL.md")).json()
    edited = SKILL + "2. 결재선을 확인한다.\n"
    put = await client.put(f"{base}/invoice-check/file",
                           json={"path": "SKILL.md", "content": edited, "baseHash": cur["hash"]})
    assert put.status == 200, await put.text()
    stale = await client.put(f"{base}/invoice-check/file",
                             json={"path": "SKILL.md", "content": "---\nname: invoice-check\n---\n", "baseHash": cur["hash"]})
    assert stale.status == 409 and (await stale.json())["error"] == "skill_changed"
    # Reference files are read-only now: ask the employee in chat.
    ref = await client.put(f"{base}/invoice-check/file",
                           json={"path": "references/rules.md", "content": "규칙", "baseHash": None})
    assert ref.status == 410 and (await ref.json())["error"] == "skill_reference_edit_removed"

    rows = {r["name"]: r for r in (await (await client.get(base)).json())["skills"]}
    assert rows["invoice-check"]["source"] == "local"
    mia = {r["name"] for r in (await (await client.get("/p/mia/deskrpg/skills")).json())["skills"]}
    assert "invoice-check" not in mia

    assert (await client.put(f"{base}/invoice-check/pinned", json={"pinned": True})).status == 200
    blocked = await client.post(f"{base}/invoice-check/archive")
    assert blocked.status == 409 and (await blocked.json())["error"] == "skill_pinned"
    assert (await client.put(f"{base}/invoice-check/pinned", json={"pinned": False})).status == 200
    assert (await client.post(f"{base}/invoice-check/archive")).status == 200
    archived = [a["name"] for a in (await (await client.get(f"{base}/archive")).json())["archived"]]
    assert "invoice-check" in archived
    assert (await client.post(f"{base}/archive/invoice-check/restore")).status == 200
    detail = await (await client.get(f"{base}/invoice-check")).json()
    assert "SKILL.md" in {f["path"] for f in detail["files"]}

    assert (await client.post(f"{base}/invoice-check/archive")).status == 200
    # A single permanent delete is gone: the archived skill stays.
    purge = await client.delete(f"{base}/archive/invoice-check", headers={"X-DeskRPG-Actor": "u-1"})
    assert purge.status == 410 and (await purge.json())["error"] == "skill_purge_removed"
    left = [a["name"] for a in (await (await client.get(f"{base}/archive")).json())["archived"]]
    assert "invoice-check" in left


async def test_켜기_끄기가_Hermes_꺼짐_목록에_들어간다(client, make_profile):
    home = make_profile("noah")
    skill = home / "skills" / "probe-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: probe-skill\ndescription: 탐침\n---\n본문\n", encoding="utf-8")
    assert (await client.put("/p/noah/deskrpg/skills/probe-skill/enabled", json={"enabled": False})).status == 200
    rows = {r["name"]: r for r in (await (await client.get("/p/noah/deskrpg/skills")).json())["skills"]}
    assert rows["probe-skill"]["disabled"] is True


async def test_관계도와_메모리_노드(client, make_profile):
    home = make_profile("noah")
    memories = home / "memories"
    memories.mkdir(parents=True, exist_ok=True)
    (memories / "MEMORY.md").write_text("월요일마다 보고서를 보낸다\n§\n지울 기억\n", encoding="utf-8")
    member = await (await client.get("/p/noah/deskrpg/learning/graph?includeMemory=0")).json()
    assert all(n["kind"] != "memory" for n in member["nodes"])
    owner = await (await client.get("/p/noah/deskrpg/learning/graph?includeMemory=1")).json()
    mem = [n for n in owner["nodes"] if n["kind"] == "memory"]
    assert len(mem) == 2, owner
    target = next(n for n in mem if "지울" in n["label"])
    node = await (await client.get(f"/p/noah/deskrpg/learning/node?id={target['id']}")).json()
    assert node["hash"] == _sha(node["content"])
    stale = await client.delete("/p/noah/deskrpg/learning/node", json={"id": target["id"], "baseHash": "0" * 64})
    assert stale.status == 409
    resp = await client.delete("/p/noah/deskrpg/learning/node", json={"id": target["id"], "baseHash": node["hash"]},
                               headers={"X-DeskRPG-Actor": "u-1"})
    assert resp.status == 200, await resp.text()
    assert "지울 기억" not in (memories / "MEMORY.md").read_text(encoding="utf-8")
    log = home / "plugin-data" / "deskrpg" / "memory_deleted.jsonl"
    assert "지울 기억" in log.read_text(encoding="utf-8")
    assert oct(log.stat().st_mode & 0o777) == "0o600"


async def test_curator_상태와_일시정지(client, make_profile):
    make_profile("noah")
    st = await (await client.get("/p/noah/deskrpg/curator")).json()
    assert set(st) == {"enabled", "paused", "intervalHours", "lastRunAt", "minIdleHours",
                       "staleAfterDays", "archiveAfterDays"}
    assert (await client.put("/p/noah/deskrpg/curator/paused", json={"paused": True})).status == 200
    assert (await (await client.get("/p/noah/deskrpg/curator")).json())["paused"] is True
    make_profile("mia")
    assert (await (await client.get("/p/mia/deskrpg/curator")).json())["paused"] is False



async def test_a_job_runs_the_real_hermes_cli_for_the_right_profile(api, client, make_profile, monkeypatch):
    make_profile("noah")
    make_profile("mia")
    created = await client.post("/p/noah/deskrpg/skills", json={"name": "invoice-check", "content": SKILL})
    assert created.status == 201, await created.text()
    # No `hermes` from the developer's PATH: this interpreter's Hermes, the one under test.
    for name in skill_jobs.HERMES_BIN_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(skill_jobs.shutil, "which", lambda name: None)
    table = skill_jobs.JobTable(timeout=120)
    outputs = {}
    for profile in ("noah", "mia"):
        job = table.start(api, profile, "probe", ["skills", "list", "--source", "local"])
        await table.wait(job)
        got = table.get(profile, job)
        assert got["state"] == "succeeded", got["outputTail"]
        outputs[profile] = got["outputTail"]
    assert "invoice-check" in outputs["noah"]
    assert "invoice-check" not in outputs["mia"]


async def test_a_job_child_gets_no_gateway_secret(api, make_profile, monkeypatch, tmp_path):
    make_profile("noah")
    # The stand-in reports only what the test needs — never the whole environment (it would print the
    # developer's shell variables into the test output).
    stand_in = tmp_path / "report-env"
    stand_in.write_text(
        "#!/bin/sh\n"
        'echo "home=$HERMES_HOME"\n'
        'echo "noninteractive=$HERMES_NONINTERACTIVE"\n'
        '[ -n "$API_SERVER_KEY" ] && echo "leak=API_SERVER_KEY"\n'
        '[ -n "$OPENROUTER_API_KEY" ] && echo "leak=OPENROUTER_API_KEY"\n'
        '[ -n "$GATEWAY_ONLY_SETTING" ] && echo "leak=GATEWAY_ONLY_SETTING"\n'
        "exit 0\n"
    )
    stand_in.chmod(0o755)
    (Path(api.get_hermes_home()) / ".env").write_text("GATEWAY_ONLY_SETTING=from-gateway-env\n")
    monkeypatch.setenv("DESKRPG_HERMES_BIN", str(stand_in))
    monkeypatch.setenv("API_SERVER_KEY", "gateway-owner-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-gateway-secret")
    monkeypatch.setenv("GATEWAY_ONLY_SETTING", "from-gateway-env")
    table = skill_jobs.JobTable(timeout=30)
    job = table.start(api, "noah", "probe", ["skills", "list"])
    await table.wait(job)
    got = table.get("noah", job)
    lines = set(got["outputTail"].splitlines())
    assert got["state"] == "succeeded"
    assert not [line for line in lines if line.startswith("leak=")]
    assert f"home={api.get_profile_dir('noah')}" in lines
    assert "noninteractive=1" in lines

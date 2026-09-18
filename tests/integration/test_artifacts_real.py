"""실제 Hermes 위에서 아티팩트 라우트·도구 핸들러가 임시 홈 아래에만 쓰는지, 사건이 커서에 실리는지."""
import json
from pathlib import Path

import pytest

pytest.importorskip("hermes_cli")

from deskrpg_plugin import artifacts_hook, artifacts_tool  # noqa: E402

B = "?board=default"


async def test_도구_핸들러가_임시_홈_아래_저장소에_쓰고_라우트가_그걸_낸다(client, api, hermes_env):
    handler = artifacts_tool.make_handler(api)
    out = json.loads(handler({"kind": "document", "title": "통합", "summary": "s", "content": "# x", "filename": "i.md"},
                             task_id=None, session_id="int-1", user_task=""))
    assert out["version"] == 1
    root = Path(hermes_env["home"]) / "deskrpg" / "artifacts"
    assert (root / "registry.db").is_file() and any((root / "blobs").rglob("i.md"))
    body = await (await client.get("/deskrpg/artifacts")).json()
    assert body["artifacts"][0]["id"] == out["artifact_id"]
    resp = await client.get(f"/deskrpg/artifacts/{out['artifact_id']}/versions/1/content")
    assert resp.status == 200 and await resp.read() == b"# x"
    assert resp.headers["Content-Security-Policy"] == "sandbox"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


async def test_아티팩트_사건이_기존_사건_커서에_실린다(client, api, hermes_env):
    resp = await client.post("/deskrpg/kanban/boards", json={"slug": "default", "name": "d"})
    assert resp.status in (200, 201)
    start = (await (await client.get(f"/deskrpg/events{B}")).json())["cursor"]
    handler = artifacts_tool.make_handler(api)
    handler({"kind": "data", "title": "표", "summary": "s", "content": "a,b\n1,2", "filename": "t.csv"},
            task_id=None, session_id="int-2", user_task="")
    body = await (await client.get(f"/deskrpg/events{B}&cursor={start}")).json()
    kinds = [e["kind"] for e in body["events"]]
    assert "artifact.created" in kinds


async def test_info_가_artifacts_능력과_상한을_낸다(client):
    body = await (await client.get("/deskrpg/info")).json()
    assert "artifacts" in body["capabilities"] and body["artifact_max_bytes"] > 0


async def test_post_tool_call_훅이_실제_홈_아래_산출_파일을_잡아_아티팩트로_등록한다(client, api, hermes_env):
    """산출 도구(write_file) 결과가 임시 홈 아래 파일을 가리키면 훅이 그걸 아티팩트로 승격한다."""
    target = Path(hermes_env["home"]) / "hook-output.md"
    target.write_text("# 훅으로 잡힘", encoding="utf-8")
    hook = artifacts_hook.make_hook(api)
    hook(
        tool_name="write_file",
        args={},
        result=json.dumps({"path": str(target)}),
        session_id="int-3",
        task_id="",
    )
    body = await (await client.get("/deskrpg/artifacts")).json()
    filenames = [a["filename"] for a in body["artifacts"]]
    assert "hook-output.md" in filenames

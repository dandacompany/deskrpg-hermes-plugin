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
    """`include=artifacts` 로 옵트인하면 실리고, 옵트인하지 않은 같은 커서에는 `artifact.*` 가 하나도 없다(R17)."""
    resp = await client.post("/deskrpg/kanban/boards", json={"slug": "default", "name": "d"})
    assert resp.status in (200, 201)
    inc = "&include=artifacts"
    start = (await (await client.get(f"/deskrpg/events{B}{inc}")).json())["cursor"]
    plain_start = (await (await client.get(f"/deskrpg/events{B}")).json())["cursor"]
    handler = artifacts_tool.make_handler(api)
    handler({"kind": "data", "title": "표", "summary": "s", "content": "a,b\n1,2", "filename": "t.csv"},
            task_id=None, session_id="int-2", user_task="")
    body = await (await client.get(f"/deskrpg/events{B}{inc}&cursor={start}")).json()
    kinds = [e["kind"] for e in body["events"]]
    assert "artifact.created" in kinds
    plain = await (await client.get(f"/deskrpg/events{B}&cursor={plain_start}")).json()
    assert not [e["kind"] for e in plain["events"] if e["kind"].startswith("artifact.")]


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


async def test_프로필_오버라이드_안에서_도구를_부르면_레지스트리는_기본_홈이고_출처_프로필이_남는다(api, hermes_env, profile):
    """게이트웨이가 프로필 세션을 돌리듯 `set_hermes_home_override(<sophie 홈>)` 안에서 도구 핸들러를 부른다.

    레지스트리는 게이트웨이당 하나라 기본 홈 아래에 생기고(R4), 행의 `profile` 은 오버라이드된 홈에서
    읽혀 `sophie` 다(I3).
    """
    import contextlib

    from deskrpg_plugin import artifacts_store as store

    sophie_home = Path(api.get_profile_dir(profile))
    assert sophie_home.parent.name == "profiles"
    handler = artifacts_tool.make_handler(api)
    token = api.set_hermes_home_override(str(sophie_home))
    try:
        assert Path(api.get_hermes_home()).resolve() == sophie_home.resolve()
        out = json.loads(handler({"kind": "document", "title": "프로필 출처", "summary": "s", "content": "# p",
                                  "filename": "p.md"}, task_id=None, session_id="int-profile", user_task=""))
    finally:
        api.reset_hermes_home_override(token)
    assert "error" not in out, out
    root = Path(hermes_env["home"]) / "deskrpg" / "artifacts"
    assert (root / "registry.db").is_file()
    assert not (sophie_home / "deskrpg" / "artifacts").exists()
    with contextlib.closing(store.open_registry(api)) as conn:
        assert store.artifacts_root(api).resolve() == root.resolve()
        row = store.get_artifact(conn, out["artifact_id"])
        assert row["profile"] == "sophie"


def test_실제_Hermes_가_post_llm_call_에_assistant_response_를_넘긴다():
    """응답 훅이 기대는 계약의 걸쇠 — 고정 커밋 Hermes 의 턴 마무리 코드가 이 이름으로 넘기는지 확인한다."""
    import inspect

    from agent import turn_finalizer

    source = inspect.getsource(turn_finalizer._apply_output_hooks)
    assert '"post_llm_call"' in source and "assistant_response=" in source and "session_id=" in source


async def test_응답_훅이_실제_Hermes_위에서_큰_HTML_블록을_저장하고_라우트가_낸다(client, api, hermes_env):
    page = "<!doctype html><html><head><title>통합 페이지</title></head><body>" + "x" * 300 + "</body></html>"
    artifacts_hook.make_response_hook(api)(
        session_id="int-resp", task_id=None, turn_id="t1", user_message="만들어 줘",
        assistant_response=f"여기 있습니다.\n\n```html\n{page}\n```\n", conversation_history=[],
        model="m", platform="cli",
    )
    body = await (await client.get("/deskrpg/artifacts?kind=web")).json()
    got = [a for a in body["artifacts"] if a["title"] == "통합 페이지"]
    assert len(got) == 1 and got[0]["session_id"] == "int-resp"
    detail = await (await client.get(f"/deskrpg/artifacts/{got[0]['id']}")).json()
    assert detail["versions"][0]["captured_via"] == "response"
    resp = await client.get(f"/deskrpg/artifacts/{got[0]['id']}/versions/1/content")
    assert (await resp.read()).decode("utf-8") == page

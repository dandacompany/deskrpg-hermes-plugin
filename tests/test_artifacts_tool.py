"""`artifact_save` 도구 — 모델이 형태를 맞추게 하는 계약. 성공·실패 모두 JSON 문자열로 모델에게 돌아간다."""
import contextlib
import json
import types

import pytest

from deskrpg_plugin import artifacts_store as store
from deskrpg_plugin import artifacts_tool as tool


@pytest.fixture
def api(tmp_path):
    home = tmp_path / "profiles" / "sophie"; home.mkdir(parents=True)
    kanban = tmp_path / "kanban"; kanban.mkdir()

    class _Sessions:
        def __init__(self, *a, **k): pass
        def get_session(self, sid): return {"id": sid, "source": "desktop"}
        def close(self): pass

    return types.SimpleNamespace(
        get_hermes_home=lambda: str(home), kanban_home=lambda: str(kanban), get_current_board=lambda: "dev",
        SessionDB=_Sessions, MAX_REQUEST_BYTES=50 * 1024 * 1024,
    )


def _call(api, **args):
    handler = tool.make_handler(api)
    return json.loads(handler(args, task_id=None, session_id="s1", user_task="x"))


def test_스키마는_필수_둘과_kind_enum_과_url_을_가진다():
    params = tool.TOOL_SCHEMA["parameters"]
    assert set(params["required"]) == {"kind", "summary"}
    assert params["properties"]["kind"]["enum"] == list(tool.policy.KINDS)
    assert "url" in params["properties"]


def test_인라인_문서를_저장하면_id_와_버전이_돌아오고_출처는_컨텍스트에서_온다(api):
    out = _call(api, kind="document", title="보고서", summary="요약", content="# 제목", filename="r.md")
    assert out["version"] == 1 and out["kind"] == "document" and not out["deduped"]
    with contextlib.closing(store.open_registry(api)) as conn:
        row = store.get_artifact(conn, out["artifact_id"])
        assert (row["profile"], row["source_kind"], row["session_id"]) == ("sophie", "chat", "s1")
        v = store.list_versions(conn, out["artifact_id"])[0]
        assert (v["created_by"], v["captured_via"], v["mime"]) == ("agent:sophie", "tool", "text/markdown")


def test_task_id_가_오면_kanban_출처가_된다(api):
    handler = tool.make_handler(api)
    out = json.loads(handler({"kind": "document", "title": "t", "summary": "s", "content": "x", "filename": "a.md"},
                             task_id="t7", session_id="s1", user_task=""))
    with contextlib.closing(store.open_registry(api)) as conn:
        row = store.get_artifact(conn, out["artifact_id"])
        assert (row["source_kind"], row["task_id"], row["board"]) == ("kanban", "t7", "dev")


def test_모델이_넘긴_session_id_profile_인자는_무시된다(api):
    out = _call(api, kind="document", title="t", summary="s", content="x", filename="a.md",
                session_id="forged", profile="admin")
    with contextlib.closing(store.open_registry(api)) as conn:
        row = store.get_artifact(conn, out["artifact_id"])
        assert row["session_id"] == "s1" and row["profile"] == "sophie"


def test_path_와_content_는_정확히_하나여야_한다(api):
    assert _call(api, kind="document", title="t", summary="s")["error"] == "artifact_incomplete"
    assert _call(api, kind="document", title="t", summary="s", content="x", filename="a.md", path="/x")["error"] == "artifact_incomplete"


def test_content_에는_filename_이_필요하다(api):
    assert _call(api, kind="document", title="t", summary="s", content="x")["error"] == "artifact_incomplete"


def test_path_는_허용_루트_아래_실제_파일이어야_한다(api, tmp_path):
    outside = tmp_path / "o.png"; outside.write_bytes(b"\x89PNG")
    assert _call(api, kind="image", title="t", summary="s", path=str(outside))["error"] == "artifact_path_outside_root"
    inside = tmp_path / "kanban" / "shot.png"; inside.write_bytes(b"\x89PNG")
    out = _call(api, kind="image", title="스크린샷", summary="s", path=str(inside))
    assert out["version"] == 1
    with contextlib.closing(store.open_registry(api)) as conn:
        assert store.list_versions(conn, out["artifact_id"])[0]["origin_path"] == str(inside.resolve())


def test_path_파일이_상한을_넘으면_읽지_않고_too_large_를_돌려준다(api, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DESKRPG_ARTIFACT_MAX_BYTES", "4")
    big = tmp_path / "kanban" / "big.md"; big.write_bytes(b"0123456789")

    def _forbidden(self, *a, **k):
        raise AssertionError("읽으면 안 된다")

    monkeypatch.setattr("pathlib.Path.read_bytes", _forbidden)
    out = _call(api, kind="document", title="t", summary="s", path=str(big))
    assert out["error"] == "artifact_too_large" and out["max_bytes"] == 4


def test_kind_완결성_실패는_모델에게_고칠_말을_돌려준다(api):
    out = _call(api, kind="web", title="t", summary="s", content="<div/>", filename="a.html")
    assert out["error"] == "artifact_incomplete" and "html" in out["detail"].lower()


def test_supersedes_와_note_가_버전에_기록된다(api):
    first = _call(api, kind="document", title="t", summary="s", content="v1", filename="a.md")
    second = _call(api, kind="document", title="다른 제목", summary="s", content="v2", filename="a.md",
                   supersedes=first["artifact_id"], note="표 정렬 반영")
    assert second["artifact_id"] == first["artifact_id"] and second["version"] == 2
    with contextlib.closing(store.open_registry(api)) as conn:
        assert store.list_versions(conn, first["artifact_id"])[1]["note"] == "표 정렬 반영"


def test_크기_초과는_too_large_와_상한을_돌려준다(api, monkeypatch):
    monkeypatch.setenv("HERMES_DESKRPG_ARTIFACT_MAX_BYTES", "4")
    out = _call(api, kind="document", title="t", summary="s", content="12345", filename="a.md")
    assert out["error"] == "artifact_too_large" and out["max_bytes"] == 4


def test_예상_못_한_예외도_JSON_오류로_돌아온다(api, monkeypatch):
    monkeypatch.setattr(tool.store, "open_registry", lambda api: (_ for _ in ()).throw(RuntimeError("disk")))
    out = _call(api, kind="document", title="t", summary="s", content="x", filename="a.md")
    assert out["error"] == "internal_error" and out["detail"] == "RuntimeError"


def test_도구는_요청_본문_상한에_묶이지_않고_저장소_상한을_쓴다(api, monkeypatch):
    """도구는 HTTP 를 거치지 않는다 — MAX_REQUEST_BYTES 가 작아도 저장소 상한(기본 100 MiB)까지 저장한다(I2)."""
    monkeypatch.delenv("HERMES_DESKRPG_ARTIFACT_MAX_BYTES", raising=False)
    api.MAX_REQUEST_BYTES = 10
    out = _call(api, kind="document", title="t", summary="s", content="0" * 20, filename="big.md")
    assert "error" not in out and out["version"] == 1
    assert tool.artifact_storage_max_bytes() == 100 * 1024 * 1024


def test_url_로_링크를_저장하면_정리된_URL_한_줄이_blob_이고_제목은_라벨이다(api):
    out = _call(api, kind="link", summary="참고 문서", url="https://Docs.io/guide/setup).")
    assert out["kind"] == "link" and out["title"] == "setup" and out["version"] == 1
    with contextlib.closing(store.open_registry(api)) as conn:
        v = store.list_versions(conn, out["artifact_id"])[0]
        assert (v["mime"], v["filename"], v["captured_via"]) == ("text/uri-list", "setup.url", "tool")
        assert store.blob_path_for(api, v).read_bytes() == b"https://docs.io/guide/setup\n"


def test_같은_세션에_같은_URL_을_다시_저장하면_중복이다(api):
    first = _call(api, kind="link", summary="s", url="https://x.io/a", title="첫 이름")
    again = _call(api, kind="link", summary="s", url="https://X.io/a", title="다른 이름")
    assert again["artifact_id"] == first["artifact_id"] and again["deduped"] is True


@pytest.mark.parametrize("args", [
    dict(kind="link", summary="s"),                                               # url 없음
    dict(kind="document", summary="s", url="https://x.io", title="t"),            # url 인데 link 아님
    dict(kind="link", summary="s", url="https://x.io", content="x", filename="a.md"),
    dict(kind="link", summary="s", url="https://x.io", path="/tmp/a.md"),
    dict(kind="link", summary="s", url="javascript:alert(1)"),
    dict(kind="document", summary="s", content="# a", filename="a.md"),           # link 아닌데 제목 없음
])
def test_링크_인자_조합이_틀리면_artifact_incomplete(api, args):
    assert _call(api, **args)["error"] == "artifact_incomplete"


@pytest.mark.parametrize("first,second", [
    (dict(kind="link", summary="s", url="https://x.io/a"),
     dict(kind="document", title="보고서", summary="s", content="# 본문", filename="r.md")),
    (dict(kind="document", title="보고서", summary="s", content="# 본문", filename="r.md"),
     dict(kind="link", summary="s", url="https://x.io/a")),
])
def test_supersedes_대상과_kind_가_다르고_한쪽이_link_면_artifact_incomplete_이고_저장하지_않는다(api, first, second):
    base = _call(api, **first)
    out = _call(api, supersedes=base["artifact_id"], **second)
    assert out["error"] == "artifact_incomplete" and "kind" in out["detail"]
    with contextlib.closing(store.open_registry(api)) as conn:
        assert len(store.list_artifacts(conn)) == 1
        assert [v["version"] for v in store.list_versions(conn, base["artifact_id"])] == [1]


def test_link_가_아닌_kind_끼리의_supersedes_는_그대로_새_버전이다(api):
    base = _call(api, kind="document", title="보고서", summary="s", content="# 1", filename="r.md")
    out = _call(api, kind="data", title="표", summary="s", content="a,b\n1,2\n", filename="t.csv",
                supersedes=base["artifact_id"])
    assert (out["artifact_id"], out["version"]) == (base["artifact_id"], 2)

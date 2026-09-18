"""post_tool_call 훅 — 산출 도구의 결과에서 파일을 찾아 자동 승격한다. 절대 던지지 않는다."""
import contextlib
import json
import types

import pytest

from deskrpg_plugin import artifacts_hook as hook
from deskrpg_plugin import artifacts_policy as policy
from deskrpg_plugin import artifacts_store as store


@pytest.fixture
def api(tmp_path):
    home = tmp_path / "profiles" / "sophie"; home.mkdir(parents=True)
    kanban = tmp_path / "kanban"; kanban.mkdir()

    class _Sessions:
        def __init__(self, *a, **k): pass
        def get_session(self, sid): return {"id": sid, "source": "cron" if sid == "cron-s" else "desktop"}
        def close(self): pass

    return types.SimpleNamespace(get_hermes_home=lambda: str(home), kanban_home=lambda: str(kanban),
                                 get_current_board=lambda: "dev", SessionDB=_Sessions, MAX_REQUEST_BYTES=10_000_000)


def _fire(api, tool_name, result, session_id="s1", task_id=""):
    hook.make_hook(api)(tool_name=tool_name, args={}, result=result, session_id=session_id, task_id=task_id,
                        tool_call_id="c1", turn_id="", api_request_id="", duration_ms=1, status="ok",
                        error_type=None, error_message=None, middleware_trace=[])


def _rows(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        return store.list_artifacts(conn)


def test_산출_도구가_아니면_아무것도_하지_않고_레지스트리도_만들지_않는다(api, tmp_path):
    _fire(api, "web_search", json.dumps({"saved_to": str(tmp_path / "kanban" / "x.md")}))
    assert not (store.artifacts_root(api) / "registry.db").exists()


def test_artifact_save_자신의_결과는_건너뛴다(api, tmp_path):
    p = tmp_path / "kanban" / "a.md"; p.write_text("x")
    _fire(api, "artifact_save", json.dumps({"saved_to": str(p)}))
    assert not (store.artifacts_root(api) / "registry.db").exists()


def test_산출_도구의_강한_키_파일을_hook_으로_승격한다(api, tmp_path):
    p = tmp_path / "kanban" / "report.pdf"; p.write_bytes(b"%PDF")
    _fire(api, "export_pdf", json.dumps({"output_file": str(p)}))
    rows = _rows(api)
    assert len(rows) == 1 and rows[0]["kind"] == "document" and rows[0]["title"] == "report.pdf"
    with contextlib.closing(store.open_registry(api)) as conn:
        v = store.list_versions(conn, rows[0]["id"])[0]
        assert v["captured_via"] == "hook" and v["origin_path"] == str(p.resolve())


def test_크론_세션의_산출물은_cron_출처로_남는다(api, tmp_path):
    p = tmp_path / "kanban" / "digest.md"; p.write_text("# d")
    _fire(api, "write_file", json.dumps({"path": str(p)}), session_id="cron-s")
    assert _rows(api)[0]["source_kind"] == "cron"


def test_허용_확장자가_아니면_건너뛴다(api, tmp_path):
    p = tmp_path / "kanban" / "bin.exe"; p.write_bytes(b"MZ")
    _fire(api, "create_binary", json.dumps({"saved_to": str(p)}))
    assert _rows(api) == []


def test_루트_밖_경로는_승격하지_않고_실패_사건도_남기지_않는다(api, tmp_path):
    outside = tmp_path / "o.md"; outside.write_text("x")
    _fire(api, "write_file", json.dumps({"saved_to": str(outside)}))
    assert _rows(api) == []


def test_같은_파일을_두_번_보면_버전이_늘지_않는다(api, tmp_path):
    p = tmp_path / "kanban" / "a.md"; p.write_text("same")
    _fire(api, "write_file", json.dumps({"saved_to": str(p)}))
    _fire(api, "write_file", json.dumps({"saved_to": str(p)}))
    with contextlib.closing(store.open_registry(api)) as conn:
        assert len(store.list_versions(conn, _rows(api)[0]["id"])) == 1


def test_저장_중_예외는_삼키고_capture_failed_사건을_남긴다(api, tmp_path, monkeypatch):
    p = tmp_path / "kanban" / "a.md"; p.write_text("x")
    real = store.store_artifact_version
    monkeypatch.setattr(store, "store_artifact_version", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    _fire(api, "write_file", json.dumps({"saved_to": str(p)}))  # 던지면 테스트가 실패한다
    monkeypatch.setattr(store, "store_artifact_version", real)
    with contextlib.closing(store.open_registry(api)) as conn:
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM artifact_events")]
        assert kinds == ["artifact.capture_failed"]


def test_결과가_JSON_이_아니면_조용히_넘어간다(api):
    _fire(api, "write_file", "파일을 썼습니다: /kanban/a.md")
    assert not (store.artifacts_root(api) / "registry.db").exists()


def test_비경로_문자열_후보가_저장_슬롯을_소모하지_않는다(api, tmp_path):
    """R11: candidates 를 자르지 않고 최대 64개까지 스캔하고, 실제 저장에 성공(또는 dedup)한
    파일이 8개가 될 때까지만 센다. 조상 키 태그로 하위 트리 전체가 후보가 되면(R9) `mime` 같은
    비경로 문자열도 섞여 들어오는데, 이런 값은 `resolve_source_path` 에서 걸러지고 저장 슬롯을
    쓰지 않으므로 뒤에 있는 진짜 파일도 여전히 잡혀야 한다."""
    p = tmp_path / "kanban" / "note.md"; p.write_text("real")
    payload = {"artifact_file": {"mime": "text/markdown"}}
    for i in range(10):
        payload["artifact_file"][chr(ord("a") + i)] = f"x{i}"
    payload["artifact_file"]["path"] = str(p)
    _fire(api, "export_file", json.dumps(payload))
    rows = _rows(api)
    assert len(rows) == 1 and rows[0]["title"] == "note.md"


def test_중간에_사라진_파일은_건너뛰고_다음_후보는_잡는다(api, tmp_path, monkeypatch):
    """R12: `resolve_source_path` 통과 뒤 `stat()` 전에 파일이 사라지면(OSError) 그 후보만
    건너뛰고 나머지 후보는 계속 시도한다. 사라진 파일은 capture_failed 사건도 남기지 않는다
    (루트 밖 경로와 같은 취급)."""
    a = tmp_path / "kanban" / "a.md"; a.write_text("a")
    b = tmp_path / "kanban" / "b.md"; b.write_text("b")
    real = policy.resolve_source_path

    def flaky(api_, raw):
        resolved = real(api_, raw)
        if resolved == a.resolve():
            resolved.unlink()  # resolve 직후, stat 직전에 사라진 상황을 흉내낸다
        return resolved

    monkeypatch.setattr(hook.policy, "resolve_source_path", flaky)
    _fire(api, "write_file", json.dumps({"files_created": [str(a), str(b)]}))
    rows = _rows(api)
    assert len(rows) == 1 and rows[0]["title"] == "b.md"
    with contextlib.closing(store.open_registry(api)) as conn:
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM artifact_events")]
        assert "artifact.capture_failed" not in kinds

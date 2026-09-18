"""post_llm_call 훅 — 턴의 최종 응답 속 큰 코드 블록을 아티팩트로 승격한다. 절대 던지지 않는다."""
import contextlib
import types

import pytest

from deskrpg_plugin import artifacts_hook as hook
from deskrpg_plugin import artifacts_store as store


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_DESKRPG_CAPTURE_RESPONSES", raising=False)
    home = tmp_path / "profiles" / "sophie"; home.mkdir(parents=True)
    kanban = tmp_path / "kanban"; kanban.mkdir()

    class _Sessions:
        def __init__(self, *a, **k): pass
        def get_session(self, sid): return {"id": sid, "source": "cron" if sid == "cron-s" else "desktop"}
        def close(self): pass

    return types.SimpleNamespace(get_hermes_home=lambda: str(home), kanban_home=lambda: str(kanban),
                                 get_current_board=lambda: "dev", SessionDB=_Sessions, MAX_REQUEST_BYTES=10_000_000)


def _fire(api, response, session_id="s1", task_id=None):
    # Hermes `agent/turn_finalizer.py:_apply_output_hooks` 가 넘기는 kwargs 그대로.
    hook.make_response_hook(api)(
        session_id=session_id, task_id=task_id, turn_id="t1", user_message="만들어 줘",
        assistant_response=response, conversation_history=[], model="m", platform="cli",
    )


def _rows(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        return store.list_artifacts(conn)


def _doc(title, pad=200):
    return f"<!doctype html><html><head><title>{title}</title></head><body>{'x' * pad}</body></html>"


def _html_answer(title="대시보드", pad=200):
    return f"여기 페이지입니다.\n\n```html\n{_doc(title, pad)}\n```\n\n열어 보세요."


def test_큰_코드_블록이_없으면_아무것도_하지_않고_레지스트리도_만들지_않는다(api):
    _fire(api, "짧은 답입니다.\n```python\nprint(1)\n```")
    assert not store.registry_path(api).exists()


def test_HTML_문서_블록을_response_출처로_저장한다(api):
    _fire(api, _html_answer("매출 대시보드"))
    rows = _rows(api)
    assert len(rows) == 1 and rows[0]["kind"] == "web" and rows[0]["title"] == "매출 대시보드"
    assert rows[0]["profile"] == "sophie" and rows[0]["source_kind"] == "chat"
    with contextlib.closing(store.open_registry(api)) as conn:
        v = store.list_versions(conn, rows[0]["id"])[0]
        assert v["captured_via"] == "response" and v["created_by"] == "agent:sophie"
        assert v["filename"] == "매출-대시보드.html" and v["mime"] == "text/html"
        assert store.blob_path_for(api, v).read_text(encoding="utf-8") == _doc("매출 대시보드")


def test_같은_제목을_고쳐_쓰면_다음_버전이고_같은_내용이면_버전이_늘지_않는다(api):
    _fire(api, _html_answer("대시보드", 200))
    _fire(api, _html_answer("대시보드", 300))
    _fire(api, _html_answer("대시보드", 300))
    rows = _rows(api)
    assert len(rows) == 1
    with contextlib.closing(store.open_registry(api)) as conn:
        assert [v["version"] for v in store.list_versions(conn, rows[0]["id"])] == [1, 2]


def test_칸반_워커와_크론_세션의_응답도_출처가_남는다(api):
    _fire(api, _html_answer("카드 결과"), task_id="t9")
    _fire(api, _html_answer("정기 보고"), session_id="cron-s")
    kinds = {r["title"]: (r["source_kind"], r["task_id"]) for r in _rows(api)}
    assert kinds == {"카드 결과": ("kanban", "t9"), "정기 보고": ("cron", None)}


def test_한_턴에_여덟_개까지만_저장한다(api):
    answer = "\n\n".join(f"```html\n{_doc(f'페이지{i}')}\n```" for i in range(11))
    _fire(api, answer)
    assert len(_rows(api)) == hook.MAX_RESPONSE_BLOCKS == 8


def test_스위치가_꺼져_있으면_잡지_않는다(api, monkeypatch):
    monkeypatch.setenv("HERMES_DESKRPG_CAPTURE_RESPONSES", "0")
    _fire(api, _html_answer())
    assert not store.registry_path(api).exists()


@pytest.mark.parametrize("value", ["1", "true", "yes", ""])
def test_스위치의_다른_값은_켠_것으로_본다(api, monkeypatch, value):
    monkeypatch.setenv("HERMES_DESKRPG_CAPTURE_RESPONSES", value)
    _fire(api, _html_answer())
    assert len(_rows(api)) == 1


def test_저장소_상한을_넘는_블록은_건너뛰고_capture_failed_를_남긴다(api, monkeypatch):
    monkeypatch.setenv("HERMES_DESKRPG_ARTIFACT_MAX_BYTES", "100")
    _fire(api, _html_answer("큰 페이지", 500))
    with contextlib.closing(store.open_registry(api)) as conn:
        assert store.list_artifacts(conn) == []
        events = [(r["kind"], r["payload"]) for r in conn.execute("SELECT kind, payload FROM artifact_events")]
    assert len(events) == 1 and events[0][0] == "artifact.capture_failed" and "too_large" in events[0][1]


def test_저장_중_예외는_삼키고_capture_failed_를_남긴다(api, monkeypatch):
    real = store.store_artifact_version
    monkeypatch.setattr(store, "store_artifact_version", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    _fire(api, _html_answer())  # 던지면 테스트가 실패한다
    monkeypatch.setattr(store, "store_artifact_version", real)
    with contextlib.closing(store.open_registry(api)) as conn:
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM artifact_events")]
    assert kinds == ["artifact.capture_failed"]


@pytest.mark.parametrize("response", [None, 42, {"text": "x"}])
def test_응답이_문자열이_아니어도_던지지_않는다(api, response):
    _fire(api, response)
    assert not store.registry_path(api).exists()

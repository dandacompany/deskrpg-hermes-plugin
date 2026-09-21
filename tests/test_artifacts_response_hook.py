"""post_llm_call 훅 — 턴의 최종 응답 속 큰 코드 블록을 아티팩트로 승격한다. 절대 던지지 않는다."""
import contextlib
import json
import types

import pytest

from deskrpg_plugin import artifacts_hook as hook
from deskrpg_plugin import artifacts_store as store


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_DESKRPG_CAPTURE_RESPONSES", raising=False)
    monkeypatch.delenv("HERMES_DESKRPG_CAPTURE_LINKS", raising=False)
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


def test_칸반_워커와_크론_세션의_응답도_출처가_남는다(api, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t9")
    _fire(api, _html_answer("카드 결과"), task_id="run-scope-id")
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    _fire(api, _html_answer("정기 보고"), session_id="cron-s", task_id="run-scope-id")
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


def test_상한을_넘는_블록은_감지_전에_건너뛰고_capture_failed_는_턴당_한_번이다(api, monkeypatch):
    monkeypatch.setenv("HERMES_DESKRPG_ARTIFACT_MAX_BYTES", "100")
    calls = []
    real_detect = hook.fences.detect
    monkeypatch.setattr(hook.fences, "detect", lambda *a, **k: calls.append(a) or real_detect(*a, **k))
    answer = "\n\n".join(f"```html\n{_doc(f'큰{i}', 500)}\n```" for i in range(3))
    _fire(api, answer)
    assert calls == []  # 상한을 넘는 블록은 정규식 감지를 거치지 않는다
    with contextlib.closing(store.open_registry(api)) as conn:
        events = [r["kind"] for r in conn.execute("SELECT kind FROM artifact_events")]
    assert events == ["artifact.capture_failed"]


def test_레지스트리가_잠기면_첫_실패에서_멈추고_실패_사건은_한_번이다(api, monkeypatch):
    import sqlite3

    attempts = []

    def locked(*a, **k):
        attempts.append(1)
        raise sqlite3.OperationalError("database is locked")

    real = store.store_artifact_version
    monkeypatch.setattr(store, "store_artifact_version", locked)
    answer = "\n\n".join(f"```html\n{_doc(f'페이지{i}')}\n```" for i in range(5))
    _fire(api, answer)
    monkeypatch.setattr(store, "store_artifact_version", real)
    assert attempts == [1]
    with contextlib.closing(store.open_registry(api)) as conn:
        events = [r["kind"] for r in conn.execute("SELECT kind FROM artifact_events")]
    assert events == ["artifact.capture_failed"]


def test_시도_자체를_턴당_여덟_번으로_묶는다(api, monkeypatch):
    attempts = []
    real = store.store_artifact_version
    monkeypatch.setattr(store, "store_artifact_version",
                        lambda *a, **k: attempts.append(1) or (_ for _ in ()).throw(ValueError("bad")))
    answer = "\n\n".join(f"```html\n{_doc(f'페이지{i}')}\n```" for i in range(12))
    _fire(api, answer)
    monkeypatch.setattr(store, "store_artifact_version", real)
    assert len(attempts) == hook.MAX_RESPONSE_BLOCKS


# ---------------------------------------------------------------------------
# 링크 (0.8.3)
# ---------------------------------------------------------------------------


def _links(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        return store.list_artifacts(conn, kind="link")


def test_답변의_링크를_response_출처의_link_로_저장한다(api):
    _fire(api, "정리했습니다: [9월 보고](https://docs.io/r) 원문 https://news.io/a")
    rows = sorted(_links(api), key=lambda r: r["title"])
    assert [(r["title"], r["summary"]) for r in rows] == [("9월 보고", "https://docs.io/r"), ("a", "https://news.io/a")]
    with contextlib.closing(store.open_registry(api)) as conn:
        v = store.list_versions(conn, rows[0]["id"])[0]
        assert (v["captured_via"], v["mime"]) == ("response", "text/uri-list")
        assert store.blob_path_for(api, v).read_bytes() == b"https://docs.io/r\n"


def test_같은_세션에서_같은_링크가_다시_나오면_하나다(api):
    _fire(api, "https://x.io/a")
    _fire(api, "다시 [이름](https://x.io/a)")
    assert len(_links(api)) == 1


def test_링크는_턴당_30개까지다(api):
    _fire(api, " ".join(f"https://x.io/{i}" for i in range(40)))
    assert len(_links(api)) == hook.MAX_RESPONSE_LINKS == 30


def test_링크_스위치가_꺼지면_코드_블록만_잡는다(api, monkeypatch):
    monkeypatch.setenv("HERMES_DESKRPG_CAPTURE_LINKS", "off")
    _fire(api, _html_answer("페이지") + "\nhttps://x.io/a")
    assert [r["kind"] for r in _rows(api)] == ["web"]


def test_코드_블록과_링크는_한_연결과_한_실패_사건을_쓴다(api, monkeypatch):
    import sqlite3

    attempts = []

    def locked(*a, **k):
        attempts.append(1)
        raise sqlite3.OperationalError("database is locked")

    real = store.store_artifact_version
    monkeypatch.setattr(store, "store_artifact_version", locked)
    _fire(api, _html_answer("페이지") + "\nhttps://x.io/a https://x.io/b")
    monkeypatch.setattr(store, "store_artifact_version", real)
    assert attempts == [1]  # 첫 잠금에서 멈춘다 — 링크로 넘어가지 않는다
    with contextlib.closing(store.open_registry(api)) as conn:
        assert [r["kind"] for r in conn.execute("SELECT kind FROM artifact_events")] == ["artifact.capture_failed"]


def test_코드_블록_안의_URL_은_링크가_아니다(api):
    _fire(api, "```bash\ncurl https://api.example.com\n```")
    assert not store.registry_path(api).exists()


def test_도구가_supersedes_로_바꾼_링크는_옛_URL_이_다시_나와도_되돌아가지_않는다(api):
    from deskrpg_plugin import artifacts_tool as tool

    def save(**args):
        return json.loads(tool.make_handler(api)(args, task_id=None, session_id="s1"))

    first = save(kind="link", summary="s", url="https://x.io/a")
    moved = save(kind="link", summary="s", url="https://x.io/zzz", supersedes=first["artifact_id"])
    assert (moved["artifact_id"], moved["version"]) == (first["artifact_id"], 2)
    _fire(api, "바뀐 곳 https://x.io/zzz 예전 https://x.io/a")
    rows = {r["id"]: r for r in _links(api)}
    assert len(rows) == 2
    with contextlib.closing(store.open_registry(api)) as conn:
        versions = store.list_versions(conn, first["artifact_id"])
        assert [v["version"] for v in versions] == [1, 2]  # zzz 는 중복, a 는 이 아티팩트로 오지 않는다
        assert store.blob_path_for(api, versions[-1]).read_bytes() == b"https://x.io/zzz\n"
        assert rows[first["artifact_id"]]["title_norm"] == "https://x.io/zzz"
        assert rows[first["artifact_id"]]["title"] == "zzz"
        other = next(r for i, r in rows.items() if i != first["artifact_id"])
        assert other["title_norm"] == "https://x.io/a"

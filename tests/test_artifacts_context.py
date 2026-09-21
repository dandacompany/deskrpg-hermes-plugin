"""도구·훅이 받은 session_id·task_id 로 출처를 정한다. 모델 인자를 믿지 않는다."""
import types

from deskrpg_plugin import artifacts_context as ctx


def _api(tmp_path, home_rel="profiles/sophie", source=None, board="dev"):
    home = tmp_path / home_rel
    home.mkdir(parents=True, exist_ok=True)

    class _Sessions:
        def __init__(self, *a, **k): pass
        def get_session(self, sid): return {"id": sid, "source": source} if source else None
        def close(self): pass

    return types.SimpleNamespace(get_hermes_home=lambda: str(home), get_current_board=lambda: board, SessionDB=_Sessions)


def test_프로필은_홈_경로의_profiles_아래_이름이고_아니면_default(tmp_path):
    assert ctx.current_profile(_api(tmp_path)) == "sophie"
    assert ctx.current_profile(_api(tmp_path, home_rel="plain")) == "default"


def test_칸반_워커면_환경변수의_카드_id_와_현재_보드를_붙인다(tmp_path, monkeypatch):
    # 디스패처가 워커에 `HERMES_KANBAN_TASK=<카드 id>` 를 넣는다(kanban_db_dispatch.py). 카드 id 의 출처는 이것뿐이다.
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_4dafb79a")
    c = ctx.resolve_context(_api(tmp_path), session_id="20260921_125750_24ff1e")
    assert (c.source_kind, c.task_id, c.board, c.profile) == ("kanban", "t_4dafb79a", "dev", "sophie")


def test_채팅은_칸반이_아니다(tmp_path, monkeypatch):
    # 예전에는 훅이 받은 `task_id` 를 카드 id 로 믿었다. 그 값은 Hermes 의 실행 범위 id 이고 채팅에서도 늘 차 있다
    # (api_server: `effective_task_id = session_id or uuid4()`) — 채팅 결과물이 전부 "칸반" 으로 분류됐다.
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    c = ctx.resolve_context(_api(tmp_path, source="api_server"), session_id="20260921_125750_24ff1e")
    assert (c.source_kind, c.task_id, c.board) == ("chat", None, None)


def test_빈_환경변수는_칸반이_아니다(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "")
    assert ctx.resolve_context(_api(tmp_path), session_id="s1").source_kind == "chat"


def test_세션_source_가_cron_이면_cron_이고_아니면_chat(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert ctx.resolve_context(_api(tmp_path, source="cron"), session_id="s1").source_kind == "cron"
    assert ctx.resolve_context(_api(tmp_path, source="desktop"), session_id="s1").source_kind == "chat"


def test_세션_DB_를_못_열어도_chat_으로_떨어지고_던지지_않는다(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    api = _api(tmp_path)
    def boom(*a, **k): raise OSError("locked")
    api.SessionDB = boom
    assert ctx.resolve_context(api, session_id="s1").source_kind == "chat"

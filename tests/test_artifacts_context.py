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


def test_task_id_가_있으면_kanban_이고_현재_보드를_붙인다(tmp_path):
    c = ctx.resolve_context(_api(tmp_path), session_id="s1", task_id="t9")
    assert (c.source_kind, c.task_id, c.board, c.profile) == ("kanban", "t9", "dev", "sophie")


def test_세션_source_가_cron_이면_cron_이고_아니면_chat(tmp_path):
    assert ctx.resolve_context(_api(tmp_path, source="cron"), session_id="s1", task_id=None).source_kind == "cron"
    assert ctx.resolve_context(_api(tmp_path, source="desktop"), session_id="s1", task_id=None).source_kind == "chat"


def test_세션_DB_를_못_열어도_chat_으로_떨어지고_던지지_않는다(tmp_path):
    api = _api(tmp_path)
    def boom(*a, **k): raise OSError("locked")
    api.SessionDB = boom
    assert ctx.resolve_context(api, session_id="s1", task_id=None).source_kind == "chat"

import sys
import types
import pytest

from deskrpg_plugin import _hermes_api


def _install(monkeypatch, missing=(), skip_modules=()):
    """`SPEC` + `OPTIONAL_SPEC` 의 모든 모듈을 가짜로 깐다.

    `missing` 에 든 이름만 그 모듈에서 빠뜨린다. `skip_modules` 에 든 모듈 경로는
    sys.modules 에 아예 넣지 않는다 — 모듈 자체가 없는 구버전 Hermes 를 흉내 낸다.
    """
    for pkg in ("hermes_cli", "cron", "gateway", "gateway.platforms"):
        monkeypatch.setitem(sys.modules, pkg, types.ModuleType(pkg))
    for module_path, names in (*_hermes_api.SPEC, *_hermes_api.OPTIONAL_SPEC):
        if module_path in skip_modules:
            continue
        mod = types.ModuleType(module_path)
        for name in names:
            if name not in missing:
                setattr(mod, name, lambda *a, **k: None)
        monkeypatch.setitem(sys.modules, module_path, mod)


def test_모든_심볼이_있으면_로드된다(monkeypatch):
    _install(monkeypatch)
    api = _hermes_api.load()
    for name in _hermes_api.REQUIRED:
        assert getattr(api, name) is not None


def test_REQUIRED_는_SPEC_의_모든_이름이다():
    assert set(_hermes_api.REQUIRED) == {n for _m, names in _hermes_api.SPEC for n in names}
    assert len(_hermes_api.REQUIRED) == len(set(_hermes_api.REQUIRED))


def test_심볼이_하나라도_없으면_던진다(monkeypatch):
    # 반쯤 동작하는 상태를 만들지 않는다 — 목록은 되는데 삭제만 조용히 실패하는 식.
    _install(monkeypatch, missing=("delete_profile",))
    with pytest.raises(_hermes_api.MissingHermesApi) as excinfo:
        _hermes_api.load()
    assert "delete_profile" in str(excinfo.value)


@pytest.mark.parametrize("name", [
    "create_task",          # hermes_cli.kanban_db
    "connect_closing",      # hermes_cli.kanban_db_connect
    "dispatch_once",        # hermes_cli.kanban_db_dispatch
    "_check_dispatcher_presence",  # hermes_cli.kanban
    "get_timezone",         # hermes_time
    "list_jobs",            # cron.jobs
    "CATALOG",              # cron.blueprint_catalog
    "MAX_REQUEST_BYTES",    # gateway.platforms.api_server
    "SessionDB",            # hermes_state
])
def test_새_심볼이_하나라도_없어도_던진다(monkeypatch, name):
    # 0.6.0 이 더한 심볼도 같은 규칙이다 — 프로필은 되는데 칸반만 500 을 내는 상태를 막는다.
    _install(monkeypatch, missing=(name,))
    with pytest.raises(_hermes_api.MissingHermesApi) as excinfo:
        _hermes_api.load()
    assert name in str(excinfo.value)


def test_새_모듈이_아예_없으면_던진다(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setitem(sys.modules, "cron.jobs", None)
    with pytest.raises(_hermes_api.MissingHermesApi) as excinfo:
        _hermes_api.load()
    assert "cron.jobs" in str(excinfo.value)


def test_hermes_가_아예_없으면_던진다(monkeypatch):
    # 실제 Hermes 가 설치된 venv 에서(통합 테스트가 같은 프로세스에서 먼저 돌았을 때) `hermes_cli.profiles`
    # 가 이미 sys.modules 에 캐시돼 있으면 부모를 None 으로 둬도 `import_module` 이 캐시를 그대로 돌려준다.
    # 캐시를 걷어내야 "Hermes 없음" 이 진짜로 재현된다 — 가짜 venv 에서는 걷어낼 것이 없어 같은 결과다.
    for key in list(sys.modules):
        if key == "hermes_cli" or key.startswith("hermes_cli."):
            monkeypatch.delitem(sys.modules, key)
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    with pytest.raises(_hermes_api.MissingHermesApi):
        _hermes_api.load()


def test_선택_심볼이_빠져도_로드된다(monkeypatch):
    _install(monkeypatch, missing=("create_swarm",))
    api = _hermes_api.load()
    assert api.create_swarm is None
    # 필수 심볼은 그대로 살아 있다 — 스웜 하나 때문에 칸반이 죽지 않는다.
    for name in _hermes_api.REQUIRED:
        assert getattr(api, name) is not None


def test_선택_모듈이_통째로_없어도_로드된다(monkeypatch):
    _install(monkeypatch, skip_modules=("hermes_cli.kanban_swarm",))
    api = _hermes_api.load()
    for name in _hermes_api.OPTIONAL:
        assert getattr(api, name) is None
    for name in _hermes_api.REQUIRED:
        assert getattr(api, name) is not None


def test_필수_심볼이_빠지면_여전히_실패한다(monkeypatch):
    _install(monkeypatch, missing=("create_task",))
    with pytest.raises(_hermes_api.MissingHermesApi):
        _hermes_api.load()

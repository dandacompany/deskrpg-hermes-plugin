"""실제 Hermes 위의 통합 테스트(T2) 공통 설비.

**임시 홈만 쓴다.** `HERMES_HOME`·`HERMES_KANBAN_HOME` 을 Hermes 모듈을 import 하기 **전에** 임시 폴더로
돌려놓는다 — `hermes_constants.get_default_hermes_root()` 의 memo 는 env 에 키가 걸려 있어 다시 계산되지만,
import 시점에 경로를 굳히는 모듈(`cron.jobs.CRON_DIR` 등)이 있어 순서가 중요하다. 그 뒤 매 테스트마다
`tmp_path` 아래 새 홈으로 다시 가리키고, 실제 `~/.hermes` 아래가 아님을 단정한다.

`hermes_cli` 가 없으면 이 디렉토리 전체가 skip 된다(가짜 venv). `HERMES_INTEGRATION_REQUIRED=1` 이면
그 skip 이 실패가 된다 — 상위 `tests/conftest.py` 의 훅.
"""

import atexit
import importlib.util
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Hermes 가 없는 venv(가짜 스위트)에서는 임시 폴더를 만들기 **전에** skip 한다 — 아래 mkdtemp 가 먼저 돌면
# 매 실행마다 폴더 하나가 남는다.
if importlib.util.find_spec("hermes_cli") is None:
    pytest.importorskip("hermes_cli")

# Hermes 를 import 하기 전에 홈을 임시 폴더로. 이 값은 부트스트랩용이고, 각 테스트는 fixture 로 다시 바꾼다.
# 세션 fixture(tmp_path_factory)는 import 시점에 없으므로 mkdtemp 를 쓰되, 인터프리터가 끝날 때 반드시 지운다.
_BOOTSTRAP = Path(tempfile.mkdtemp(prefix="deskrpg-hermes-it-"))
atexit.register(shutil.rmtree, _BOOTSTRAP, True)
os.environ["HERMES_HOME"] = str(_BOOTSTRAP / "home")
os.environ["HERMES_KANBAN_HOME"] = str(_BOOTSTRAP / "kanban")
(_BOOTSTRAP / "home").mkdir(parents=True, exist_ok=True)

pytest.importorskip("hermes_cli")

from aiohttp import web  # noqa: E402

from deskrpg_plugin import _hermes_api, routes  # noqa: E402
from tests.conftest import FakeAdapter  # noqa: E402

pytestmark = pytest.mark.integration


def pytest_collection_modifyitems(items):
    for item in items:
        if "/integration/" in str(item.fspath).replace(os.sep, "/"):
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """테스트마다 새 홈. `{"home", "kanban"}` 경로를 돌려준다."""
    home = tmp_path / "home"
    kanban = tmp_path / "kanban"
    home.mkdir()
    kanban.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban))
    # 셸에 있던 `*_API_KEY` 를 Hermes 가 자격증명 풀에 "ingest" 한다(실측: OPENROUTER_API_KEY) — 임시 홈이라
    # 실제 파일은 안전하지만, 로컬과 CI 의 결과가 달라지므로 테스트 프로세스 안에서만 지운다.
    for name in [k for k in os.environ if k.endswith("_API_KEY")]:
        monkeypatch.delenv(name, raising=False)
    import hermes_constants

    resolved = Path(hermes_constants.get_hermes_home()).resolve()
    assert resolved == home.resolve(), f"Hermes 홈이 임시 폴더가 아니다: {resolved}"
    real_home = Path.home() / ".hermes"
    assert not resolved.is_relative_to(real_home.resolve()) if real_home.exists() else True
    return {"home": home, "kanban": kanban}


@pytest.fixture
def api(hermes_env):
    return _hermes_api.load()


@pytest.fixture
async def client(aiohttp_client, api):
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), api)
    return await aiohttp_client(app)


@pytest.fixture
def profile(api, hermes_env):
    """임시 홈 아래 실제 프로필 `sophie` — `create_profile` 은 headless 로 동작한다(실측)."""
    home = api.create_profile("sophie")
    assert Path(home).is_relative_to(hermes_env["home"])
    return "sophie"


@pytest.fixture
def hermes_home(hermes_env):
    """default 프로필 홈 — `hermes_env` 가 가리키는 같은 임시 폴더."""
    return hermes_env["home"]


@pytest.fixture
def make_profile(api, hermes_env):
    """이름을 받아 임시 홈 아래 실제 프로필을 만들고 그 홈 경로를 돌려주는 팩토리."""

    def _make(name):
        api.create_profile(name, no_alias=True)
        home = Path(api.get_profile_dir(name))
        assert home.is_relative_to(hermes_env["home"]), f"프로필 홈이 임시 폴더 밖이다: {home}"
        return home

    return _make

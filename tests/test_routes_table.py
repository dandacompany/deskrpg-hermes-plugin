"""라우트 테이블(`routes.ROUTES`)·매니페스트가 spec 과 일치하는지 고정한다."""

from deskrpg_plugin import routes


# spec §4–§7 에서 옮겨 적은 계약 라우트 전부. `routes.ROUTES` 는 이 집합과 **정확히** 같아야 한다 —
# 행을 빼먹으면 DeskRPG 화면 하나가 404 로 죽고, 스코프를 틀리면 프로필 키로 소유자 라우트가 열리거나
# 그 반대가 된다. 여기서 도출하지 않고 손으로 적는 이유가 그것이다.
_OWNER = routes.Scope.DEFAULT
_PROFILE = routes.Scope.PROFILE
EXPECTED_ROUTES = {
    # 0.5.0 까지의 아홉 개 (V2 — 동작·응답 불변)
    ("GET", "/deskrpg/info", _OWNER),
    ("GET", "/deskrpg/profiles", _OWNER),
    ("POST", "/deskrpg/profiles", _OWNER),
    ("DELETE", "/deskrpg/profiles/{name}", _OWNER),
    ("GET", "/p/{profile}/deskrpg/identity", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/identity", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/config", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/config", _PROFILE),
    # catalog 는 프로필 스코프다. 실측(2026-09-07)에서는 프로필끼리 인증 상태가
    # 같지만(루트 auth 폴백), 프로필이 자기 자격증명을 갖는 순간 갈린다 —
    # 그때 스코프를 좁히면 이미 쓰던 화면이 깨지므로 처음부터 좁게 둔다.
    ("GET", "/p/{profile}/deskrpg/catalog", _PROFILE),
    # 0.9.0 — 직원 설정 피커: 프로필 홈 스코프의 툴셋·스킬 목록.
    ("GET", "/p/{profile}/deskrpg/toolsets", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/skills", _PROFILE),
    # §5.1 보드
    ("GET", "/deskrpg/kanban/boards", _OWNER),
    ("POST", "/deskrpg/kanban/boards", _OWNER),
    ("PATCH", "/deskrpg/kanban/boards/{slug}", _OWNER),
    # §5.2 보드 보기
    ("GET", "/deskrpg/kanban/board", _OWNER),
    # §5.3 카드
    ("POST", "/deskrpg/kanban/tasks", _OWNER),
    ("GET", "/deskrpg/kanban/tasks/{task_id}", _OWNER),
    ("PATCH", "/deskrpg/kanban/tasks/{task_id}", _OWNER),
    ("DELETE", "/deskrpg/kanban/tasks/{task_id}", _OWNER),
    ("POST", "/deskrpg/kanban/tasks/{task_id}/comments", _OWNER),
    # §5.4 동작 — 열 개 이름을 한 와일드카드 행이 받는다(모르는 이름 404)
    ("POST", "/deskrpg/kanban/tasks/{id}/{action}", _OWNER),
    # §5.5 첨부·링크·디스패치·로그
    ("GET", "/deskrpg/kanban/tasks/{id}/attachments", _OWNER),
    ("POST", "/deskrpg/kanban/tasks/{id}/attachments", _OWNER),
    ("GET", "/deskrpg/kanban/attachments/{id}", _OWNER),
    ("DELETE", "/deskrpg/kanban/attachments/{id}", _OWNER),
    ("POST", "/deskrpg/kanban/links", _OWNER),
    ("DELETE", "/deskrpg/kanban/links", _OWNER),
    ("POST", "/deskrpg/kanban/dispatch", _OWNER),
    # §5.7 스웜 — 심볼이 없는 Hermes 빌드에서는 `routes.routes_for()` 가 걸러낸다(등록 안 함).
    ("POST", "/deskrpg/kanban/swarm", _OWNER),
    ("GET", "/deskrpg/kanban/tasks/{id}/blackboard", _OWNER),
    ("GET", "/deskrpg/kanban/tasks/{id}/log", _OWNER),
    # §5.6 운영 설정·프로필
    ("GET", "/deskrpg/kanban/orchestration", _OWNER),
    ("PUT", "/deskrpg/kanban/orchestration", _OWNER),
    ("GET", "/deskrpg/kanban/profiles", _OWNER),
    # §6 사건
    ("GET", "/deskrpg/events", _OWNER),
    # §7 크론 (프로필 키)
    ("GET", "/p/{profile}/deskrpg/cron/jobs", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/cron/jobs", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/cron/jobs/{id}", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/cron/jobs/{id}", _PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/cron/jobs/{id}", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/cron/jobs/{id}/runs", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/cron/jobs/{id}/pause", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/cron/jobs/{id}/resume", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/cron/jobs/{id}/run", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/cron/delivery-targets", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/cron/blueprints", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/cron/blueprints/instantiate", _PROFILE),
    # §8 아티팩트 (소유자 키)
    ("GET", "/deskrpg/artifacts", _OWNER),
    ("GET", "/deskrpg/artifacts/{artifact_id}", _OWNER),
    ("GET", "/deskrpg/artifacts/{artifact_id}/versions/{v}/content", _OWNER),
    ("POST", "/deskrpg/artifacts/{artifact_id}/versions", _OWNER),
    ("POST", "/deskrpg/artifacts/{artifact_id}/rework", _OWNER),
    ("DELETE", "/deskrpg/artifacts/{artifact_id}", _OWNER),
}


def test_라우트_테이블이_스펙의_쉰세_개와_스코프까지_정확히_같다():
    assert len(EXPECTED_ROUTES) == 53
    assert len(routes.ROUTES) == 53, "행 수가 다르다 — 중복 행이거나 빠진 행이다"
    assert {(m, p, s) for m, p, _h, s in routes.ROUTES} == EXPECTED_ROUTES


def test_소유자_라우트는_34_개_프로필_라우트는_19_개다():
    by_scope = {}
    for _m, _p, _h, scope in routes.ROUTES:
        by_scope[scope] = by_scope.get(scope, 0) + 1
    # 소유자: 기존 4 + 칸반 21 + 스웜 2 + 사건 1 + 아티팩트 6 = 34 ·
    # 프로필: 기존 5 + 크론 12 + 0.9.0 피커 2 = 19.
    assert by_scope == {routes.Scope.DEFAULT: 4 + 21 + 2 + 1 + 6, routes.Scope.PROFILE: 5 + 12 + 2}


def test_고정_세그먼트_카드_라우트가_action_와일드카드보다_앞에_있다():
    # aiohttp 는 같은 프리픽스 안에서 등록 순서대로 첫 매치를 고른다. comments/attachments/log 가
    # `{action}` 뒤에 오면 그 POST 들이 동작 디스패처로 흘러 404 가 된다.
    paths = [p for _m, p, _h, _s in routes.ROUTES]
    wildcard = paths.index("/deskrpg/kanban/tasks/{id}/{action}")
    for fixed in (
        "/deskrpg/kanban/tasks/{task_id}/comments",
        "/deskrpg/kanban/tasks/{id}/attachments",
        "/deskrpg/kanban/tasks/{id}/log",
    ):
        assert paths.index(fixed) < wildcard, f"{fixed} 가 와일드카드 뒤에 있다"


def test_모든_행의_핸들러_이름이_팩토리_매핑에_있다(fake_api):
    for _m, _p, name, _s in routes.ROUTES:
        assert callable(routes.handler_for(name, fake_api)), name




def test_보고하는_버전이_plugin_yaml_과_일치한다():
    """같은 사실이 두 곳에 적히면 반드시 갈라진다.

    실제로 갈렸다: 버전을 0.2.0·0.3.0 으로 두 번 올리는 동안 `routes.py` 의 하드코딩된
    문자열을 두 번 다 놓쳐 `/deskrpg/info` 가 계속 `0.1.0` 을 보고했고, 스테이징에서
    DeskRPG 가 그 값을 캐시하는 것을 보고서야 드러났다.
    """
    import pathlib
    import re

    from deskrpg_plugin.routes import PLUGIN_VERSION

    raw = (pathlib.Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(
        encoding="utf-8"
    )
    declared = re.search(r"^version:\s*(.+)$", raw, re.M).group(1).strip().strip("\"'")
    assert PLUGIN_VERSION == declared
    assert PLUGIN_VERSION != "unknown"


def test_plugin_yaml_이_requires_hermes_를_최상위에_선언하고_버전은_0_8_0_이다():
    """`requires: {hermes: …}` 처럼 중첩하면 Hermes 는 모르는 키로 무시한다 — 실제 필드는
    최상위 `requires_hermes` 다(hermes_cli/plugin_validate.py). 무시되면 0.20.x 에 설치돼도
    경고 없이 로드된 뒤 칸반 심볼이 없어 죽는다.
    """
    import pathlib

    import yaml

    raw = (pathlib.Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")
    manifest = yaml.safe_load(raw)
    assert manifest["version"] == "0.9.0"
    assert manifest["requires_hermes"] == ">=0.21.1"
    assert "requires" not in manifest


def test_프로필_라우트가_포괄_라우트_앞으로_올라간다(fake_api):
    """Hermes 의 `/p/{profile}/{tail:.*}` 가 먼저 등록돼 있어도 우리 라우트가 이긴다.

    스테이징 실측: 이 순서를 안 고치면 `/p/sophie/deskrpg/cron/jobs` 가
    포괄 라우트에 걸려 404 "Unknown or unconfigured profile" 이 된다.
    """
    from aiohttp import web

    from deskrpg_plugin.routes import attach
    from tests.conftest import FakeAdapter

    app = web.Application()

    async def _catchall(request):  # pragma: no cover - 매칭되면 테스트가 실패한다
        return web.json_response({"error": "Unknown or unconfigured profile"}, status=404)

    app.router.add_route("*", "/p/{profile}/{tail:.*}", _catchall)
    attach(app, FakeAdapter(authorized=True), fake_api)

    canonicals = [r.canonical for r in app.router._resources]
    catchall_index = next(i for i, c in enumerate(canonicals) if c.startswith("/p/") and c.endswith("/{tail}"))
    profile_indexes = [i for i, c in enumerate(canonicals) if c.startswith("/p/{profile}/deskrpg/")]
    assert profile_indexes, "프로필 스코프 라우트가 하나도 등록되지 않았다"
    assert max(profile_indexes) < catchall_index

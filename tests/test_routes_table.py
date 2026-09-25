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
    ("POST", "/deskrpg/worker-plugin", _OWNER),
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
    # 0.15.0 — 스킬 Hub(검색·미리보기·설치·설치 작업 조회·삭제·업데이트).
    ("GET", "/p/{profile}/deskrpg/skills/hub/search", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/skills/hub/preview", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/skills/hub/installs", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/skills/hub/installs/{job_id}", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/skills/hub/uninstall", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/skills/hub/update", _PROFILE),
    # 0.15.0 — curator 상태·일시정지·실행·실행 작업 조회, 학습 관계도·노드 조회·편집·삭제.
    ("GET", "/p/{profile}/deskrpg/curator", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/curator/paused", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/curator/runs", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/curator/runs/{job_id}", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/learning/graph", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/learning/node", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/learning/node", _PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/learning/node", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/skills", _PROFILE),
    # 0.15.0 NPC 스킬 관리 — 스킬 CRUD
    ("GET", "/p/{profile}/deskrpg/skills/{name}", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/skills/{name}/file", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/skills", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/skills/enabled", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/skills/{name}/file", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/skills/{name}/enabled", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/skills/archive", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/skills/archive/{name}/restore", _PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/skills/archive/{name}", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/skills/{name}/pinned", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/skills/{name}/archive", _PROFILE),
    # 0.10.0 — 도구별 프로바이더 선택·키 입력. 대시보드 도구 설정 심볼이 없는 빌드에서는 라우트가 없다.
    ("GET", "/p/{profile}/deskrpg/toolsets/{toolset}/providers", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/toolsets/{toolset}/provider", _PROFILE),
    # 프로바이더 API 키 입력 — 쓰기 전용, PROVIDER_REGISTRY 없는 빌드에서는 라우트가 없다.
    ("PUT", "/p/{profile}/deskrpg/provider-keys/{provider}", _PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/provider-keys/{provider}", _PROFILE),
    # OAuth 디바이스 로그인 — Hermes 세션 위임, 디바이스 로그인 심볼이 없는 빌드에서는 라우트가 없다.
    ("POST", "/p/{profile}/deskrpg/oauth/{provider}/start", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/oauth/{provider}/sessions/{session_id}", _PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/oauth/sessions/{session_id}", _PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/oauth/{provider}", _PROFILE),
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
    ("GET", "/deskrpg/kanban/attachments", _OWNER),
    ("GET", "/deskrpg/kanban/attachments/{id}", _OWNER),
    ("DELETE", "/deskrpg/kanban/attachments/{id}", _OWNER),
    ("GET", "/deskrpg/kanban/links", _OWNER),
    ("GET", "/deskrpg/kanban/runs", _OWNER),
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
    ("POST", "/deskrpg/events/handoff", _OWNER),
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
    # 카드 제안 해소 (소유자 키) — 등록 여부는 사용자가 고르고, 이 라우트가 그 선택을 한 번만 받는다.
    ("POST", "/deskrpg/card-proposals/{proposal_id}/resolve", _OWNER),
    # 카드 생성이 실패했을 때의 롤백 — 카드가 기록된 제안은 되돌리지 않는다.
    ("POST", "/deskrpg/card-proposals/{proposal_id}/unresolve", _OWNER),
    # 만들어진 카드 id 를 사후에 적는다 — 그 순간부터 unresolve 가 막힌다(카드 중복 이중 방어).
    ("POST", "/deskrpg/card-proposals/{proposal_id}/task", _OWNER),
    # 0.17.0 NPC MCP 커넥터 관리 (프로필 키)
    ("GET", "/p/{profile}/deskrpg/mcp/jobs/{job_id}", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/mcp/oauth/{session_id}/callback", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/mcp/oauth/{session_id}", _PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/mcp/oauth/{session_id}", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/mcp/catalog", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/mcp/catalog/{entry}/install", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/mcp/reload", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/mcp/export/{name}", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/mcp/servers", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/mcp/servers", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/mcp/servers/{name}", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/mcp/servers/{name}", _PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/mcp/servers/{name}", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/mcp/servers/{name}/enabled", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/mcp/servers/{name}/trust", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/mcp/servers/{name}/tools", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/mcp/servers/{name}/secrets/{key}", _PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/mcp/servers/{name}/secrets/{key}", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/mcp/servers/{name}/test", _PROFILE),
    ("GET", "/p/{profile}/deskrpg/mcp/servers/{name}/tools", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/mcp/servers/{name}/oauth", _PROFILE),
    # 0.18.0 무인 실행 승인 정책 (프로필 키)
    ("GET", "/p/{profile}/deskrpg/approval-policy", _PROFILE),
    ("PUT", "/p/{profile}/deskrpg/approval-policy", _PROFILE),
    ("POST", "/p/{profile}/deskrpg/approval-policy/allowlist", _PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/approval-policy/allowlist", _PROFILE),
}


def test_라우트_테이블이_스펙의_예순여덟_개와_스코프까지_정확히_같다():
    assert len(EXPECTED_ROUTES) == 119
    assert len(routes.ROUTES) == 119, "행 수가 다르다 — 중복 행이거나 빠진 행이다"
    assert {(m, p, s) for m, p, _h, s in routes.ROUTES} == EXPECTED_ROUTES


def test_소유자_라우트는_41_개_프로필_라우트는_27_개다():
    by_scope = {}
    for _m, _p, _h, scope in routes.ROUTES:
        by_scope[scope] = by_scope.get(scope, 0) + 1
    # 소유자: 기존 4 + 워커 플러그인 1 + 칸반 21 + 보드 첨부 목록 1 + 뷰 묶음 조회 2 + 스웜 2 + 사건 1 + 아티팩트 6 + 카드 제안 3 = 41 ·
    # 프로필: 기존 5 + 크론 12 + 0.9.0 피커 2 + 프로바이더 키 2 + OAuth 4 + 0.10.0 도구 프로바이더 2
    #   + 0.15.0 스킬 CRUD 11 + 스킬 Hub 6 + curator·관계도 8 + 0.17.0 MCP 21 + 0.18.0 승인 정책 4 = 77.
    assert by_scope == {routes.Scope.DEFAULT: 4 + 1 + 21 + 1 + 2 + 2 + 2 + 6 + 3,
                        routes.Scope.PROFILE: 5 + 12 + 2 + 2 + 4 + 2 + 11 + 6 + 8 + 21 + 4}


def test_OAuth_취소_행이_연결_끊기_행보다_앞에_있다():
    # 둘 다 `/p/{profile}/deskrpg/oauth/` 아래 DELETE 다. 취소를 앞에 둬 의도를 고정한다.
    rows = [(m, p) for m, p, _h, _s in routes.ROUTES]
    cancel = rows.index(("DELETE", "/p/{profile}/deskrpg/oauth/sessions/{session_id}"))
    disconnect = rows.index(("DELETE", "/p/{profile}/deskrpg/oauth/{provider}"))
    assert cancel < disconnect


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


def test_스킬_고정_세그먼트는_이름_와일드카드보다_먼저다():
    # `/skills/hub/search` 는 `/skills/{name}/file` 과 세그먼트 수가 같다. aiohttp 는 등록 순서대로 첫 매치를
    # 고르므로 고정 행이 뒤에 오면 `hub`·`archive`·`enabled` 가 스킬 이름으로 잡힌다.
    paths = [p for _m, p, _h, _s in routes.ROUTES]
    first_wild = min(i for i, p in enumerate(paths) if p.startswith("/p/{profile}/deskrpg/skills/{name}"))
    fixed = [p for p in paths if p.startswith("/p/{profile}/deskrpg/skills/hub/")]
    assert len(fixed) == 6
    for p in (*fixed, "/p/{profile}/deskrpg/skills/archive", "/p/{profile}/deskrpg/skills/enabled"):
        assert paths.index(p) < first_wild, f"{p} 가 스킬 이름 와일드카드 뒤에 있다"


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
    assert manifest["version"] == "0.18.0"
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


# F3 — `get_toolsets`/`get_skills` 라우트 등록은 `profile_toolsets`/`profile_skills`
# capability 와 같은 심볼 집합(has_toolset_symbols/has_skill_symbols)을 봐야 한다.
# 기존에는 단일 심볼(`_get_platform_tools`/`_find_all_skills`)만 봐서, 그 심볼만 있고
# 나머지가 빠진 반쪽짜리 빌드에서도 라우트는 뜨는데 capability 는 없다고 광고하는
# 모순이 생겼다.


def test_get_toolsets_라우트는_capability_와_같은_심볼_집합을_본다(fake_api):
    from deskrpg_plugin.contract_fields import capabilities

    # _get_platform_tools 는 남기고, has_toolset_symbols 를 구성하는 다른 심볼 하나만 뺀다.
    fake_api._toolset_has_keys = None
    paths = [p for _m, p, _h, _s in routes.routes_for(fake_api)]
    assert "/p/{profile}/deskrpg/toolsets" not in paths
    assert "profile_toolsets" not in capabilities(fake_api)


def test_get_skills_라우트는_capability_와_같은_심볼_집합을_본다(fake_api):
    from deskrpg_plugin.contract_fields import capabilities

    # _find_all_skills 는 남기고, has_skill_symbols 를 구성하는 다른 심볼 하나만 뺀다.
    fake_api._sort_skills = None
    paths = [p for _m, p, _h, _s in routes.routes_for(fake_api)]
    assert "/p/{profile}/deskrpg/skills" not in paths
    assert "profile_skills" not in capabilities(fake_api)


def test_hub_curator_관계도_라우트는_profile_skill_admin_과_같은_심볼_집합을_본다(fake_api):
    from deskrpg_plugin.contract_fields import capabilities

    names = ("/skills/hub/", "/curator", "/learning/")
    all_paths = [p for _m, p, _h, _s in routes.routes_for(fake_api)]
    assert sum(any(n in p for n in names) for p in all_paths) == 14
    fake_api.build_learning_graph = None
    paths = [p for _m, p, _h, _s in routes.routes_for(fake_api)]
    assert not [p for p in paths if any(n in p for n in names)]
    assert "profile_skill_admin" not in capabilities(fake_api)


def test_MCP_고정_세그먼트는_서버_이름_와일드카드보다_먼저다():
    paths = [p for _m, p, _h, _s in routes.ROUTES]
    first_wild = min(i for i, p in enumerate(paths) if p.startswith("/p/{profile}/deskrpg/mcp/servers/{name}"))
    for p in ("/p/{profile}/deskrpg/mcp/jobs/{job_id}", "/p/{profile}/deskrpg/mcp/catalog",
              "/p/{profile}/deskrpg/mcp/reload", "/p/{profile}/deskrpg/mcp/servers"):
        assert paths.index(p) < first_wild, f"{p} 가 서버 이름 와일드카드 뒤에 있다"


def test_MCP_라우트는_profile_mcp_admin_과_같은_심볼_집합을_본다(fake_api):
    from deskrpg_plugin.contract_fields import capabilities

    assert sum(p.startswith("/p/{profile}/deskrpg/mcp/") for _m, p, _h, _s in routes.routes_for(fake_api)) == 21
    fake_api._connect_server = None
    assert not [p for _m, p, _h, _s in routes.routes_for(fake_api) if "/deskrpg/mcp/" in p]
    assert "profile_mcp_admin" not in capabilities(fake_api)

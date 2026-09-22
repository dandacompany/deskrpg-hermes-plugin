"""라우트 선언 테이블과 등록.

라우트를 흩어 놓지 않고 테이블 하나에 모은다. 등록은 테이블을 돌며 전부
require_auth 로 감싸므로, 핸들러를 빠뜨릴 자리가 없다.
"""

import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .auth import Scope, require_auth
from . import contract_fields as _contract_fields
from . import identity as _identity
from . import profiles as _profiles
from . import worker_plugin as _worker_plugin
from . import config as _config
from . import catalog as _catalog
from . import picker as _picker
from . import tool_providers as _tool_providers
from . import provider_keys as _provider_keys
from . import oauth as _oauth
from . import kanban_board as _kanban_board
from . import kanban_views as _kanban_views
from . import kanban_actions as _kanban_actions
from . import kanban_files as _kanban_files
from . import kanban_ops as _kanban_ops
from . import kanban_swarm as _kanban_swarm
from . import cron as _cron
from . import events as _events
from . import artifacts_routes as _artifacts_routes
from . import card_proposal_routes as _card_proposal_routes
from .artifacts_tool import artifact_upload_max_bytes as _artifact_upload_max_bytes


def _read_plugin_version() -> str:
    """`plugin.yaml` 의 version 을 정본으로 읽는다.

    예전에는 이 값을 여기 문자열로 박아 뒀는데, 버전을 두 번 올리는 동안(0.2.0·0.3.0)
    **두 번 다 여기를 놓쳐** `/deskrpg/info` 가 계속 `0.1.0` 을 보고했다. 스테이징에서
    DeskRPG 가 그 값을 캐시하는 걸 보고서야 드러났다 — 같은 사실이 두 곳에 적혀 있으면
    반드시 갈라진다.

    yaml 파서를 쓰지 않는다. 이 플러그인은 Hermes 가 주는 것 외에 의존을 두지 않고,
    필요한 것은 최상위 `version:` 한 줄이다. 읽지 못하면 예외를 던지지 않고
    `"unknown"` 을 돌려준다 — 버전을 모르는 것이 라우트를 못 뜨게 할 이유는 아니다.
    """
    try:
        for line in (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return "unknown"


logger = logging.getLogger("deskrpg_plugin")

PLUGIN_VERSION = _read_plugin_version()

# (method, path, handler_name, scope)
ROUTES = [
    ("GET", "/deskrpg/info", "info", Scope.DEFAULT),
    ("GET", "/deskrpg/profiles", "list_profiles", Scope.DEFAULT),
    ("POST", "/deskrpg/profiles", "create_profile", Scope.DEFAULT),
    ("DELETE", "/deskrpg/profiles/{name}", "delete_profile", Scope.DEFAULT),
    # 워커(칸반·크론)는 프로필 홈으로 뜬다 — 그 홈에도 이 플러그인이 있게 한다. 명시 호출 전용(worker_plugin 모듈 주석).
    ("POST", "/deskrpg/worker-plugin", "ensure_worker_plugin", Scope.DEFAULT),
    ("GET", "/p/{profile}/deskrpg/identity", "get_identity", Scope.PROFILE),
    ("PUT", "/p/{profile}/deskrpg/identity", "put_identity", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/config", "get_config", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/catalog", "get_catalog", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/toolsets", "get_toolsets", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/skills", "get_skills", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/toolsets/{toolset}/providers", "get_tool_providers", Scope.PROFILE),
    ("PUT", "/p/{profile}/deskrpg/toolsets/{toolset}/provider", "put_tool_provider", Scope.PROFILE),
    ("PUT", "/p/{profile}/deskrpg/provider-keys/{provider}", "put_provider_key", Scope.PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/provider-keys/{provider}", "delete_provider_key", Scope.PROFILE),
    # OAuth 디바이스 로그인 — Hermes 세션 위임. 취소 행이 연결 끊기 행보다 **먼저** 와야 한다:
    # 둘 다 DELETE 이고 `/oauth/sessions/x` 는 `/oauth/{provider}` 에도 맞지 않지만(세그먼트 수가 다르다)
    # 순서로 의도를 고정해 둔다.
    ("POST", "/p/{profile}/deskrpg/oauth/{provider}/start", "oauth_start", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/oauth/{provider}/sessions/{session_id}", "oauth_poll", Scope.PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/oauth/sessions/{session_id}", "oauth_cancel", Scope.PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/oauth/{provider}", "oauth_disconnect", Scope.PROFILE),
    ("PUT", "/p/{profile}/deskrpg/config", "put_config", Scope.PROFILE),
    # ---- 0.6.0 칸반 (소유자 키, spec §5) ------------------------------------------------
    # 칸반은 프로필과 무관한 호스트 공유 저장소(HERMES_KANBAN_HOME)라 전부 소유자 키다(C7).
    ("GET", "/deskrpg/kanban/boards", "kanban_list_boards", Scope.DEFAULT),
    ("POST", "/deskrpg/kanban/boards", "kanban_create_board", Scope.DEFAULT),
    ("PATCH", "/deskrpg/kanban/boards/{slug}", "kanban_patch_board", Scope.DEFAULT),
    ("GET", "/deskrpg/kanban/board", "kanban_get_board", Scope.DEFAULT),
    ("POST", "/deskrpg/kanban/tasks", "kanban_create_task", Scope.DEFAULT),
    ("GET", "/deskrpg/kanban/tasks/{task_id}", "kanban_get_task", Scope.DEFAULT),
    ("PATCH", "/deskrpg/kanban/tasks/{task_id}", "kanban_patch_task", Scope.DEFAULT),
    ("DELETE", "/deskrpg/kanban/tasks/{task_id}", "kanban_delete_task", Scope.DEFAULT),
    # `/tasks/{id}/<고정 세그먼트>` 는 아래 `{action}` 와일드카드보다 **앞에** 둔다 — aiohttp 는 같은
    # 프리픽스 안에서 등록 순서대로 첫 매치를 고르므로, 뒤에 두면 comments/attachments POST 가
    # 동작 디스패처로 흘러 404 가 난다.
    ("POST", "/deskrpg/kanban/tasks/{task_id}/comments", "kanban_add_comment", Scope.DEFAULT),
    ("GET", "/deskrpg/kanban/tasks/{id}/attachments", "kanban_list_attachments", Scope.DEFAULT),
    ("POST", "/deskrpg/kanban/tasks/{id}/attachments", "kanban_upload_attachment", Scope.DEFAULT),
    ("GET", "/deskrpg/kanban/tasks/{id}/log", "kanban_worker_log", Scope.DEFAULT),
    ("POST", "/deskrpg/kanban/tasks/{id}/{action}", "kanban_task_action", Scope.DEFAULT),
    # 보드 전체 첨부(결과물 갤러리). `/attachments/{id}` 와 세그먼트 수가 달라 충돌하지 않는다.
    ("GET", "/deskrpg/kanban/attachments", "kanban_list_board_attachments", Scope.DEFAULT),
    ("GET", "/deskrpg/kanban/attachments/{id}", "kanban_download_attachment", Scope.DEFAULT),
    ("DELETE", "/deskrpg/kanban/attachments/{id}", "kanban_delete_attachment", Scope.DEFAULT),
    # 묶음 조회 — 목록 앞에 둔다(고정 세그먼트가 와일드카드보다 앞이라는 이 표의 규칙과 무관하게,
    # 같은 경로의 GET 은 POST/DELETE 와 충돌하지 않는다. 읽기를 위에 모아 둔다).
    ("GET", "/deskrpg/kanban/links", "kanban_list_links", Scope.DEFAULT),
    ("GET", "/deskrpg/kanban/runs", "kanban_list_runs", Scope.DEFAULT),
    ("POST", "/deskrpg/kanban/links", "kanban_add_link", Scope.DEFAULT),
    ("DELETE", "/deskrpg/kanban/links", "kanban_remove_link", Scope.DEFAULT),
    ("POST", "/deskrpg/kanban/dispatch", "kanban_dispatch", Scope.DEFAULT),
    ("POST", "/deskrpg/kanban/swarm", "kanban_create_swarm", Scope.DEFAULT),
    ("GET", "/deskrpg/kanban/tasks/{id}/blackboard", "kanban_blackboard", Scope.DEFAULT),
    ("GET", "/deskrpg/kanban/orchestration", "kanban_get_orchestration", Scope.DEFAULT),
    ("PUT", "/deskrpg/kanban/orchestration", "kanban_put_orchestration", Scope.DEFAULT),
    ("GET", "/deskrpg/kanban/profiles", "kanban_profiles", Scope.DEFAULT),
    # ---- 0.6.0 사건 (소유자 키, spec §6) ------------------------------------------------
    ("GET", "/deskrpg/events", "events", Scope.DEFAULT),
    ("POST", "/deskrpg/events/handoff", "events_handoff", Scope.DEFAULT),
    # ---- 0.6.0 크론 (프로필 키, spec §7) ------------------------------------------------
    # 프로필 프리픽스 미러는 Hermes 가 자기 라우트에만 만들어 주므로 여기 직접 적는다(C1).
    ("GET", "/p/{profile}/deskrpg/cron/jobs", "cron_list_jobs", Scope.PROFILE),
    ("POST", "/p/{profile}/deskrpg/cron/jobs", "cron_create_job", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/cron/jobs/{id}", "cron_get_job", Scope.PROFILE),
    ("PUT", "/p/{profile}/deskrpg/cron/jobs/{id}", "cron_update_job", Scope.PROFILE),
    ("DELETE", "/p/{profile}/deskrpg/cron/jobs/{id}", "cron_delete_job", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/cron/jobs/{id}/runs", "cron_list_runs", Scope.PROFILE),
    ("POST", "/p/{profile}/deskrpg/cron/jobs/{id}/pause", "cron_pause", Scope.PROFILE),
    ("POST", "/p/{profile}/deskrpg/cron/jobs/{id}/resume", "cron_resume", Scope.PROFILE),
    ("POST", "/p/{profile}/deskrpg/cron/jobs/{id}/run", "cron_run", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/cron/delivery-targets", "cron_delivery_targets", Scope.PROFILE),
    ("GET", "/p/{profile}/deskrpg/cron/blueprints", "cron_blueprints", Scope.PROFILE),
    ("POST", "/p/{profile}/deskrpg/cron/blueprints/instantiate", "cron_instantiate_blueprint", Scope.PROFILE),
    # ---- 0.8.0 아티팩트 (소유자 키 — 호스트 공유 저장소) --------------------------------
    ("GET", "/deskrpg/artifacts", "artifacts_list", Scope.DEFAULT),
    ("GET", "/deskrpg/artifacts/{artifact_id}", "artifacts_get", Scope.DEFAULT),
    ("GET", "/deskrpg/artifacts/{artifact_id}/versions/{v}/content", "artifacts_content", Scope.DEFAULT),
    ("POST", "/deskrpg/artifacts/{artifact_id}/versions", "artifacts_add_version", Scope.DEFAULT),
    ("POST", "/deskrpg/artifacts/{artifact_id}/rework", "artifacts_rework", Scope.DEFAULT),
    ("DELETE", "/deskrpg/artifacts/{artifact_id}", "artifacts_delete", Scope.DEFAULT),
    # ---- 카드 제안 해소 (소유자 키 — 제안 저장소도 게이트웨이당 하나다) ------------------
    ("POST", "/deskrpg/card-proposals/{proposal_id}/resolve", "card_proposal_resolve", Scope.DEFAULT),
    ("POST", "/deskrpg/card-proposals/{proposal_id}/unresolve", "card_proposal_unresolve", Scope.DEFAULT),
    ("POST", "/deskrpg/card-proposals/{proposal_id}/task", "card_proposal_record_task", Scope.DEFAULT),
]

# Hermes 의 프로필 프리픽스 미들웨어는 `request.match_info.get("profile")` 로
# 인증 스코프를 정한다(gateway/platforms/api_server.py) — 우리가 만든 규칙이
# 아니라 물려받는 규칙이다(auth.py 의 Scope 주석과 같은 얘기). 그래서 경로의
# `{profile}` 이라는 정확한 이름이 Scope.PROFILE 과 실제로 맞물려 있는지는
# 매직 문자열에 의존한다(I-4) — 누가 `{profile_name}` 으로 바꾸면 라우트는
# 계속 200 을 내면서 인증만 조용히 default 키로 내려앉는다. import 시점에
# 바로 걸러 그 어긋남이 리뷰를 안 거치고 살아남을 수 없게 한다.
PROFILE_PATH_VAR = "{profile}"


def _assert_scope_matches_path():
    for method, path, handler_name, scope in ROUTES:
        has_profile_var = PROFILE_PATH_VAR in path
        if scope is Scope.PROFILE and not has_profile_var:
            raise AssertionError(
                f"{method} {path} ({handler_name}) 은 Scope.PROFILE 인데 "
                f"경로에 {PROFILE_PATH_VAR} 가 없다 — 인증이 default 키로 내려앉는다"
            )
        if scope is Scope.DEFAULT and has_profile_var:
            raise AssertionError(
                f"{method} {path} ({handler_name}) 은 Scope.DEFAULT 인데 "
                f"경로에 {PROFILE_PATH_VAR} 가 있다 — 프로필 키로 인증되어야 할 수 있다"
            )


_assert_scope_matches_path()

_HANDLERS = {
    "info": lambda api: _make_info(api),
    "ensure_worker_plugin": lambda api: _worker_plugin.ensure_handler(api),
    "list_profiles": lambda api: _profiles.list_handler(api),
    "create_profile": lambda api: _profiles.create_handler(api),
    "delete_profile": lambda api: _profiles.delete_handler(api),
    "get_identity": lambda api: _identity.get_handler(api),
    "put_identity": lambda api: _identity.put_handler(api),
    "get_config": lambda api: _config.get_handler(api),
    "put_config": lambda api: _config.put_handler(api),
    "get_catalog": lambda api: _catalog.get_handler(api),
    "get_toolsets": lambda api: _picker.toolsets_handler(api),
    "get_skills": lambda api: _picker.skills_handler(api),
    "get_tool_providers": lambda api: _tool_providers.providers_handler(api),
    "put_tool_provider": lambda api: _tool_providers.select_handler(api),
    "put_provider_key": lambda api: _provider_keys.put_handler(api),
    "delete_provider_key": lambda api: _provider_keys.delete_handler(api),
    "oauth_start": lambda api: _oauth.start_handler(api),
    "oauth_poll": lambda api: _oauth.poll_handler(api),
    "oauth_cancel": lambda api: _oauth.cancel_handler(api),
    "oauth_disconnect": lambda api: _oauth.disconnect_handler(api),
    # 칸반
    "kanban_list_boards": lambda api: _kanban_board.list_boards_handler(api),
    "kanban_create_board": lambda api: _kanban_board.create_board_handler(api),
    "kanban_patch_board": lambda api: _kanban_board.patch_board_handler(api),
    "kanban_get_board": lambda api: _kanban_board.get_board_handler(api),
    "kanban_create_task": lambda api: _kanban_board.create_task_handler(api),
    "kanban_get_task": lambda api: _kanban_board.get_task_handler(api),
    "kanban_patch_task": lambda api: _kanban_board.patch_task_handler(api),
    "kanban_delete_task": lambda api: _kanban_board.delete_task_handler(api),
    "kanban_add_comment": lambda api: _kanban_board.add_comment_handler(api),
    "kanban_list_attachments": lambda api: _kanban_files.list_attachments_handler(api),
    "kanban_list_board_attachments": lambda api: _kanban_files.list_board_attachments_handler(api),
    "kanban_upload_attachment": lambda api: _kanban_files.upload_attachment_handler(api),
    "kanban_worker_log": lambda api: _kanban_files.worker_log_handler(api),
    "kanban_task_action": lambda api: _make_task_action(api),
    "kanban_download_attachment": lambda api: _kanban_files.download_attachment_handler(api),
    "kanban_delete_attachment": lambda api: _kanban_files.delete_attachment_handler(api),
    "kanban_list_links": lambda api: _kanban_views.links_handler(api),
    "kanban_list_runs": lambda api: _kanban_views.runs_handler(api),
    "kanban_add_link": lambda api: _kanban_board.link_handler(api, "add"),
    "kanban_remove_link": lambda api: _kanban_board.link_handler(api, "remove"),
    "kanban_dispatch": lambda api: _kanban_ops.dispatch_handler(api),
    "kanban_create_swarm": lambda api: _kanban_swarm.create_swarm_handler(api),
    "kanban_blackboard": lambda api: _kanban_swarm.blackboard_handler(api),
    "kanban_get_orchestration": lambda api: _kanban_ops.get_orchestration_handler(api),
    "kanban_put_orchestration": lambda api: _kanban_ops.put_orchestration_handler(api),
    "kanban_profiles": lambda api: _kanban_ops.profiles_handler(api),
    # 사건
    "events": lambda api: _events.events_handler(api),
    "events_handoff": lambda api: _events.handoff_handler(api),
    # 크론
    "cron_list_jobs": lambda api: _cron.list_jobs_handler(api),
    "cron_create_job": lambda api: _cron.create_job_handler(api),
    "cron_get_job": lambda api: _cron.get_job_handler(api),
    "cron_update_job": lambda api: _cron.update_job_handler(api),
    "cron_delete_job": lambda api: _cron.delete_job_handler(api),
    "cron_list_runs": lambda api: _cron.list_runs_handler(api),
    "cron_pause": lambda api: _cron.pause_handler(api),
    "cron_resume": lambda api: _cron.resume_handler(api),
    "cron_run": lambda api: _cron.run_handler(api),
    "cron_delivery_targets": lambda api: _cron.delivery_targets_handler(api),
    "cron_blueprints": lambda api: _cron.blueprints_handler(api),
    "cron_instantiate_blueprint": lambda api: _cron.instantiate_blueprint_handler(api),
    # 아티팩트
    "artifacts_list": lambda api: _artifacts_routes.list_handler(api),
    "artifacts_get": lambda api: _artifacts_routes.get_handler(api),
    "artifacts_content": lambda api: _artifacts_routes.content_handler(api),
    "artifacts_add_version": lambda api: _artifacts_routes.add_version_handler(api),
    "artifacts_rework": lambda api: _artifacts_routes.rework_handler(api),
    "artifacts_delete": lambda api: _artifacts_routes.delete_handler(api),
    # 카드 제안
    "card_proposal_resolve": lambda api: _card_proposal_routes.resolve_handler(api),
    "card_proposal_unresolve": lambda api: _card_proposal_routes.unresolve_handler(api),
    "card_proposal_record_task": lambda api: _card_proposal_routes.record_task_handler(api),
}


def _assert_every_route_has_a_handler():
    # 테이블 행과 팩토리 매핑이 어긋나면 attach 가 첫 요청이 아니라 게이트웨이 기동에서 KeyError 로 죽는다.
    # 그래도 import 시점이 더 이르다 — 테스트 수집만으로 걸린다.
    missing = [name for _m, _p, name, _s in ROUTES if name not in _HANDLERS]
    if missing:
        raise AssertionError(f"ROUTES 에 있지만 _HANDLERS 에 없는 핸들러: {missing}")


_assert_every_route_has_a_handler()


def _make_task_action(api):
    """`POST /deskrpg/kanban/tasks/{id}/{action}` — 동작 이름으로 `kanban_actions.action_handler` 에 배분한다.

    10개 동작을 행 10개로 적는 대신 와일드카드 한 행으로 둔다. 모르는 이름은 404 — 오타 난 동작이
    405 나 다른 카드 라우트로 흘러가지 않게 여기서 끊는다. 핸들러들은 구성 시점에 한 번만 만든다.
    """
    from .common import json_error
    from .contract_fields import KANBAN_TASK_ACTIONS

    handlers = {name: _kanban_actions.action_handler(api, name) for name in KANBAN_TASK_ACTIONS}

    async def handler(request):
        action = request.match_info["action"]
        target = handlers.get(action)
        if target is None:
            return json_error(404, "unknown_action", action)
        return await target(request)

    return handler


def _system_timezone_name():
    """Hermes 설정에 타임존이 없을 때 쓸 서버 로컬 IANA 이름. 못 알아내면 None.

    Hermes 는 `timezone` 미설정을 None(= 서버 로컬 시각) 으로 돌려주는데, 그러면 info 의
    `timezone` 이 null 이 되고 DeskRPG 크론 화면은 "게이트웨이 시간대 미확인" 을 그린다.
    실제로는 서버가 어떤 지역 시각으로 도는지 알 수 있으므로 그 이름을 대신 보고한다.
    추정이지 설정이 아니므로, IANA 이름으로 확인되는 것만 낸다 — `KST` 같은 약어는 버린다.

    순서: TZ 환경변수 → /etc/localtime 심볼릭 링크. 둘 다 실패하면 None(옛 동작).
    """
    candidates = []
    env = os.environ.get("TZ")
    if env:
        candidates.append(env.lstrip(":"))

    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        link = ""
    if link:
        parts = link.replace("\\", "/").split("/zoneinfo/")
        if len(parts) > 1:
            candidates.append(parts[-1])

    for name in candidates:
        try:
            ZoneInfo(name)
        except Exception:
            continue
        return name
    return None


def _info_timezone(api):
    """`hermes_time.get_timezone()` 의 ZoneInfo 를 IANA 이름으로. 없으면 서버 로컬 이름.

    Hermes 는 타임존이 미설정이면 None(서버 로컬)을 돌려준다 — 그 경우 서버의 실제 로컬
    타임존 이름으로 폴백한다. 설정 파일이 깨져 예외가 나더라도 info 가 죽어선 안 된다 —
    DeskRPG 는 이 응답으로 자동화 기능을 켜고 끈다.
    """
    try:
        tz = api.get_timezone()
    except Exception:
        tz = None
    key = getattr(tz, "key", None)
    if isinstance(key, str) and key:
        return key
    return _system_timezone_name()


def _info_dispatcher_present(api) -> bool:
    """디스패처(게이트웨이의 kanban.dispatch_in_gateway)가 살아 있는지. 예외는 fail-open(true).

    Hermes 의 `_check_dispatcher_presence` 자체도 fail-open 이다 — 경고를 놓치는 쪽이
    멀쩡한 게이트웨이에 "디스패처 없음" 을 외치는 쪽보다 낫다.
    """
    try:
        present, _message = api._check_dispatcher_presence(api.get_hermes_home())
        return bool(present)
    except Exception:
        return True


_FALSY = {"", "0", "false", "no", "off"}


def _info_worker_plugin(api):
    """워커에서 이 플러그인이 안 뜨는 프로필. 판정이 실패해도 info 전체를 500 으로 만들지 않는다 —
    null 은 "모른다" 이고, 빈 `missing` 은 "전부 된다" 다. 둘을 섞지 않는다."""
    try:
        return _worker_plugin.report(api)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[deskrpg] 워커 플러그인 판정 실패: %s", type(exc).__name__)
        return None


def _info_dashboard_url(api):
    """Hermes 대시보드 공개 주소, 또는 None.

    주소는 Hermes 자신의 `resolve_public_url()`(`HERMES_DASHBOARD_PUBLIC_URL` → `dashboard.public_url`)
    로 구한다 — 규칙을 베끼면 Hermes 가 바꿀 때 어긋난다.

    컨테이너 이미지는 `HERMES_DASHBOARD` 로 대시보드 서비스를 켜고 끈다. 이 값이 **정의돼 있고 거짓**이면
    주소가 설정돼 있어도 대시보드는 떠 있지 않으므로 None 이다(DeskRPG compose 는 비밀번호가 없으면
    빈 값으로 둔다). 정의되지 않은 호스트 설치는 운영자가 공개 주소를 적어 둔 것을 의도로 본다.
    info 는 자동화 게이트가 읽으므로 여기서 무엇이 던져도 500 을 내지 않는다.
    """
    flag = os.environ.get("HERMES_DASHBOARD")
    if flag is not None and flag.strip().lower() in _FALSY:
        return None
    resolve = getattr(api, "resolve_public_url", None)
    if resolve is None:
        return None
    try:
        url = resolve()
    except Exception:
        return None
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else None


def _make_info(api):
    from aiohttp import web

    from .contract_fields import capabilities

    async def handler(request):
        return web.json_response(
            {
                "plugin": "deskrpg",
                "version": PLUGIN_VERSION,
                "routes": [f"{m} {p}" for m, p, _h, _s in routes_for(api)],
                "capabilities": list(capabilities(api)),
                "timezone": _info_timezone(api),
                "dashboard_url": _info_dashboard_url(api),
                "artifact_max_bytes": _artifact_upload_max_bytes(api),
                "worker_plugin": _info_worker_plugin(api),
                "kanban": {
                    "dispatcher_present": _info_dispatcher_present(api),
                    "attachments": True,
                    # 첨부는 요청 본문에 실려 오므로 api_server 의 본문 상한과 칸반 자체 상한
                    # 중 작은 쪽이 실효 상한이다. 클라이언트가 이 값으로 업로드 전에 거른다.
                    "attachment_max_bytes": min(
                        int(api.MAX_REQUEST_BYTES), int(api.KANBAN_ATTACHMENT_MAX_BYTES)
                    ),
                },
            }
        )

    return handler


def handler_for(name, api):
    return _HANDLERS[name](api)


# 이 Hermes 빌드에 심볼이 없으면 등록하지 않는 라우트. 등록해 놓고 500 을 내지 않는다 —
# 호출부가 "설치는 됐는데 고장" 과 "기능이 없음" 을 구분할 수 없게 된다.
#
# 값은 심볼 이름(문자열, `getattr(api, name) is not None` 으로 판정) 또는 판정 함수
# (`predicate(api) -> bool`)다. `get_toolsets`/`get_skills` 는 단일 심볼이 아니라
# `contract_fields.has_toolset_symbols`/`has_skill_symbols` 를 그대로 써야 한다 — 그래야
# 라우트 유무가 `capabilities()` 의 `profile_toolsets`/`profile_skills` 와 항상 같은
# 심볼 집합으로 판정된다(F3, 2026-09-19).
_OPTIONAL_ROUTES = {
    "kanban_create_swarm": "create_swarm",
    "kanban_blackboard": "latest_blackboard",
    "get_toolsets": _contract_fields.has_toolset_symbols,
    "get_skills": _contract_fields.has_skill_symbols,
    "get_tool_providers": _contract_fields.has_tool_provider_symbols,
    "put_tool_provider": _contract_fields.has_tool_provider_symbols,
    "put_provider_key": "PROVIDER_REGISTRY",
    "delete_provider_key": "PROVIDER_REGISTRY",
    "oauth_start": _contract_fields.has_oauth_symbols,
    "oauth_poll": _contract_fields.has_oauth_symbols,
    "oauth_cancel": _contract_fields.has_oauth_symbols,
    "oauth_disconnect": _contract_fields.has_oauth_symbols,
}


def _route_available(api, handler_name: str) -> bool:
    gate = _OPTIONAL_ROUTES.get(handler_name)
    if gate is None:
        return True
    if callable(gate):
        return bool(gate(api))
    return getattr(api, gate, None) is not None


def routes_for(api):
    """이 빌드에서 실제로 뜰 라우트만."""
    return [row for row in ROUTES if _route_available(api, row[2])]


def attach(app, adapter, api) -> None:
    """테이블을 돌며 전부 인증으로 감싸 등록한다.

    app.router.add_* 를 여기 말고 어디서도 부르지 않는다 — 그래야 감싸지 않은
    핸들러가 생길 수 없다.
    """
    before = len(getattr(app.router, "_resources", []))
    for method, path, handler_name, scope in routes_for(api):
        handler = require_auth(adapter, scope, handler_for(handler_name, api))
        app.router.add_route(method, path, handler)
        # 프로필 프리픽스 미러는 Hermes 가 자기 라우트에만 만들어 주므로,
        # /p/{profile}/ 경로는 우리가 테이블에 그대로 적어 등록한다.
    _raise_above_profile_catchall(app, before)


# Hermes 는 `/p/{profile}/{tail:.*}` 포괄 라우트를 **자기 라우트 전부를 등록한 뒤**
# 마지막에 건다(gateway/platforms/api_server.py — "Registered LAST so every native
# mirror above wins"). 플러그인 배선은 그보다 더 뒤에 일어나므로, 우리가 그냥
# add_route 하면 `/p/{profile}/deskrpg/...` 요청은 전부 그 포괄 라우트에 먼저
# 걸려 404("Unknown or unconfigured profile") 가 된다 — 스테이징에서 실측했다.
# aiohttp 는 등록 순서대로 매칭하므로, 우리 리소스를 포괄 라우트 앞으로 옮긴다.
_PROFILE_CATCHALL_SUFFIX = "/{tail}"


def _raise_above_profile_catchall(app, first_index: int) -> None:
    """방금 등록한 리소스를 `/p/{profile}/{tail:.*}` 앞으로 끌어올린다.

    aiohttp 3.14 의 `UrlDispatcher.resolve` 는 `_resources` 목록이 아니라
    **`_resource_index` 의 버킷**을 훑는다(경로를 뒤에서부터 잘라 가며 후보를 찾고,
    같은 버킷 안에서는 등록 순서를 지킨다). `/p/{profile}/...` 는 전부 `/p` 버킷에
    들어가므로 목록만 바꾸면 아무 효과가 없다 — 버킷도 같이 고쳐야 한다(실측).

    둘 다 공개 API 가 아니다. 모양이 다르면 조용히 아무것도 하지 않는다 —
    순서를 못 바꿔 404 가 나는 편이, 라우터를 깨뜨려 게이트웨이가 못 뜨는 것보다 낫다.
    """
    resources = getattr(app.router, "_resources", None)
    if not isinstance(resources, list) or first_index >= len(resources):
        return
    catchall = None
    for resource in resources[:first_index]:
        canonical = getattr(resource, "canonical", "")
        if canonical.startswith("/p/") and canonical.endswith(_PROFILE_CATCHALL_SUFFIX):
            catchall = resource
            break
    if catchall is None:
        return
    ours = resources[first_index:]

    def _reorder(bucket):
        mine = [r for r in bucket if r in ours]
        if not mine or catchall not in bucket:
            return bucket
        rest = [r for r in bucket if r not in ours]
        return rest[: rest.index(catchall)] + mine + rest[rest.index(catchall) :]

    del resources[first_index:]
    at = resources.index(catchall)
    resources[at:at] = ours

    index = getattr(app.router, "_resource_index", None)
    if isinstance(index, dict):
        for key, bucket in list(index.items()):
            if isinstance(bucket, list):
                index[key] = _reorder(bucket)

"""크론 — 프로필 스코프 잡 CRUD·실행 이력·배달처·템플릿 (`/p/{profile}/deskrpg/cron/...`, K1–K9).

모든 Hermes 크론 호출은 **그 프로필의 홈**을 가리키는 스코프 안에서 돈다(`cron_scope`):
`set_hermes_home_override(home)` + `use_cron_store(home)`. 대시보드의 `_cron_store_scope` 와
같은 방식이다 — 게이트웨이 프로세스 하나가 여러 프로필의 잡을 다루므로, 프로세스 전역을
바꾸지 않고 컨텍스트 변수로만 갈아 끼운다.

잡 레코드는 Hermes 가 주는 그대로 내보내고 `state` 만 `effective_job_state` 로 덮어쓴다(K2).
대시보드 헬퍼(`_normalize_dashboard_cron_updates`, `_notify_cron_provider_for_profile`)는
FastAPI 를 import 하므로 부르지 않고 **로직을 이식**했다 — 원본 위치를 각 함수 주석에 적어 둔다.

전부 블로킹(파일·sqlite)이라 핸들러는 `run_blocking` 으로 워커 스레드에 보낸다.
"""

import contextlib
import logging
import re
from pathlib import Path

from aiohttp import web

from . import cron_results
from .common import (
    RequestError,
    log_event,
    read_json_object,
    require_bool,
    require_str,
    require_str_list,
    run_blocking,
)

logger = logging.getLogger("deskrpg_plugin")

# K5 — PUT 이 받는 updates 키. 그 외는 400. 계약의 UPDATE_CRON_JOB_UPDATES_KEYS 에 skills·script 를
# 더한 것이 스펙 K5 의 목록이다.
UPDATE_ALLOWED_KEYS = frozenset(
    {"schedule", "prompt", "name", "deliver", "model", "provider", "enabled", "skills", "script"}
)

LOCAL_DELIVERY_TARGET = {"id": "local", "name": "Local", "home_target_set": True, "home_env_var": None}

DESKRPG_ORIGIN_SOURCE = "deskrpg"


# ---------------------------------------------------------------------------
# 프로필 홈 해석 · 저장소 스코프
# ---------------------------------------------------------------------------


def resolve_profile_home(api, raw_name) -> Path:
    """경로의 프로필 이름 → 프로필 홈. 이름 형식 오류 400, 없으면 404.

    대시보드 `_cron_profile_home` 과 같은 순서: normalize → validate → exists → dir.
    `get_profile_dir` 에는 검증을 통과한 이름만 넘긴다 — 경로 탈출은 여기서 끝난다.
    """
    name = api.normalize_profile_name(raw_name)
    try:
        api.validate_profile_name(name)
    except Exception as exc:
        raise RequestError(400, "invalid_profile", str(exc)) from exc
    if not api.profile_exists(name):
        raise RequestError(404, "profile_not_found", name)
    return Path(api.get_profile_dir(name))


@contextlib.contextmanager
def cron_scope(api, profile_name):
    """프로필 홈으로 HERMES_HOME 과 크론 저장소를 돌려놓는 블록. 홈 경로를 yield 한다.

    **워커 스레드 안에서만** 쓴다. 두 오버라이드는 컨텍스트 변수라 스레드/태스크에 국한되고,
    블록을 나가면 반드시 원상복구한다 — 한 프로필의 잡을 다른 프로필의 파일에 쓰는 사고를 막는다.
    """
    home = resolve_profile_home(api, profile_name)
    token = api.set_hermes_home_override(str(home))
    try:
        with api.use_cron_store(home):
            yield home
    finally:
        api.reset_hermes_home_override(token)


def with_state(api, job):
    """K2 — 잡 레코드 그대로에 `state = effective_job_state(job)` 를 덮어쓴다."""
    if job is None:
        return None
    return {**job, "state": api.effective_job_state(job)}


# ---------------------------------------------------------------------------
# 대시보드 정규화 로직 이식 (hermes_cli/web_server_cron.py · web_routers/cron.py)
# ---------------------------------------------------------------------------


def optional_text(value, *, strip_trailing_slash=False):
    """`_cron_optional_text` 이식: None/공백 → None, 아니면 strip 한 문자열."""
    if value is None:
        return None
    text = str(value).strip()
    if strip_trailing_slash:
        text = text.rstrip("/")
    return text or None


def string_list(value):
    """`_cron_string_list` 이식: 문자열(쉼표·줄바꿈 구분)·리스트 → 비어 있지 않은 항목 리스트, 없으면 None."""
    if isinstance(value, str):
        raw_items = re.split(r"[\n,]", value)
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return None
    items = [str(item).strip() for item in raw_items if str(item).strip()]
    return items or None


def normalize_script(value, profile_home) -> "str | None":
    """`_normalize_dashboard_cron_script` 이식: 스크립트는 `<home>/scripts` 샌드박스 안의 실제 파일이어야 한다.

    상대 경로는 샌드박스 기준, 절대 경로는 resolve 한 뒤 샌드박스 안인지 본다. 저장은 샌드박스
    기준 상대 경로로 한다. 밖을 가리키거나 없거나 파일이 아니면 400 `invalid_script`.
    """
    text = optional_text(value)
    if not text:
        return None
    scripts_root = (Path(profile_home) / "scripts").resolve()
    raw_path = Path(text).expanduser()
    candidate = raw_path.resolve() if raw_path.is_absolute() else (scripts_root / raw_path).resolve()
    try:
        relative = candidate.relative_to(scripts_root)
    except ValueError as exc:
        raise RequestError(400, "invalid_script", f"script must be inside {scripts_root}") from exc
    if not candidate.exists():
        raise RequestError(400, "invalid_script", f"script does not exist: {candidate}")
    if not candidate.is_file():
        raise RequestError(400, "invalid_script", f"script is not a file: {candidate}")
    return str(relative)


def normalize_updates(updates: dict, profile_home) -> dict:
    """K5 — `_normalize_dashboard_cron_updates` 이식 + 허용 키 화이트리스트 + 타입 검사.

    model/provider: 선택 문자열(null 로 지움) · script: 샌드박스 · deliver: 빈값→"local" ·
    skills: 리스트 · enabled: bool · schedule/prompt/name: 비어 있지 않은 문자열.
    """
    if not isinstance(updates, dict):
        raise RequestError(400, "invalid_field", "updates 는 JSON 객체여야 한다")
    unknown = sorted(set(updates) - UPDATE_ALLOWED_KEYS)
    if unknown:
        raise RequestError(400, "invalid_field", f"허용되지 않는 updates 키: {', '.join(unknown)}")
    if not updates:
        raise RequestError(400, "invalid_field", "updates 가 비어 있다")

    normalized = {}
    for key in ("schedule", "prompt", "name"):
        if key in updates:
            normalized[key] = require_str(updates, key)
    for key in ("model", "provider"):
        if key in updates:
            value = updates[key]
            if value is not None and not isinstance(value, str):
                raise RequestError(400, "invalid_field", f"{key} 는 문자열 또는 null 이어야 한다")
            normalized[key] = optional_text(value)
    if "script" in updates:
        value = updates["script"]
        if value is not None and not isinstance(value, str):
            raise RequestError(400, "invalid_field", "script 는 문자열 또는 null 이어야 한다")
        normalized["script"] = normalize_script(value, profile_home)
    if "deliver" in updates:
        value = updates["deliver"]
        if value is not None and not isinstance(value, str):
            raise RequestError(400, "invalid_field", "deliver 는 문자열이어야 한다")
        normalized["deliver"] = optional_text(value) or "local"
    if "skills" in updates:
        value = updates["skills"]
        if value is not None and not isinstance(value, (str, list)):
            raise RequestError(400, "invalid_field", "skills 는 문자열 배열이어야 한다")
        if isinstance(value, list) and not all(isinstance(v, str) for v in value):
            raise RequestError(400, "invalid_field", "skills 는 문자열 배열이어야 한다")
        normalized["skills"] = string_list(value)
    if "enabled" in updates:
        normalized["enabled"] = require_bool(updates, "enabled")
    return normalized


def reconcile_provider(api, profile_name):
    """K7 — `_notify_cron_provider_for_profile` 이식. **최선 노력**: 어떤 예외도 요청을 실패시키지 않는다.

    크론 저장소 스코프 **안에서** 부른다(프로바이더가 스코프의 jobs.json 을 읽는다).
    외부 프로바이더는 원격 레지스트리를 **프로필 구분 없이** 이 프로필의 잡으로 수렴시켜 다른
    프로필의 일회성 잡을 해제해 버리므로, 프로필이 둘 이상이면 건너뛴다(변경된 프로필은 다음
    fire/start 에서 멱등하게 다시 등록된다). 내장 프로바이더는 틱마다 jobs.json 을 다시 읽어 no-op 이다.
    """
    try:
        provider = api.resolve_cron_scheduler()
        external = not isinstance(provider, api.InProcessCronScheduler)
        if external and _profile_count(api) > 1:
            logger.warning(
                "[deskrpg] 크론 프로바이더 재조정 건너뜀 profile=%s: 외부 프로바이더 '%s' 는 프로필 구분이 없어 "
                "다른 프로필의 일회성 잡을 해제할 수 있다",
                profile_name,
                getattr(provider, "name", type(provider).__name__),
            )
            return
        provider.on_jobs_changed()
    except Exception:
        logger.debug("[deskrpg] 크론 프로바이더 재조정 실패 profile=%s", profile_name, exc_info=True)


def _profile_count(api) -> int:
    return sum(1 for info in api.list_profiles() if getattr(info, "name", str(info)))


# ---------------------------------------------------------------------------
# 생성 경로 (K4 · K9 공용)
# ---------------------------------------------------------------------------


def create_job_in_scope(api, profile_name, spec: dict):
    """스코프 안에서 잡을 만들고 프로바이더를 재조정한다. `{job}` 용 레코드를 돌려준다.

    `CronSchedulerRegistrationError` → 424(잡은 저장됐지만 외부 스케줄러 등록 실패 — 본문에 잡을
    실어 클라이언트가 재시도 대신 상태를 보여 주게 한다). 스케줄 문법 등 Hermes 의 ValueError → 400
    `invalid_schedule`(prompt/script 유무는 우리가 먼저 검사하므로 남는 ValueError 는 스케줄이 대부분이다).
    """
    with cron_scope(api, profile_name):
        try:
            job = api.create_job_with_scheduler_registration(**spec)
        except api.CronSchedulerRegistrationError as exc:
            raise _RegistrationFailed(exc) from exc
        except ValueError as exc:
            raise RequestError(400, "invalid_schedule", str(exc)) from exc
        reconcile_provider(api, profile_name)
        return with_state(api, job)


class _RegistrationFailed(Exception):
    """424 응답에 저장된 잡을 실어 보내기 위한 운반체."""

    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause

    def response(self, api):
        job = getattr(self.cause, "job", None)
        body = {"error": "scheduler_registration_failed", "detail": str(self.cause)}
        if isinstance(job, dict):
            body["job"] = with_state(api, job)
        return web.json_response(body, status=424)


def parse_create_body(body: dict, profile_home) -> dict:
    """K4 — POST jobs 본문 → `create_job` kwargs. schedule 필수, prompt 는 script 없을 때 필수."""
    schedule = require_str(body, "schedule")
    script = normalize_script(require_str(body, "script", required=False, default=None), profile_home)
    prompt = require_str(body, "prompt", required=not script, default=None)
    spec = {
        "prompt": prompt or "",
        "schedule": schedule,
        "name": require_str(body, "name", required=False, default=None),
        "deliver": optional_text(require_str(body, "deliver", required=False, default=None, allow_empty=True)) or "local",
        "model": optional_text(require_str(body, "model", required=False, default=None, allow_empty=True)),
        "provider": optional_text(require_str(body, "provider", required=False, default=None, allow_empty=True)),
        "skills": string_list(require_str_list(body, "skills", required=False, default=None)),
        "script": script,
        "paused": require_bool(body, "paused", required=False, default=False),
        "origin": {"source": DESKRPG_ORIGIN_SOURCE},
    }
    repeat = body.get("repeat")
    if repeat is not None:
        # Hermes 의 normalize_repeat_value 가 int·'forever'·'once'·숫자 문자열을 받는다. bool 은 int 의
        # 하위 타입이라 따로 거른다.
        if isinstance(repeat, bool) or not isinstance(repeat, (int, str)):
            raise RequestError(400, "invalid_field", "repeat 는 정수 또는 문자열이어야 한다")
        spec["repeat"] = repeat
    return spec


# ---------------------------------------------------------------------------
# 핸들러 공통
# ---------------------------------------------------------------------------


def _profile_of(request) -> str:
    return request.match_info["profile"]


def _job_id_of(request) -> str:
    job_id = request.match_info.get("id", "")
    if not job_id or "/" in job_id or job_id in (".", ".."):
        raise RequestError(400, "invalid_job_id", job_id)
    return job_id


def _query_flag(request, key) -> bool:
    return request.query.get(key, "").strip().lower() in ("1", "true", "yes", "on")


async def _guarded(api, fn):
    """워커 스레드에서 fn 을 돌리고 RequestError·등록 실패를 JSON 응답으로 바꾼다."""
    try:
        return await run_blocking(fn)
    except RequestError as exc:
        return exc.response()
    except _RegistrationFailed as exc:
        return exc.response(api)


# ---------------------------------------------------------------------------
# 핸들러 팩토리 — routes.py 가 `handler(api)` 로 만든다
# ---------------------------------------------------------------------------


def list_jobs_handler(api):
    """K2 — GET jobs?include_disabled=true → {jobs:[CronJob]}"""

    async def handler(request):
        profile = _profile_of(request)
        include_disabled = _query_flag(request, "include_disabled")

        def work():
            with cron_scope(api, profile):
                return [with_state(api, job) for job in api.list_jobs(include_disabled=include_disabled)]

        result = await _guarded(api, work)
        if isinstance(result, web.Response):
            return result
        return web.json_response({"jobs": result})

    return handler


def get_job_handler(api):
    """K2 — GET jobs/{id} → {job} (없으면 404)"""

    async def handler(request):
        profile = _profile_of(request)

        def work():
            job_id = _job_id_of(request)
            with cron_scope(api, profile):
                job = api.get_job(job_id)
                if job is None:
                    raise RequestError(404, "job_not_found", job_id)
                return with_state(api, job)

        result = await _guarded(api, work)
        if isinstance(result, web.Response):
            return result
        return web.json_response({"job": result})

    return handler


def list_runs_handler(api):
    """K3 — GET jobs/{id}/runs?limit=20 → {runs:[...], limit}"""

    async def handler(request):
        profile = _profile_of(request)
        limit = cron_results.clamp_runs_limit(request.query.get("limit"))

        def work():
            job_id = _job_id_of(request)
            with cron_scope(api, profile) as home:
                job = api.get_job(job_id)
                if job is None:
                    raise RequestError(404, "job_not_found", job_id)
                # job_id 가 이름일 수도 있으니 세션 id 접두사에 쓰이는 정본 id 로 바꾼다.
                return cron_results.cron_runs_for_job(api, home, str(job.get("id") or job_id), limit)

        result = await _guarded(api, work)
        if isinstance(result, web.Response):
            return result
        return web.json_response({"runs": result, "limit": limit})

    return handler


def create_job_handler(api):
    """K4 — POST jobs → 201 {job}"""

    async def handler(request):
        profile = _profile_of(request)
        try:
            body = await read_json_object(request)
        except RequestError as exc:
            return exc.response()

        def work():
            home = resolve_profile_home(api, profile)
            spec = parse_create_body(body, home)
            job = create_job_in_scope(api, profile, spec)
            log_event("cron.job.created", profile=profile, job_id=str(job.get("id")))
            return job

        result = await _guarded(api, work)
        if isinstance(result, web.Response):
            return result
        return web.json_response({"job": result}, status=201)

    return handler


def update_job_handler(api):
    """K5 — PUT jobs/{id} {updates:{...}} → {job}"""

    async def handler(request):
        profile = _profile_of(request)
        try:
            body = await read_json_object(request)
        except RequestError as exc:
            return exc.response()

        def work():
            job_id = _job_id_of(request)
            if "updates" not in body:
                raise RequestError(400, "missing_field", "updates")
            with cron_scope(api, profile) as home:
                updates = normalize_updates(body["updates"], home)
                try:
                    job = api.update_job(job_id, updates)
                except ValueError as exc:
                    raise RequestError(400, "invalid_update", str(exc)) from exc
                if job is None:
                    raise RequestError(404, "job_not_found", job_id)
                reconcile_provider(api, profile)
                log_event("cron.job.updated", profile=profile, job_id=job_id, keys=sorted(updates))
                return with_state(api, job)

        result = await _guarded(api, work)
        if isinstance(result, web.Response):
            return result
        return web.json_response({"job": result})

    return handler


def _toggle_handler(api, action):
    """K6 — pause/resume 공통. ValueError(지난 일회성) → 409 job_terminal."""
    hermes_fn = {"pause": api.pause_job, "resume": api.resume_job}[action]

    async def handler(request):
        profile = _profile_of(request)

        def work():
            job_id = _job_id_of(request)
            with cron_scope(api, profile):
                try:
                    job = hermes_fn(job_id)
                except ValueError as exc:
                    raise RequestError(409, "job_terminal", str(exc)) from exc
                if job is None:
                    raise RequestError(404, "job_not_found", job_id)
                reconcile_provider(api, profile)
                log_event(f"cron.job.{action}", profile=profile, job_id=job_id)
                return with_state(api, job)

        result = await _guarded(api, work)
        if isinstance(result, web.Response):
            return result
        return web.json_response({"job": result})

    return handler


def pause_handler(api):
    return _toggle_handler(api, "pause")


def resume_handler(api):
    return _toggle_handler(api, "resume")


def run_handler(api):
    """K6 — POST jobs/{id}/run → 202 {accepted:true, job}. 실행은 스케줄러 틱이 한다.

    `trigger_job` 은 일시정지된 잡을 **재개까지** 해 버리므로 먼저 막는다(409 job_paused) —
    "한 번만 돌려 보기" 가 영구 재개로 둔갑하면 안 된다.
    """

    async def handler(request):
        profile = _profile_of(request)

        def work():
            job_id = _job_id_of(request)
            with cron_scope(api, profile):
                job = api.get_job(job_id)
                if job is None:
                    raise RequestError(404, "job_not_found", job_id)
                if api.effective_job_state(job) == "paused":
                    raise RequestError(409, "job_paused", job_id)
                try:
                    triggered = api.trigger_job(job_id)
                except ValueError as exc:
                    raise RequestError(409, "job_terminal", str(exc)) from exc
                if triggered is None:
                    raise RequestError(404, "job_not_found", job_id)
                reconcile_provider(api, profile)
                log_event("cron.job.run", profile=profile, job_id=job_id)
                return with_state(api, triggered)

        result = await _guarded(api, work)
        if isinstance(result, web.Response):
            return result
        return web.json_response({"accepted": True, "job": result}, status=202)

    return handler


def delete_job_handler(api):
    """K7 — DELETE jobs/{id} → {ok:true} (없으면 404)"""

    async def handler(request):
        profile = _profile_of(request)

        def work():
            job_id = _job_id_of(request)
            with cron_scope(api, profile):
                try:
                    removed = api.remove_job(job_id)
                except ValueError as exc:
                    raise RequestError(400, "invalid_job_id", str(exc)) from exc
                if not removed:
                    raise RequestError(404, "job_not_found", job_id)
                reconcile_provider(api, profile)
                log_event("cron.job.deleted", profile=profile, job_id=job_id)
                return True

        result = await _guarded(api, work)
        if isinstance(result, web.Response):
            return result
        return web.json_response({"ok": True})

    return handler


def delivery_targets_in_scope(api) -> list:
    """K8 — local 을 앞에 붙인 배달처 목록. 스코프 안에서 부른다(게이트웨이 설정이 홈을 본다)."""
    return [dict(LOCAL_DELIVERY_TARGET), *api.cron_delivery_targets()]


def delivery_targets_handler(api):
    """K8 — GET delivery-targets → {targets:[...]}"""

    async def handler(request):
        profile = _profile_of(request)

        def work():
            with cron_scope(api, profile):
                return delivery_targets_in_scope(api)

        result = await _guarded(api, work)
        if isinstance(result, web.Response):
            return result
        return web.json_response({"targets": result})

    return handler


def blueprint_entries_in_scope(api) -> list:
    """K9 — 카탈로그 항목들. `deliver` 필드의 options 는 K8 목록의 id 로 교체한다."""
    deliver_options = [t["id"] for t in delivery_targets_in_scope(api) if t.get("id")]
    entries = []
    for blueprint in api.CATALOG:
        entry = dict(api.blueprint_catalog_entry(blueprint))
        fields = []
        for field in entry.get("fields", []):
            field = dict(field)
            if field.get("name") == "deliver":
                field["options"] = list(deliver_options)
            fields.append(field)
        entry["fields"] = fields
        entries.append(entry)
    return entries


def blueprints_handler(api):
    """K9 — GET blueprints → {blueprints:[AutomationBlueprint]}"""

    async def handler(request):
        profile = _profile_of(request)

        def work():
            with cron_scope(api, profile):
                return blueprint_entries_in_scope(api)

        result = await _guarded(api, work)
        if isinstance(result, web.Response):
            return result
        return web.json_response({"blueprints": result})

    return handler


def instantiate_blueprint_handler(api):
    """K9 — POST blueprints/instantiate {blueprint, values} → 201 {job}

    모르는 키 404, 값 오류(`BlueprintFillError`) 422 `invalid_blueprint_values`. `fill_blueprint` 가
    만든 spec 의 origin 을 `{"source":"deskrpg","blueprint":key}` 로 바꿔 K4 와 같은 생성 경로를 탄다.
    """

    async def handler(request):
        profile = _profile_of(request)
        try:
            body = await read_json_object(request)
        except RequestError as exc:
            return exc.response()

        def work():
            key = require_str(body, "blueprint")
            values = body.get("values", {})
            if values is None:
                values = {}
            if not isinstance(values, dict):
                raise RequestError(400, "invalid_field", "values 는 JSON 객체여야 한다")
            resolve_profile_home(api, profile)
            blueprint = api.get_blueprint(key)
            if blueprint is None:
                raise RequestError(404, "blueprint_not_found", key)
            origin = {"source": DESKRPG_ORIGIN_SOURCE, "blueprint": key}
            try:
                spec = dict(api.fill_blueprint(blueprint, values, origin=origin))
            except api.BlueprintFillError as exc:
                raise RequestError(422, "invalid_blueprint_values", str(exc)) from exc
            spec["origin"] = origin
            job = create_job_in_scope(api, profile, spec)
            log_event("cron.blueprint.instantiated", profile=profile, job_id=str(job.get("id")), blueprint=key)
            return job

        result = await _guarded(api, work)
        if isinstance(result, web.Response):
            return result
        return web.json_response({"job": result}, status=201)

    return handler

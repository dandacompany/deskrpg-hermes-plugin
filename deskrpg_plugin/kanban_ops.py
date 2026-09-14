"""디스패치·운영 설정·프로필 — 보드 하나가 아니라 **호스트**를 건드리는 라우트.

- `POST /deskrpg/kanban/dispatch?board=&max=8` → `{spawned, skipped_locked, warning?}`
- `GET|PUT /deskrpg/kanban/orchestration` → OrchestrationSettings(+ PUT 은 `restart_required`)
- `GET /deskrpg/kanban/profiles` → `{profiles:[{name, is_default, description}]}`

운영 설정은 Hermes `config.yaml` 의 `kanban.*` 절이다. `load_config` → 절 갱신 → `save_config(cfg)`
로 돌아가며, 다른 절은 건드리지 않는다. `max_in_progress*` 는 디스패처가 기동할 때 한 번만 읽으므로
바뀌면 `restart_required:true` 로 알린다 — 저장은 됐지만 지금 도는 디스패처는 모른다는 뜻이다.
"""

from aiohttp import web

from .common import (
    RequestError,
    guarded,
    log_event,
    parse_board_slug,
    read_json_object,
    require_bool,
    require_str,
    run_blocking,
)
from .contract_fields import UPDATE_ORCHESTRATION_KEYS
from .kanban_common import open_board

DEFAULT_DISPATCH_MAX = 8
_PROFILE_KEYS = ("orchestrator_profile", "default_assignee")
_LIMIT_KEYS = ("max_in_progress", "max_in_progress_per_profile")


# ---------------------------------------------------------------------------
# 설정 읽기
# ---------------------------------------------------------------------------


def _kanban_section(api) -> dict:
    try:
        cfg = api.load_config() or {}
    except Exception:
        return {}
    section = cfg.get("kanban") if isinstance(cfg, dict) else None
    return section if isinstance(section, dict) else {}


def _dispatch_in_gateway(api) -> bool:
    # Hermes `_check_dispatcher_presence` 와 같은 기본값(true).
    return bool(_kanban_section(api).get("dispatch_in_gateway", True))


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _parse_max(request) -> int:
    raw = request.query.get("max")
    if raw is None or raw == "":
        return DEFAULT_DISPATCH_MAX
    try:
        value = int(raw)
    except ValueError:
        raise RequestError(400, "invalid_max", raw)
    if value < 1:
        raise RequestError(400, "invalid_max", "max 는 1 이상이어야 한다")
    return value


def _spawned_entry(item) -> dict:
    """DispatchResult.spawned 의 `(task_id, assignee, workspace_path)` → 계약 `{task_id, profile}`."""
    if isinstance(item, dict):
        return {"task_id": item.get("task_id"), "profile": item.get("profile") or item.get("assignee")}
    if isinstance(item, (list, tuple)):
        return {"task_id": item[0], "profile": item[1] if len(item) > 1 else None}
    return {"task_id": str(item), "profile": None}


def dispatch_handler(api):
    """수동 틱 — `kanban.*` 운영 설정(max_in_progress* 등)을 거치지 않고 `dispatch_once` 를 바로 부른다.
    대시보드의 수동 디스패치와 같다; 설정을 적용하는 것은 게이트웨이의 주기 디스패처다."""

    @guarded
    async def handler(request):
        def work(slug, max_spawn):
            with open_board(api, slug) as conn:
                return api.dispatch_once(conn, board=slug, max_spawn=max_spawn)

        slug = parse_board_slug(request)
        max_spawn = _parse_max(request)
        result = await run_blocking(work, slug, max_spawn)
        spawned = [_spawned_entry(item) for item in (getattr(result, "spawned", None) or [])]
        body = {"spawned": spawned, "skipped_locked": bool(getattr(result, "skipped_locked", False))}
        if not _dispatch_in_gateway(api):
            # 실행은 했다 — 다만 게이트웨이 안의 주기 디스패처가 꺼져 있어 이 호출이 유일한 틱이다.
            body["warning"] = "embedded_dispatcher_disabled"
        log_event("kanban.dispatch", board=slug, spawned=spawned, skipped_locked=body["skipped_locked"])
        return web.json_response(body)

    return handler


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def orchestration_payload(api) -> dict:
    """대시보드 `get_orchestration_settings` 와 같은 해소 규칙 — 비었거나 없는 프로필은 활성 기본 프로필로."""
    section = _kanban_section(api)
    explicit = {key: str(section.get(key) or "").strip() for key in _PROFILE_KEYS}
    try:
        active_default = api.get_active_profile_name() or "default"
    except Exception:
        active_default = "default"
    resolved = {}
    for key, value in explicit.items():
        try:
            exists = bool(value) and api.profile_exists(value)
        except Exception:
            exists = bool(value)
        resolved[key] = value if exists else active_default
    body = {
        "orchestrator_profile": explicit["orchestrator_profile"],
        "default_assignee": explicit["default_assignee"],
        "auto_decompose": bool(section.get("auto_decompose", True)),
        "auto_promote_children": bool(section.get("auto_promote_children", True)),
        "resolved_orchestrator_profile": resolved["orchestrator_profile"],
        "resolved_default_assignee": resolved["default_assignee"],
        "dispatch_in_gateway": bool(section.get("dispatch_in_gateway", True)),
    }
    for key in _LIMIT_KEYS:
        value = section.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            body[key] = value
    return body


def get_orchestration_handler(api):
    @guarded
    async def handler(request):
        return web.json_response(await run_blocking(orchestration_payload, api))

    return handler


def _validated_profile(api, key: str, body: dict) -> str:
    """빈 문자열은 비움. 비어 있지 않으면 존재해야 한다(400)."""
    value = require_str(body, key, required=False, default="", allow_empty=True).strip()
    if value and not api.profile_exists(value):
        raise RequestError(400, "profile_not_found", value)
    return value


def _validated_limit(key: str, body: dict):
    """정수(1 이상)면 그 값, 명시적 null 이면 `None`(=지움)."""
    if body.get(key) is None:
        return None
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RequestError(400, "invalid_field", f"{key} 는 1 이상의 정수여야 한다")
    return value


def _apply_orchestration(api, body: dict) -> dict:
    """설정을 읽고 → 절을 갱신하고 → 저장한다. 워커 스레드 안에서 돈다."""
    unknown = set(body) - UPDATE_ORCHESTRATION_KEYS
    if unknown:
        raise RequestError(400, "unknown_field", ", ".join(sorted(unknown)))

    # 검증을 전부 끝낸 뒤에 쓴다 — 절반만 반영된 설정을 남기지 않는다.
    updates = {}
    for key in _PROFILE_KEYS:
        if key in body:
            updates[key] = _validated_profile(api, key, body)
    if "auto_decompose" in body:
        updates["auto_decompose"] = require_bool(body, "auto_decompose")
    for key in _LIMIT_KEYS:
        if key in body:
            updates[key] = _validated_limit(key, body)

    cfg = api.load_config() or {}
    section = cfg.get("kanban")
    if not isinstance(section, dict):
        section = cfg["kanban"] = {}
    restart_required = False
    for key, value in updates.items():
        if key in _LIMIT_KEYS:
            if section.get(key) != value:
                restart_required = True
            if value is None:
                section.pop(key, None)
                continue
        section[key] = value
    api.save_config(cfg)
    log_event("kanban.orchestration.put", fields=list(updates), restart_required=restart_required)
    return {**orchestration_payload(api), "restart_required": restart_required}


def put_orchestration_handler(api):
    @guarded
    async def handler(request):
        body = await read_json_object(request)
        payload = await run_blocking(_apply_orchestration, api, body)
        return web.json_response(payload)

    return handler


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------


def _profile_summaries(api) -> list:
    out = []
    for info in api.list_profiles():
        name = getattr(info, "name", str(info))
        try:
            meta = api.read_profile_meta(api.get_profile_dir(name)) or {}
        except Exception:
            meta = {}
        out.append({
            "name": name,
            "is_default": bool(getattr(info, "is_default", name == "default")),
            "description": str(meta.get("description") or ""),
        })
    return out


def profiles_handler(api):
    @guarded
    async def handler(request):
        return web.json_response({"profiles": await run_blocking(_profile_summaries, api)})

    return handler

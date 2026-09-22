"""DeskRPG 계약(`src/lib/hermes/deskrpg-plugin-types.ts`, 0.6.0)의 키 집합.

TS 타입을 파이썬 쪽에 그대로 베낀 것이다 — 핸들러가 응답을 만들 때 이 집합으로
모양을 맞추고, 테스트가 응답 키가 계약 밖으로 새지 않는지 단정한다. **판단은 없다.**
필드 의미·상태 전이 규칙은 계약 파일과 핸들러 몫이다.

`*_REQUIRED` 는 TS 에서 `?` 가 없는 키, `*_OPTIONAL` 은 `?` 가 붙은 키다.
`*_KEYS` 는 둘의 합집합이다. 계약 파일을 고치면 여기도 같이 고친다.
"""

# ---------------------------------------------------------------------------
# 공통 — /deskrpg/info
# ---------------------------------------------------------------------------

PLUGIN_INFO_REQUIRED = frozenset({
    "plugin", "version", "capabilities", "timezone", "kanban", "dashboard_url", "artifact_max_bytes",
})
# `routes` 는 0.1.0 부터 내던 필드라 유지한다. 계약 타입에는 없지만 해가 없다.
# `worker_plugin` 은 0.11.2 에서 더했다 — 옛 플러그인에는 없으므로 계약상 선택 키다.
PLUGIN_INFO_KEYS = PLUGIN_INFO_REQUIRED | frozenset({"routes", "worker_plugin"})
PLUGIN_INFO_KANBAN_KEYS = frozenset({"dispatcher_present", "attachments", "attachment_max_bytes"})
# 항상 있는 것. 스웜처럼 Hermes 빌드에 따라 갈리는 것은 `capabilities()` 가 붙인다.
# `kanban_views` = 묶음 조회(`GET /kanban/links`, `GET /kanban/runs`). Hermes 의 선택 심볼을
# 쓰지 않고 보드 DB 만 읽으므로 칸반이 되면 늘 된다 — 그래도 **capability 로 내보낸다.**
# 호출부가 버전으로 판단하면 "새 플러그인인데 404" 를 진단할 수 없다.
# `card_proposals` 는 사건 옵트인 토큰(`include=card_proposals`)과 같은 이름이다 — DeskRPG 는 이 값으로
# "이 게이트웨이가 카드 제안을 아는가" 를 판정해 칸반·크론과 같은 방식의 안내를 띄운다. 경로 문자열을
# 뒤져 판정하게 두면 경로를 고치는 날 조용히 깨진다.
# `worker_plugin` = `/deskrpg/info` 의 `worker_plugin` 보고와 `POST /deskrpg/worker-plugin`. 프로필 목록·경로 심볼만
# 쓰므로 늘 된다.
# `kanban_attachment_list` = `GET /deskrpg/kanban/attachments?board=` (보드 전체 첨부, 결과물 갤러리용).
CAPABILITIES = (
    "kanban", "cron", "events", "event_cursor_handoff", "artifacts", "kanban_views", "card_proposals", "worker_plugin",
    "kanban_attachment_list",
)


_TOOLSET_SYMBOLS = (
    "_get_effective_configurable_toolsets", "_get_platform_tools",
    "_toolset_has_keys", "_toolset_allowed_for_platform",
    # 쓰기(config PUT)가 `hermes tools` 저장과 같은 규칙을 따르는 데 필요하다 — config._apply_enabled_toolsets.
    "_configurable_keys", "_platform_default_keys", "_get_plugin_toolset_keys", "parse_config_string_list",
)
_SKILL_SYMBOLS = ("_find_all_skills", "_sort_skills")
_OAUTH_SYMBOLS = (
    "_DEVICE_CODE_STARTERS", "_start_device_code_flow", "poll_oauth_session",
    "_oauth_sessions", "_oauth_sessions_lock", "_oauth_profile_name", "clear_provider_auth",
)


def _has(api, names) -> bool:
    return all(getattr(api, n, None) is not None for n in names)


def has_toolset_symbols(api) -> bool:
    """`profile_toolsets` capability 와 config PUT `enabledToolsets` 가 같은 판정을 쓴다."""
    return _has(api, _TOOLSET_SYMBOLS)


def has_skill_symbols(api) -> bool:
    """`profile_skills` capability 와 config PUT `disabledSkills` 가 같은 판정을 쓴다."""
    return _has(api, _SKILL_SYMBOLS)


_TOOL_PROVIDER_SYMBOLS = (
    "TOOL_CATEGORIES", "_visible_providers", "provider_readiness_status",
    "_is_provider_active", "apply_provider_selection",
)


def has_tool_provider_symbols(api) -> bool:
    """`profile_tool_providers` capability 와 도구 프로바이더 라우트 두 개가 같은 판정을 쓴다.

    목록 판정(`_get_effective_configurable_toolsets`)과 홈 갈아 끼우기도 필요하므로 툴셋 심볼까지 본다.
    """
    return has_toolset_symbols(api) and _has(api, _TOOL_PROVIDER_SYMBOLS)


def has_initial_status(api) -> bool:
    """이 Hermes 빌드의 `create_task` 가 `initial_status` 를 받는가.

    버전으로 판단하지 않는다 — 이 모듈의 원칙대로 **되는 것만** 광고한다. 받지 못하는
    빌드에서 이 필드를 받아 넘기면 TypeError 가 400 invalid_task 로 뭉뚱그려진다.
    """
    import inspect

    create = getattr(api, "create_task", None)
    if create is None:
        return False
    try:
        return "initial_status" in inspect.signature(create).parameters
    except (TypeError, ValueError):
        return False


def has_oauth_symbols(api) -> bool:
    """`profile_oauth` capability 와 OAuth 라우트 네 개가 같은 판정을 쓴다."""
    return _has(api, _OAUTH_SYMBOLS)


# 앱 안 디바이스 로그인을 허용하는 프로바이더. Hermes 의 `_DEVICE_CODE_STARTERS` 에 있어도 여기 없으면
# 카탈로그는 CLI 안내(external)를 주고 OAuth 라우트는 400 `oauth_flow_unsupported` 로 거절한다.
# - xAI·MiniMax 폴러는 저장 직전 `_profile_scope(_oauth_session_profile(session_id))` 로 프로필을 다시 풀고
#   `cancelled` 를 보지 않는다(hermes_cli/web_server_oauth.py:397, :424). 취소(또는 `_gc_oauth_sessions`)로
#   세션이 사라지면 프로필이 None 이 되어 토큰이 **default 프로필**에 저장된다.
# - Nous 는 저장 전에 `cancelled` 를 보지만 15초 네트워크 갱신 동안 프로세스 전역 `_profile_scope` 를 쥔다(:343).
# Codex 워커는 세션 프로필을 시작 때 잡아 두고 저장 직전 락 안에서 `cancelled` 를 본다(web_routers/oauth.py:279-292).
IN_APP_DEVICE_LOGIN = frozenset({"openai-codex"})


def in_app_device_login(api, provider_id: str) -> bool:
    """카탈로그의 로그인 버튼과 OAuth 라우트가 같은 판정을 쓴다."""
    starters = getattr(api, "_DEVICE_CODE_STARTERS", None) or {}
    return provider_id in IN_APP_DEVICE_LOGIN and provider_id in starters and has_oauth_symbols(api)


def capabilities(api) -> tuple[str, ...]:
    """이 Hermes 빌드에서 **실제로 되는** 것만 돌려준다.

    버전만 보고 판단하면 "새 플러그인인데 404" 라는 진단 불가능한 상태가 된다.
    capability 문자열이 가용성을 말하게 한다.
    """
    extra = []
    if getattr(api, "create_swarm", None) is not None:
        extra.append("swarm")
    if has_toolset_symbols(api):
        extra.append("profile_toolsets")
    if has_skill_symbols(api):
        extra.append("profile_skills")
    if _has(api, ("PROVIDER_REGISTRY",)):
        extra.append("profile_clone")
        extra.append("profile_provider_keys")
    if has_oauth_symbols(api):
        extra.append("profile_oauth")
    if has_tool_provider_symbols(api):
        extra.append("profile_tool_providers")
    if has_initial_status(api):
        # 실행 전 승인 관문이 카드를 `blocked` 로 세울 수 있는가. 화면은 이 값이 없으면
        # "플러그인 업데이트 필요" 로 안내한다 — 조용히 승인 없이 실행되지 않게.
        extra.append("initial_status")
    return CAPABILITIES + tuple(extra)

# ---------------------------------------------------------------------------
# A.1 칸반 — 상태·열
# ---------------------------------------------------------------------------

CARD_STATUSES = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived")
# 보드 화면의 열 — archived 는 `include_archived=true` 일 때만 열로 나온다.
BOARD_COLUMNS = tuple(s for s in CARD_STATUSES if s != "archived")

WORKSPACE_KINDS = ("scratch", "worktree", "dir")

KANBAN_TASK_ACTIONS = (
    "reassign", "reclaim", "specify", "decompose", "estimate", "approve",
    "request-changes", "unblock", "terminate", "archive",
)

# ---------------------------------------------------------------------------
# A.1 칸반 — 보드·카드·상세
# ---------------------------------------------------------------------------

BOARD_META_REQUIRED = frozenset({"slug"})
BOARD_META_OPTIONAL = frozenset({
    "name", "description", "is_current", "total", "default_workdir",
    "default_workspace_kind", "project_id", "project_name",
})
BOARD_META_KEYS = BOARD_META_REQUIRED | BOARD_META_OPTIONAL

DIAGNOSTIC_ACTION_REQUIRED = frozenset({"kind", "label"})
DIAGNOSTIC_ACTION_OPTIONAL = frozenset({"payload", "suggested"})
DIAGNOSTIC_ACTION_KEYS = DIAGNOSTIC_ACTION_REQUIRED | DIAGNOSTIC_ACTION_OPTIONAL

DIAGNOSTIC_REQUIRED = frozenset({
    "kind", "severity", "title", "detail", "actions", "count", "last_seen_at", "data",
})
DIAGNOSTIC_KEYS = DIAGNOSTIC_REQUIRED
DIAGNOSTIC_SEVERITIES = ("critical", "error", "warning")

# 보드 열에 실리는 카드 요약(KanbanTask)
KANBAN_TASK_REQUIRED = frozenset({"id", "title", "status"})
KANBAN_TASK_OPTIONAL = frozenset({
    "body", "assignee", "priority", "tenant", "created_at", "latest_summary",
    "comment_count", "link_counts", "progress", "warnings", "started_at",
    "worker_pid", "last_heartbeat_at",
})
KANBAN_TASK_KEYS = KANBAN_TASK_REQUIRED | KANBAN_TASK_OPTIONAL

# 카드 상세에서만 오는 필드(KanbanTaskFull = KanbanTask & …)
KANBAN_TASK_FULL_EXTRA_OPTIONAL = frozenset({
    "result", "created_by", "model_override", "provider_override", "reasoning_effort",
    "completed_at", "last_failure_error", "workspace_kind", "workspace_path",
    "branch_name", "consecutive_failures", "diagnostics",
})
KANBAN_TASK_FULL_REQUIRED = KANBAN_TASK_REQUIRED
KANBAN_TASK_FULL_OPTIONAL = KANBAN_TASK_OPTIONAL | KANBAN_TASK_FULL_EXTRA_OPTIONAL
KANBAN_TASK_FULL_KEYS = KANBAN_TASK_FULL_REQUIRED | KANBAN_TASK_FULL_OPTIONAL

KANBAN_RUN_REQUIRED = frozenset({"id", "status"})
KANBAN_RUN_OPTIONAL = frozenset({
    "profile", "outcome", "summary", "error", "metadata", "worker_pid", "started_at", "ended_at",
})
KANBAN_RUN_KEYS = KANBAN_RUN_REQUIRED | KANBAN_RUN_OPTIONAL

# 타임라인용 실행 기록(`GET /kanban/runs`). 카드별 `runs[]` 보다 넓다 — 어느 카드·어느
# 서브프로젝트·어느 보드의 실적인지가 응답만 보고 가려져야 다시 조인하지 않는다.
KANBAN_TIMELINE_RUN_REQUIRED = KANBAN_RUN_REQUIRED | frozenset({"task_id", "board"})
KANBAN_TIMELINE_RUN_OPTIONAL = KANBAN_RUN_OPTIONAL | frozenset({"task_title", "tenant", "step_key"})
KANBAN_TIMELINE_RUN_KEYS = KANBAN_TIMELINE_RUN_REQUIRED | KANBAN_TIMELINE_RUN_OPTIONAL

KANBAN_COMMENT_REQUIRED = frozenset({"id", "author", "body", "created_at"})
KANBAN_COMMENT_KEYS = KANBAN_COMMENT_REQUIRED

# 카드별 이력(KanbanEvent) — 통합 사건 스트림(PluginEvent)과는 다른 모양이다.
KANBAN_EVENT_REQUIRED = frozenset({"id", "kind", "payload", "created_at"})
KANBAN_EVENT_KEYS = KANBAN_EVENT_REQUIRED

KANBAN_ATTACHMENT_REQUIRED = frozenset({"id", "filename"})
KANBAN_ATTACHMENT_OPTIONAL = frozenset({"size"})
KANBAN_ATTACHMENT_KEYS = KANBAN_ATTACHMENT_REQUIRED | KANBAN_ATTACHMENT_OPTIONAL

KANBAN_COLUMN_KEYS = frozenset({"name", "tasks"})
KANBAN_BOARD_KEYS = frozenset({"columns", "tenants", "assignees", "latest_event_id", "now"})

# KanbanTaskDetail — 전부 필수. `attachments` 는 첨부 기능이 없을 때 null.
KANBAN_TASK_DETAIL_REQUIRED = frozenset({"task", "comments", "events", "attachments", "links", "runs"})
KANBAN_TASK_DETAIL_KEYS = KANBAN_TASK_DETAIL_REQUIRED
KANBAN_TASK_DETAIL_LINKS_KEYS = frozenset({"parents", "children"})

WORKER_LOG_KEYS = frozenset({"exists", "size_bytes", "content", "truncated"})
KANBAN_PROFILE_SUMMARY_KEYS = frozenset({"name", "is_default", "description"})
DISPATCH_RESULT_KEYS = frozenset({"spawned"})
DISPATCH_SPAWNED_REQUIRED = frozenset({"task_id"})
DISPATCH_SPAWNED_OPTIONAL = frozenset({"profile", "run_id"})

# ---------------------------------------------------------------------------
# A.1 칸반 — 요청 본문
# ---------------------------------------------------------------------------

CREATE_TASK_REQUIRED = frozenset({"title"})
CREATE_TASK_OPTIONAL = frozenset({
    "body", "assignee", "tenant", "priority", "workspace_kind", "workspace_path", "parents",
    "triage", "idempotency_key", "max_runtime_seconds", "skills", "goal_mode",
    "goal_max_turns", "model_override", "provider_override", "reasoning_effort", "project_id",
    "initial_status",
})

# 생성 시점에만 지정할 수 있는 상태. Hermes 의 `VALID_INITIAL_STATUSES` 와 같아야 한다
# (`hermes_cli/kanban_db.py`). `blocked` 는 사람이 풀어 줄 때까지 sticky 라 실행 전 승인
# 대기 자리로 쓴다 — `triage` 는 게이트웨이가 자동 분해하므로 그 용도로 쓸 수 없다.
INITIAL_STATUSES = ("running", "blocked")
CREATE_TASK_KEYS = CREATE_TASK_REQUIRED | CREATE_TASK_OPTIONAL

# PATCH — CreateTaskBody 에서 idempotency_key 를 뺀 전부가 선택이고 status 가 더 붙는다.
UPDATE_TASK_KEYS = (CREATE_TASK_KEYS - {"idempotency_key"}) | frozenset({"status"})

CREATE_BOARD_REQUIRED = frozenset({"slug", "name"})
CREATE_BOARD_OPTIONAL = frozenset({"default_workdir"})
CREATE_BOARD_KEYS = CREATE_BOARD_REQUIRED | CREATE_BOARD_OPTIONAL
UPDATE_BOARD_KEYS = frozenset({"name", "description", "default_workdir"})

ORCHESTRATION_SETTINGS_REQUIRED = frozenset({
    "orchestrator_profile", "default_assignee", "auto_decompose",
    "resolved_orchestrator_profile", "resolved_default_assignee",
})
ORCHESTRATION_SETTINGS_OPTIONAL = frozenset({"max_in_progress", "max_in_progress_per_profile"})
ORCHESTRATION_SETTINGS_KEYS = ORCHESTRATION_SETTINGS_REQUIRED | ORCHESTRATION_SETTINGS_OPTIONAL
UPDATE_ORCHESTRATION_KEYS = frozenset({
    "orchestrator_profile", "default_assignee", "auto_decompose",
    "max_in_progress", "max_in_progress_per_profile",
})

# ---------------------------------------------------------------------------
# A.1 통합 사건 — /deskrpg/events
# ---------------------------------------------------------------------------

EVENT_KINDS = frozenset({
    "task.created",
    "task.status",
    "task.comment",
    "task.run.started",
    "task.run.finished",
    "task.deleted",
    "task.link",
    "cron.run.started",
    "cron.run.finished",
    # 아티팩트 출처 — `include=artifacts` 로 옵트인했을 때만 실린다(R17).
    "artifact.created",
    "artifact.versioned",
    "artifact.deleted",
    "artifact.capture_failed",
    "artifact.delete_partial",
    # 카드 제안 — `include=card_proposals` 로 옵트인했을 때만 실린다. 아티팩트 옵트인과 독립이다.
    "card_proposal.created",
})

PLUGIN_EVENT_REQUIRED = frozenset({"id", "ts", "kind", "payload"})
PLUGIN_EVENT_OPTIONAL = frozenset({"board", "task_id", "profile", "job_id", "run_id", "artifact_id"})
PLUGIN_EVENT_KEYS = PLUGIN_EVENT_REQUIRED | PLUGIN_EVENT_OPTIONAL

EVENTS_PAGE_KEYS = frozenset({"events", "cursor", "has_more"})
EVENT_HANDOFF_KEYS = frozenset({"cursor"})

TASK_STATUS_PAYLOAD_KEYS = frozenset({"from", "to", "parent_count", "title", "assignee"})
CRON_RUN_STARTED_PAYLOAD_KEYS = frozenset({"job_id", "job_name", "profile", "session_id", "started_at"})
CRON_RUN_FINISHED_PAYLOAD_KEYS = CRON_RUN_STARTED_PAYLOAD_KEYS | frozenset({"status", "ended_at", "result_text"})
CRON_RUN_FINISHED_STATUSES = ("ok", "error")

# ---------------------------------------------------------------------------
# A.2 크론 — /p/{profile}/deskrpg/cron
# ---------------------------------------------------------------------------

CRON_JOB_STATES = ("scheduled", "paused", "running", "error", "completed", "disabled")

CRON_SCHEDULE_REQUIRED = frozenset({"kind"})
CRON_SCHEDULE_OPTIONAL = frozenset({"expr", "minutes", "run_at", "display"})
CRON_SCHEDULE_KEYS = CRON_SCHEDULE_REQUIRED | CRON_SCHEDULE_OPTIONAL

# CronJob — 전부 필수(null 허용 필드는 키는 있고 값이 null).
CRON_JOB_REQUIRED = frozenset({
    "id", "name", "prompt", "schedule", "schedule_display", "repeat", "enabled", "state",
    "next_run_at", "last_run_at", "last_status", "last_error", "deliver", "skills",
    "model", "provider", "created_at",
})
CRON_JOB_KEYS = CRON_JOB_REQUIRED
CRON_JOB_NULLABLE = frozenset({
    "next_run_at", "last_run_at", "last_status", "last_error", "deliver", "model", "provider",
})

CRON_RUN_REQUIRED = frozenset({"id", "started_at", "ended_at", "status", "summary", "result_text"})
CRON_RUN_KEYS = CRON_RUN_REQUIRED

CREATE_CRON_JOB_REQUIRED = frozenset({"schedule", "name"})
# prompt 는 script 가 없을 때만 필수 — 라우트가 판단한다.
CREATE_CRON_JOB_OPTIONAL = frozenset({
    "prompt", "script", "deliver", "model", "provider", "skills", "paused", "repeat",
})
CREATE_CRON_JOB_KEYS = CREATE_CRON_JOB_REQUIRED | CREATE_CRON_JOB_OPTIONAL

UPDATE_CRON_JOB_UPDATES_KEYS = frozenset({
    "schedule", "prompt", "name", "deliver", "model", "provider", "enabled",
})

DELIVERY_TARGET_REQUIRED = frozenset({"id", "name", "home_target_set", "home_env_var"})
DELIVERY_TARGET_KEYS = DELIVERY_TARGET_REQUIRED

BLUEPRINT_FIELD_REQUIRED = frozenset({"name", "type", "label"})
BLUEPRINT_FIELD_OPTIONAL = frozenset({"default", "options", "optional", "strict", "help"})
BLUEPRINT_FIELD_KEYS = BLUEPRINT_FIELD_REQUIRED | BLUEPRINT_FIELD_OPTIONAL
BLUEPRINT_FIELD_TYPES = ("enum", "text", "time", "weekdays")

AUTOMATION_BLUEPRINT_REQUIRED = frozenset({
    "key", "title", "description", "category", "tags", "fields", "command", "appUrl",
})
AUTOMATION_BLUEPRINT_KEYS = AUTOMATION_BLUEPRINT_REQUIRED

INSTANTIATE_BLUEPRINT_REQUIRED = frozenset({"blueprint", "values"})

# ---------------------------------------------------------------------------
# 응답 봉투 — fake-plugin-server.ts 가 내는 최상위 키
# ---------------------------------------------------------------------------

ENVELOPES = {
    "boards": frozenset({"boards", "current"}),
    "board": frozenset({"board"}),
    "task": frozenset({"task"}),
    "comment": frozenset({"comment"}),
    "attachment": frozenset({"attachment"}),
    "attachments": frozenset({"attachments"}),
    "jobs": frozenset({"jobs"}),
    "job": frozenset({"job"}),
    "runs": frozenset({"runs"}),
    "targets": frozenset({"targets"}),
    "blueprints": frozenset({"blueprints"}),
    "ok": frozenset({"ok"}),
    "accepted": frozenset({"accepted"}),
    "error": frozenset({"error", "detail"}),
}

# ---------------------------------------------------------------------------
# 아티팩트 (0.8.0)
# ---------------------------------------------------------------------------
ARTIFACT_KINDS = ("document", "image", "media", "web", "react", "data", "file", "link")
ARTIFACT_SOURCES = ("chat", "kanban", "cron")
ARTIFACT_SUMMARY_REQUIRED = frozenset({
    "id", "kind", "title", "profile", "source_kind", "session_id", "current_version",
    "filename", "mime", "size", "sha256", "created_at", "updated_at",
})
ARTIFACT_SUMMARY_OPTIONAL = frozenset({"summary", "board", "task_id", "job_id", "run_id", "missing"})
ARTIFACT_SUMMARY_KEYS = ARTIFACT_SUMMARY_REQUIRED | ARTIFACT_SUMMARY_OPTIONAL
ARTIFACT_VERSION_REQUIRED = frozenset({"version", "filename", "mime", "size", "sha256", "created_by", "captured_via", "created_at"})
ARTIFACT_VERSION_OPTIONAL = frozenset({"origin_path", "note", "pruned_at"})
ARTIFACT_VERSION_KEYS = ARTIFACT_VERSION_REQUIRED | ARTIFACT_VERSION_OPTIONAL

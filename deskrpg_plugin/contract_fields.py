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

PLUGIN_INFO_REQUIRED = frozenset({"plugin", "version", "capabilities", "timezone", "kanban", "dashboard_url"})
# `routes` 는 0.1.0 부터 내던 필드라 유지한다. 계약 타입에는 없지만 해가 없다.
PLUGIN_INFO_KEYS = PLUGIN_INFO_REQUIRED | frozenset({"routes"})
PLUGIN_INFO_KANBAN_KEYS = frozenset({"dispatcher_present", "attachments", "attachment_max_bytes"})
# 항상 있는 것. 스웜처럼 Hermes 빌드에 따라 갈리는 것은 `capabilities()` 가 붙인다.
CAPABILITIES = ("kanban", "cron", "events")


def capabilities(api) -> tuple[str, ...]:
    """이 Hermes 빌드에서 **실제로 되는** 것만 돌려준다.

    버전만 보고 판단하면 "새 플러그인인데 404" 라는 진단 불가능한 상태가 된다.
    capability 문자열이 가용성을 말하게 한다.
    """
    extra = ("swarm",) if getattr(api, "create_swarm", None) is not None else ()
    return CAPABILITIES + extra

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
})
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
})

PLUGIN_EVENT_REQUIRED = frozenset({"id", "ts", "kind", "payload"})
PLUGIN_EVENT_OPTIONAL = frozenset({"board", "task_id", "profile", "job_id", "run_id"})
PLUGIN_EVENT_KEYS = PLUGIN_EVENT_REQUIRED | PLUGIN_EVENT_OPTIONAL

EVENTS_PAGE_KEYS = frozenset({"events", "cursor", "has_more"})

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

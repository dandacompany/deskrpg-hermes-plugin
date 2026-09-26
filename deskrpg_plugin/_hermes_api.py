"""Hermes 내부 API 를 부르는 **유일한** 지점.

여기 말고 어디서도 `hermes_cli.*`·`cron.*`·`hermes_state` 를 임포트하지 않는다. Hermes 가
함수를 옮기거나 이름을 바꾸면 이 파일 하나만 고치면 되고, 무엇이 없어졌는지도 한눈에 보인다.

전부 아니면 전무다. 심볼이 하나라도 없으면 로드를 포기한다 — 목록은 되는데
삭제만 조용히 실패하는 반쯤 동작하는 상태가 최악이다.

`OPTIONAL_SPEC` 은 예외다 — 없으면 `None` 으로 남기고 로드는 계속한다. 그 심볼을 쓰는
라우트가 **통째로** 등록되지 않으므로 반쯤 동작하는 상태가 생기지 않는다.

## 접근 규약 (0.6.0 — 다른 모듈은 이 규약을 따른다)

`load()` 가 돌려주는 네임스페이스는 **평면(flat)** 이다. 원래 모듈이 무엇이든 심볼 이름
그대로 `api.<name>` 으로 부른다:

    api.create_task(conn, title=...)      # hermes_cli.kanban_db.create_task
    api.dispatch_once(...)                # hermes_cli.kanban_db_dispatch.dispatch_once
    api.list_jobs()                       # cron.jobs.list_jobs
    api.get_timezone()                    # hermes_time.get_timezone
    api.MAX_REQUEST_BYTES                 # gateway.platforms.api_server.MAX_REQUEST_BYTES

하위 네임스페이스(`api.kanban_db.create_task`)는 두지 않는다 — 기존 프로필/소울 심볼이
이미 평면이었고, 테스트의 `fake_api` 도 평면 SimpleNamespace 라 한 벌로 흉내 낼 수 있다.
그래서 이름이 겹치면 안 된다 — `SPEC` 을 넓힐 때 import 시점의 중복 검사가 막는다.

`REQUIRED` 는 `SPEC` 에 든 모든 이름의 평면 튜플이다(테스트·가드가 순회한다).
"""

import importlib
import types

# (모듈 경로, 그 모듈에서 가져올 이름들). 순서는 문서용이고 동작에는 무관하다.
#
# `connect`/`connect_closing` 은 0.21.1 에서 `hermes_cli.kanban_db_connect` 로 쪼개졌고
# `hermes_cli.kanban_db` 에는 "revert-scheduled" 호환 __getattr__ 로만 남아 있다
# (COMPAT_MANIFEST.md). 호환 블록이 걷혀도 살아남도록 원래 모듈에서 직접 가져온다.
SPEC = (
    (
        "hermes_cli.profiles",
        (
            "get_profile_dir",
            "profile_exists",
            "validate_profile_name",
            "list_profiles",
            "create_profile",
            "delete_profile",
            "remove_wrapper_script",
            "read_profile_meta",
            "get_active_profile_name",
            "normalize_profile_name",
        ),
    ),
    ("hermes_cli.default_soul", ("DEFAULT_SOUL_MD", "is_legacy_template_soul")),
    ("hermes_cli.kanban_db_connect", ("connect", "connect_closing")),
    (
        "hermes_cli.kanban_db",
        (
            "init_db",
            "board_exists",
            "list_boards",
            "create_board",
            "write_board_metadata",
            "get_current_board",
            "scoped_current_board",
            "list_tasks",
            "get_task",
            "create_task",
            "assign_task",
            "complete_task",
            "block_task",
            "schedule_task",
            "request_review",
            "request_changes",
            "unblock_task",
            "reopen_review_task",
            "archive_task",
            "delete_task",
            "set_model_override",
            "set_reasoning_effort",
            "add_comment",
            "list_comments",
            "list_events",
            "link_tasks",
            "unlink_tasks",
            "parent_ids",
            "child_ids",
            "list_runs",
            "get_run",
            "reclaim_task",
            "reassign_task",
            "list_attachments",
            "get_attachment",
            "delete_attachment",
            "store_attachment_bytes",
            "attachments_root",
            "read_worker_log",
            "latest_summaries",
            "latest_summary",
            "task_age",
            "task_graph_contexts",
            "notify_task_updated",
            "known_assignees",
            "write_txn",
            "VALID_STATUSES",
            "KANBAN_ATTACHMENT_MAX_BYTES",
            "kanban_home",
            "kanban_db_path",
            "board_dir",
            "AttachmentTooLarge",
            "invalidate_descendants_for_parent_reopen",
            "recompute_ready",
            # Public verbs the upstream dashboard uses — they replace the underscore internals and direct SQL
            # this list used to require (decision 0017).
            "unsatisfied_parents",
            "promote_task",
            "edit_task",
        ),
    ),
    ("hermes_cli.kanban_db_dispatch", ("dispatch_once",)),
    ("hermes_cli.kanban_specify", ("specify_task",)),
    ("hermes_cli.kanban_decompose", ("decompose_task",)),
    ("hermes_cli.kanban_diagnostics", ("compute_task_diagnostics", "config_from_runtime_config")),
    ("hermes_cli.config", ("load_config", "save_config")),
    ("hermes_constants", ("set_hermes_home_override", "reset_hermes_home_override", "get_hermes_home")),
    ("hermes_time", ("get_timezone",)),
    (
        "cron.jobs",
        (
            "use_cron_store",
            "list_jobs",
            "get_job",
            "update_job",
            "pause_job",
            "resume_job",
            "trigger_job",
            "remove_job",
            "effective_job_state",
            "get_cron_output_dir",
        ),
    ),
    ("cron.scheduler", ("create_job_with_scheduler_registration", "CronSchedulerRegistrationError")),
    ("cron.scheduler_delivery", ("cron_delivery_targets",)),
    (
        "cron.blueprint_catalog",
        ("CATALOG", "get_blueprint", "blueprint_catalog_entry", "fill_blueprint", "BlueprintFillError"),
    ),
    ("cron.executions", ("list_executions", "get_execution")),
    ("cron.scheduler_provider", ("resolve_cron_scheduler", "InProcessCronScheduler")),
    ("hermes_state", ("SessionDB",)),
    ("gateway.platforms.api_server", ("MAX_REQUEST_BYTES",)),
)

# 없어도 플러그인은 뜬다 — 그 기능만 꺼진다.
#
# `SPEC` 은 전부 아니면 전무다. 그 규칙의 의도는 "목록은 되는데 삭제만 조용히 실패하는
# 반쯤 동작하는 상태" 를 막는 것이다. 스웜은 다르다 — 없으면 라우트가 **통째로** 등록되지
# 않으므로 반쯤 되는 상태가 생기지 않는다. 반대로 이걸 `SPEC` 에 넣으면 `kanban_swarm`
# 이 없는 구버전 Hermes 에서 칸반·크론까지 전부 죽는다.
OPTIONAL_SPEC = (
    # Hermes internals with no public replacement. Upstream can move them at any time, so the plugin must still
    # load without them: a missing probe drops only the "dispatcher missing" warning, and a missing terminator
    # only refuses reopening a finished card whose descendants are running (kanban_board._write_status).
    ("hermes_cli.kanban", ("_check_dispatcher_presence",)),
    ("hermes_cli.kanban_db_dispatch", ("_terminate_reclaimed_worker",)),
    ("hermes_cli.kanban_review_policy", ("API_VERSION", "get_review_state", "approve_task",
        "update_review_policy", "guard_task_mutation", "patch_review_task")),
    # 계획 B — 디바이스 코드 로그인. 대시보드 라우터 모듈이라 fastapi 가 없는 빌드에서는 통째로 빠진다.
    (
        "hermes_cli.web_routers.oauth",
        # `_gc_oauth_sessions` 는 없으면 시작 전 만료 세션 정리만 빠진다.
        ("_DEVICE_CODE_STARTERS", "_start_device_code_flow", "poll_oauth_session", "_gc_oauth_sessions"),
    ),
    (
        "hermes_cli.web_server_oauth",
        ("_OAUTH_PROVIDER_CATALOG", "_oauth_sessions", "_oauth_sessions_lock", "_oauth_profile_name"),
    ),
    # 계획 B — OAuth 가 default 를 None(=프로세스 홈)으로 넘겨도 되는지 본다. 없으면 default 의 앱 안 로그인만 거절된다.
    ("hermes_constants", ("get_process_hermes_home",)),
    ("hermes_cli.kanban_swarm", ("create_swarm", "latest_blackboard", "SwarmWorkerSpec")),
    # Swarm on an approval-policy board: the plugin assembles the swarm in one transaction so every result card
    # gets its policy before any worker can be dispatched (kanban_swarm_policy.py). These are Hermes internals;
    # the `swarm_review_policy` capability is announced only when all of them are present with the expected
    # signatures (contract_fields.has_swarm_policy_symbols).
    ("hermes_cli.kanban_swarm", ("_create_swarm_uncommitted", "_activate_root_inline")),
    ("hermes_cli.kanban_review_policy", ("create_policy", "inherited_policy")),
    ("hermes_cli.kanban_db", ("latest_run", "_fire_kanban_lifecycle_hook")),
    # 0.7.1 — 대시보드 공개 주소. 없는 빌드는 `/deskrpg/info` 의 dashboard_url 만 null 이 된다.
    ("hermes_cli.dashboard_auth.prefix", ("resolve_public_url",)),
    # 0.9.0 — 직원 설정 피커. 없는 빌드는 그 라우트와 capability 만 빠진다.
    (
        "hermes_cli.tools_config",
        (
            "_get_effective_configurable_toolsets",
            "_get_platform_tools",
            "_toolset_has_keys",
            "_toolset_allowed_for_platform",
            # 0.9.0 — config PUT 이 `_save_platform_tools` 와 같은 규칙으로 쓰는 데 쓴다.
            "_configurable_keys",
            "_platform_default_keys",
            "_get_plugin_toolset_keys",
            # 0.10.0 — 도구별 프로바이더. 대시보드 도구 설정 라우터와 같은 함수다.
            "TOOL_CATEGORIES",
            "_visible_providers",
            "provider_readiness_status",
            "_is_provider_active",
            "apply_provider_selection",
        ),
    ),
    # 툴셋 목록이 구독 기능 판정을 한 번만 계산하는 데 쓴다. 없으면 툴셋마다 Hermes 가 다시 계산한다.
    ("hermes_cli.nous_subscription", ("get_nous_subscription_features",)),
    ("tools.skills_tool", ("_find_all_skills", "_sort_skills")),
    ("agent.skill_utils", ("ESSENTIAL_SKILLS", "parse_config_string_list", "is_external_skill_path")),
    # `_plugin_aliases` 는 복제가 설정의 프로바이더 id 를 Hermes 와 같이 정식 id 로 푸는 데 쓴다.
    (
        "hermes_cli.auth",
        ("PROVIDER_REGISTRY", "_plugin_aliases", "clear_provider_auth"),
    ),
    # 0.15.0 — NPC 스킬 관리. 한 릴리스로 함께 나가므로 하나라도 없으면 profile_skill_admin 전체가 빠진다.
    (
        "tools.skill_usage",
        (
            "load_usage", "activity_count", "latest_activity_at", "is_curator_managed",
            "is_hub_installed", "is_bundled", "set_pinned", "archive_skill", "restore_skill",
            "list_archived_skill_names", "_archive_dir", "_find_skill_dir", "_find_external_skill_dir",
        ),
    ),
    ("tools.skill_ledger", ("capture_before", "append_entry", "set_ledger_actor", "reset_ledger_actor")),
    ("tools.skill_manager_tool", ("_create_skill", "_edit_skill", "_write_file", "_find_skill")),
    ("agent.prompt_builder", ("clear_skills_system_prompt_cache",)),
    (
        "agent.curator",
        ("load_state", "is_enabled", "is_paused", "set_paused", "get_interval_hours",
         "get_min_idle_hours", "get_stale_after_days", "get_archive_after_days"),
    ),
    ("agent.learning_graph", ("build_learning_graph",)),
    ("agent.learning_mutations", ("node_detail", "edit_node", "delete_node", "parse_node_kind")),
    ("tools.skills_hub_search", ("create_source_router", "parallel_search_sources")),
    ("hermes_cli.skills_hub", ("_resolve_source_meta_and_bundle",)),
    ("tools.skills_hub_install", ("quarantine_bundle",)),
    ("tools.skills_guard", ("scan_skill", "should_allow_install")),
    ("hermes_cli.web_server_gateway", ("_profile_action_environment", "_dashboard_spawn_executable")),
    # 0.17.0 — NPC MCP 커넥터 관리. 전부 있을 때만 profile_mcp_admin 을 알린다(contract_fields).
    # 모듈 간 흔한 이름(start·registry·list_catalog …)은 `(원래 이름, 평면 이름)` 쌍으로 별칭을 준다.
    ("hermes_cli.mcp_config", (
        "_get_mcp_servers", "_save_mcp_server", "_remove_mcp_server", "_env_key_for_server",
        "_bearer_auth_headers", "_oauth_tokens_present", "redact_mcp_probe_text",
        "_resolve_mcp_server_config",
    )),
    ("hermes_cli.mcp_security", ("validate_mcp_server_entry",)),
    ("tools.mcp_tool_loop", ("_ensure_mcp_loop", "_run_on_mcp_loop")),
    ("tools.mcp_tool_discovery", ("_connect_server", "discover_mcp_tools")),
    ("tools.mcp_tool_lifecycle", ("_stop_mcp_loop_if_idle", "shutdown_mcp_servers")),
    ("tools.mcp_tool_agent", ("reprobe_tool_availability",)),
    ("tools.registry", (("registry", "mcp_registry"),)),
    ("gateway.run", ("_profile_runtime_scope",)),
    ("hermes_cli.mcp_catalog", (
        ("list_catalog", "mcp_list_catalog"), ("get_entry", "mcp_get_catalog_entry"),
        ("card_install_config", "mcp_card_install_config"),
    )),
    ("tools.connectors.mcp_oauth", (("start", "mcp_oauth_start"), ("cancel_attempt", "mcp_oauth_cancel_attempt"))),
    ("tui_gateway.mcp_oauth_sessions", ("deliver_callback_flow", "poll_flow", "cancel_flow")),
    # 0.18.0 — 무인 실행 막힘 사건. 막힌 명령의 위험 패턴 키를 다시 계산하고, 명령을 가려서 싣는다.
    # 없어도 사건은 남는다(패턴·명령만 빠진다) — capability 판정에 넣지 않는다.
    ("tools.approval_detection", ("detect_dangerous_command",)),
    ("agent.redact", ("redact_sensitive_text",)),
)

def _pairs(names):
    """`"name"` 또는 `("원래 이름", "평면 이름")` 을 `(src, dst)` 쌍으로 푼다."""
    return [(n, n) if isinstance(n, str) else n for n in names]


REQUIRED = tuple(dst for _module, names in SPEC for _src, dst in _pairs(names))
OPTIONAL = tuple(dst for _module, names in OPTIONAL_SPEC for _src, dst in _pairs(names))


def _assert_no_duplicate_names():
    # 평면 네임스페이스라 이름이 겹치면 한쪽이 조용히 다른 쪽을 덮는다 — import 시점에 막는다.
    seen = set()
    for _module, names in (*SPEC, *OPTIONAL_SPEC):
        for _src, name in _pairs(names):
            if name in seen:
                raise AssertionError(f"duplicate name in _hermes_api.SPEC: {name}")
            seen.add(name)


_assert_no_duplicate_names()


class MissingHermesApi(RuntimeError):
    """이 Hermes 빌드에 필요한 내부 API 가 없다."""


def load() -> types.SimpleNamespace:
    resolved = {}
    missing = []
    for module_path, names in SPEC:
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # ImportError 뿐 아니라 초기화 실패도 잡는다
            raise MissingHermesApi(f"{module_path} cannot be imported: {exc!r}") from exc
        for src, dst in _pairs(names):
            value = getattr(module, src, None)
            if value is None:
                missing.append(f"{module_path}.{src}")
            resolved[dst] = value

    if missing:
        raise MissingHermesApi("missing symbols: " + ", ".join(missing))

    for module_path, names in OPTIONAL_SPEC:
        try:
            module = importlib.import_module(module_path)
        except Exception:
            # 모듈 자체가 없는 구버전 Hermes. 이 기능만 끄고 계속한다.
            module = None
        for src, dst in _pairs(names):
            resolved[dst] = getattr(module, src, None) if module is not None else None

    return types.SimpleNamespace(**resolved)

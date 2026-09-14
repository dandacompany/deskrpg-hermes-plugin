"""Hermes 내부 API 를 부르는 **유일한** 지점.

여기 말고 어디서도 `hermes_cli.*`·`cron.*`·`hermes_state` 를 임포트하지 않는다. Hermes 가
함수를 옮기거나 이름을 바꾸면 이 파일 하나만 고치면 되고, 무엇이 없어졌는지도 한눈에 보인다.

전부 아니면 전무다. 심볼이 하나라도 없으면 로드를 포기한다 — 목록은 되는데
삭제만 조용히 실패하는 반쯤 동작하는 상태가 최악이다.

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
            "_retry_status_for_run",
            "_parents_satisfied",
            "_end_run",
            "invalidate_descendants_for_parent_reopen",
            "recompute_ready",
        ),
    ),
    ("hermes_cli.kanban_db_dispatch", ("dispatch_once", "_terminate_reclaimed_worker")),
    ("hermes_cli.kanban_specify", ("specify_task",)),
    ("hermes_cli.kanban_decompose", ("decompose_task",)),
    ("hermes_cli.kanban_diagnostics", ("compute_task_diagnostics", "config_from_runtime_config")),
    ("hermes_cli.kanban", ("_check_dispatcher_presence",)),
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

REQUIRED = tuple(name for _module, names in SPEC for name in names)


def _assert_no_duplicate_names():
    # 평면 네임스페이스라 이름이 겹치면 한쪽이 조용히 다른 쪽을 덮는다 — import 시점에 막는다.
    seen = set()
    for _module, names in SPEC:
        for name in names:
            if name in seen:
                raise AssertionError(f"_hermes_api.SPEC 에 같은 이름이 두 번 있다: {name}")
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
            raise MissingHermesApi(f"{module_path} 를 임포트할 수 없다: {exc!r}") from exc
        for name in names:
            value = getattr(module, name, None)
            if value is None:
                missing.append(f"{module_path}.{name}")
            resolved[name] = value

    if missing:
        raise MissingHermesApi("없는 심볼: " + ", ".join(missing))

    return types.SimpleNamespace(**resolved)

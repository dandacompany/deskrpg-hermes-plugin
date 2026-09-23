import contextlib
import os
import re
import threading
import types
from pathlib import Path

import pytest
from aiohttp import web

# 실제 Hermes 0.20.6 의 validate_profile_name 정규식(앵커 있음). fake 가 이보다
# 느슨하면 대문자·점·유니코드 이름이 테스트에서만 통과해 회귀를 못 잡는다(M-7).
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


async def _unfaked_oauth(*_a, **_k):
    raise AssertionError("OAuth 시작·폴링 가짜를 덮지 않은 테스트가 불렀다")


class FakeAdapter:
    """api_server 어댑터를 흉내 낸다. _check_auth 만 쓴다."""

    def __init__(self, *, authorized: bool):
        self.authorized = authorized
        self.checked = []

    def _check_auth(self, request):
        self.checked.append(request.path)
        if self.authorized:
            return None
        return web.json_response({"error": "unauthorized"}, status=401)


@pytest.fixture
def fake_api(tmp_path):
    """Hermes 내부 API 를 흉내 낸다. 프로필은 tmp_path 아래 디렉토리다."""

    def get_profile_dir(name):
        return tmp_path / "profiles" / name

    def profile_exists(name):
        return get_profile_dir(name).is_dir()

    def validate_profile_name(name):
        if not name or not _PROFILE_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid profile name: {name!r}")

    def list_profiles():
        root = tmp_path / "profiles"
        if not root.is_dir():
            return []
        return [types.SimpleNamespace(name=p.name) for p in sorted(root.iterdir())]

    def get_wrapper_path(name):
        return tmp_path / "wrappers" / name

    def create_profile(name, **kwargs):
        d = get_profile_dir(name)
        d.mkdir(parents=True, exist_ok=False)
        (d / "SOUL.md").write_text("You are Hermes Agent", encoding="utf-8")
        # 실제 CLI 는 프로필 생성과 함께 wrapper 스크립트도 만든다 — 여기서도
        # 만들어 둬야 delete_profile/remove_wrapper_script 의 순서 상호작용을
        # 진실되게 재현할 수 있다(I-3).
        wrapper = get_wrapper_path(name)
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        return d

    def remove_wrapper_script(name):
        wrapper = get_wrapper_path(name)
        if wrapper.is_file():
            wrapper.unlink()
            return True
        return False

    def delete_profile(name, yes=False):
        import shutil

        d = get_profile_dir(name)
        shutil.rmtree(d)
        # 0.20.6 의 delete_profile 은 wrapper 가 남아 있으면 스스로 지운다 —
        # 그 부수효과를 재현해야 "delete_profile 뒤에 부르면 항상 False" 라는
        # 실제 버그를 fake 위에서도 관찰할 수 있다.
        wrapper = get_wrapper_path(name)
        if wrapper.is_file():
            wrapper.unlink()
        return d

    class FakeSwarmWorkerSpec:
        """실제 `SwarmWorkerSpec` 은 frozen dataclass 다. 테스트는 넘어온 값만 본다."""

        def __init__(self, *, profile, title, body, skills=(), priority=0, max_runtime_seconds=None):
            self.profile = profile
            self.title = title
            self.body = body
            self.skills = list(skills)
            self.priority = priority
            self.max_runtime_seconds = max_runtime_seconds

    swarm_calls = []

    def create_swarm(conn, *, goal, workers, verifier_assignee, synthesizer_assignee, **kw):
        from tests.fakes_kanban import FakeTask

        workers = list(workers)
        swarm_calls.append(
            {
                "goal": goal,
                "workers": workers,
                "verifier_assignee": verifier_assignee,
                "synthesizer_assignee": synthesizer_assignee,
                **kw,
            }
        )
        # 실제 create_swarm 은 루트·워커·검증자·합성자 카드를 진짜로 만든다. 여기서 이름만
        # 흉내 내고 카드를 안 남기면 이후 `latest_blackboard`/`require_task` 조회가
        # (진짜라면 있어야 할) 카드를 못 찾는다 — id 는 고정하되 board 상태에 직접 심는다.
        state = conn.db._state(conn)
        created_at = conn.db._now()
        created_by = kw.get("created_by")

        def _plant(task_id: str, title: str, assignee):
            state.tasks[task_id] = FakeTask(
                id=task_id, title=title, body=title, assignee=assignee, status="running",
                priority=0, created_by=created_by, created_at=created_at,
            )

        worker_ids = [f"t_w{i}" for i in range(len(workers))]
        _plant("t_root", goal, None)
        for wid, worker in zip(worker_ids, workers):
            _plant(wid, worker.title, worker.profile)
        _plant("t_ver", "verify", verifier_assignee)
        _plant("t_syn", "synthesize", synthesizer_assignee)

        return types.SimpleNamespace(
            as_dict=lambda: {
                "root_id": "t_root",
                "worker_ids": worker_ids,
                "verifier_id": "t_ver",
                "synthesizer_id": "t_syn",
            }
        )

    def latest_blackboard(conn, root_id):
        return {"topology": {"goal": "g"}, "_authors": {"topology": "swarm-orchestrator"}}

    FAKE_TOOLSETS = [
        ("web", "🔍 Web", "검색과 스크래핑"),
        ("file", "📁 File", "파일 읽기·쓰기"),
        ("tts", "🔊 TTS", "음성 합성"),
        ("discord", "Discord", "디스코드 전용"),
    ]

    def _get_platform_tools(cfg, platform, *, include_default_mcp_servers=True):
        saved = (cfg.get("platform_toolsets") or {}).get(platform)
        return set(saved) if isinstance(saved, list) else {"web", "file"}

    def _parse_config_string_list(value):
        # agent/skill_utils.py:parse_config_string_list 와 같은 규칙(JSON 배열 문자열 → 목록).
        import ast
        if isinstance(value, str):
            if value.strip().startswith("["):
                parsed = ast.literal_eval(value.strip())
                return [str(x) for x in parsed]
            return [value] if value.strip() else []
        return [str(x) for x in value] if isinstance(value, list) else []

    def _find_all_skills(*, skip_disabled=False):
        return [
            {"name": "hermes-agent", "description": "필수", "category": "core"},
            {"name": "pdf", "description": "PDF 다루기", "category": "docs"},
            {"name": "xlsx", "description": "엑셀", "category": "docs"},
        ]

    api = types.SimpleNamespace(
        get_profile_dir=get_profile_dir,
        get_wrapper_path=get_wrapper_path,
        profile_exists=profile_exists,
        validate_profile_name=validate_profile_name,
        list_profiles=list_profiles,
        create_profile=create_profile,
        delete_profile=delete_profile,
        remove_wrapper_script=remove_wrapper_script,
        read_profile_meta=lambda d: {"description": ""},
        DEFAULT_SOUL_MD="You are Hermes Agent",
        is_legacy_template_soul=lambda text: False,
        # 0.6.0 — hermes_cli.profiles 추가분
        get_active_profile_name=lambda: "default",
        normalize_profile_name=lambda name: (name or "").strip().lower(),
        # 0.7.0 — hermes_cli.kanban_swarm (없으면 None, task-1 의 OPTIONAL_SPEC 규칙과 같다)
        create_swarm=create_swarm,
        latest_blackboard=latest_blackboard,
        SwarmWorkerSpec=FakeSwarmWorkerSpec,
        swarm_calls=swarm_calls,
        # 0.7.1 — hermes_cli.dashboard_auth.prefix (OPTIONAL_SPEC). 기본은 공개 주소 없음.
        resolve_public_url=lambda: "",
        # 0.9.0 — 직원 설정 피커 (OPTIONAL_SPEC). fake 는 심볼이 다 있는 빌드를 흉내 낸다.
        _get_effective_configurable_toolsets=lambda: list(FAKE_TOOLSETS),
        _get_platform_tools=_get_platform_tools,
        _toolset_has_keys=lambda name, cfg=None: name != "tts",
        _toolset_allowed_for_platform=lambda name, platform: name != "discord",
        _configurable_keys=lambda: {row[0] for row in FAKE_TOOLSETS},
        _platform_default_keys=lambda: {"hermes-api-server", "hermes-cron", "hermes-cli", "hermes-telegram"},
        _get_plugin_toolset_keys=lambda: set(),
        parse_config_string_list=_parse_config_string_list,
        _find_all_skills=_find_all_skills,
        _sort_skills=lambda rows: sorted(rows, key=lambda s: (s.get("category") or "", s["name"])),
        ESSENTIAL_SKILLS=frozenset({"hermes-agent"}),
        PROVIDER_REGISTRY={
            "openai": types.SimpleNamespace(id="openai", name="OpenAI", auth_type="api_key",
                                            api_key_env_vars=("OPENAI_API_KEY",), base_url_env_var="OPENAI_BASE_URL"),
            "openai-codex": types.SimpleNamespace(id="openai-codex", name="Codex", auth_type="oauth_external",
                                                  api_key_env_vars=(), base_url_env_var=""),
        },
        # 계획 B — 디바이스 코드 로그인 (OPTIONAL_SPEC). 심볼이 다 있는 빌드를 흉내 낸다(라우트·capability 가 뜬다).
        # 동작이 필요한 테스트(tests/test_oauth.py)는 시작·폴링을 제 가짜로 덮는다.
        _DEVICE_CODE_STARTERS={"openai-codex": object()},
        _start_device_code_flow=_unfaked_oauth,
        poll_oauth_session=_unfaked_oauth,
        _gc_oauth_sessions=lambda: None,
        _OAUTH_PROVIDER_CATALOG=(),
        _oauth_sessions={},
        _oauth_sessions_lock=threading.Lock(),
        _oauth_profile_name=lambda p: None if not p or p == "current" else p,
        clear_provider_auth=lambda pid=None: True,
        # 0.10.0 — 도구별 프로바이더 (OPTIONAL_SPEC). hermes_cli.tools_config 의 대시보드용 함수를 흉내 낸다.
        TOOL_CATEGORIES=FAKE_TOOL_CATEGORIES,
        _visible_providers=_fake_visible_providers,
        provider_readiness_status=_fake_readiness,
        _is_provider_active=_fake_is_active,
        apply_provider_selection=_fake_apply_provider_selection,
    )
    _add_automation_fakes(api, tmp_path)
    from tests.fakes_skills import install_fake_skills

    install_fake_skills(api)
    return api


def _add_automation_fakes(api, tmp_path):
    """0.6.0 이 `_hermes_api.SPEC` 에 더한 모든 심볼의 기본 가짜.

    칸반 DB 는 `tests/fakes_kanban.py` 의 인메모리 골격이고, 나머지는 현실적인 기본값을
    돌려주는 람다다. 테스트가 특정 동작을 원하면 `fake_api.<name> = …` 로 덮는다.
    `_hermes_api.REQUIRED` 의 모든 이름이 여기 있는지는 test_info 가 단정한다.
    """
    from zoneinfo import ZoneInfo

    from tests.fakes_kanban import install_fake_kanban

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(exist_ok=True)
    kanban_root = tmp_path / "kanban"
    kanban_root.mkdir(exist_ok=True)
    api.kanban = install_fake_kanban(api, kanban_root)

    # 홈 오버라이드를 실제처럼 기억한다 — 스택(리스트)이라 중첩 호출도 재현한다.
    _override = []

    def set_hermes_home_override(path):
        _override.append(Path(path))
        return len(_override)

    def reset_hermes_home_override(token):
        del _override[token - 1:]

    def get_hermes_home():
        return _override[-1] if _override else hermes_home

    class _Config(dict):
        pass

    config = _Config({"kanban": {}, "timezone": "Asia/Seoul"})

    class _CronSchedulerRegistrationError(RuntimeError):
        pass

    class _BlueprintFillError(ValueError):
        pass

    class _InProcessCronScheduler:
        pass

    class _SessionDB:
        def __init__(self, *a, **k):
            pass

    api.__dict__.update(
        # kanban_db_dispatch / specify / decompose / diagnostics / kanban
        dispatch_once=lambda *a, **k: types.SimpleNamespace(spawned=[]),
        _terminate_reclaimed_worker=lambda *a, **k: None,
        specify_task=lambda *a, **k: None,
        decompose_task=lambda *a, **k: [],
        compute_task_diagnostics=lambda *a, **k: [],
        config_from_runtime_config=lambda *a, **k: {},
        _check_dispatcher_presence=lambda hermes_home=None: (True, ""),
        # hermes_cli.config
        load_config=lambda *a, **k: config,
        save_config=lambda cfg, *a, **k: None,
        # hermes_constants / hermes_time
        set_hermes_home_override=set_hermes_home_override,
        reset_hermes_home_override=reset_hermes_home_override,
        get_hermes_home=get_hermes_home,
        get_process_hermes_home=lambda: hermes_home,
        get_timezone=lambda: ZoneInfo("Asia/Seoul"),
        # cron.jobs
        use_cron_store=lambda home: _noop_context(),
        list_jobs=lambda include_disabled=False: [],
        get_job=lambda job_id: None,
        update_job=lambda job_id, updates, **k: None,
        pause_job=lambda job_id, **k: False,
        resume_job=lambda job_id, **k: False,
        trigger_job=lambda job_id, **k: False,
        remove_job=lambda job_id, **k: False,
        effective_job_state=lambda job: job.get("state", "scheduled") if isinstance(job, dict) else "scheduled",
        get_cron_output_dir=lambda *a, **k: tmp_path / "cron-output",
        # cron.scheduler / scheduler_delivery / blueprint_catalog / executions / scheduler_provider
        create_job_with_scheduler_registration=lambda **kwargs: {"id": "job-1", **kwargs},
        CronSchedulerRegistrationError=_CronSchedulerRegistrationError,
        cron_delivery_targets=lambda: [],
        CATALOG=[],
        get_blueprint=lambda key: None,
        blueprint_catalog_entry=lambda bp: {},
        fill_blueprint=lambda *a, **k: {},
        BlueprintFillError=_BlueprintFillError,
        list_executions=lambda *a, **k: [],
        get_execution=lambda execution_id: None,
        resolve_cron_scheduler=lambda: _InProcessCronScheduler(),
        InProcessCronScheduler=_InProcessCronScheduler,
        # hermes_state / api_server
        SessionDB=_SessionDB,
        MAX_REQUEST_BYTES=10_000_000,
    )


@contextlib.contextmanager
def _noop_context():
    yield

@pytest.fixture(autouse=True)
def _isolated_user_home(tmp_path, monkeypatch):
    """유닛 파일 탐색이 **실제 홈을 절대 보지 않게** 한다.

    없으면 테스트 결과가 실행하는 사람의 머신에 달린다 — 실제로
    `~/Library/LaunchAgents/ai.hermes.gateway-noah.plist` 가 있는 Mac 에서
    삭제 테스트가 409 로 떨어졌다. 가드가 제대로 문 것이지만, 테스트가
    환경에 좌우되면 회귀를 잡는 그물이 못 된다.
    """
    from deskrpg_plugin import safedelete

    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setattr(safedelete, "user_home", lambda: home)
    return home


# ---------------------------------------------------------------------------
# 통합 테스트 훅 (T6) — `HERMES_INTEGRATION_REQUIRED=1` 이면 skip 을 실패로 바꾼다.
#
# CI 의 integration 잡은 Hermes 를 설치한 뒤 `-m integration` 으로 돈다. 설치가 조용히 실패해 전부
# skip 되면 잡이 초록으로 끝나는데, 그건 "통과" 가 아니라 "안 돌았다" 다(spec T3). 수집 단계의 skip
# (`pytest.importorskip("hermes_cli")`)과 실행 단계의 skip 을 둘 다 잡는다.
# ---------------------------------------------------------------------------

import os as _os


def _integration_required() -> bool:
    return _os.environ.get("HERMES_INTEGRATION_REQUIRED", "").strip() in ("1", "true", "yes")


def _fail_skipped_report(report, what: str) -> None:
    if report.skipped and _integration_required():
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        report.outcome = "failed"
        report.longrepr = (
            f"HERMES_INTEGRATION_REQUIRED=1 인데 {what} 이(가) skip 됐다 — Hermes 가 설치되지 않았거나 "
            f"importorskip 이 걸렸다: {reason}"
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    _fail_skipped_report(outcome.get_result(), f"수집 {collector.nodeid or collector.name}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    _fail_skipped_report(outcome.get_result(), item.nodeid)


# ---------------------------------------------------------------------------
# 0.10.0 — 도구별 프로바이더 가짜. 실제 TOOL_CATEGORIES 의 행 모양(hermes_cli/tools_config.py:222)을 따른다.
# ---------------------------------------------------------------------------

FAKE_TOOL_CATEGORIES = {
    "tts": {
        "name": "Text-to-Speech",
        "providers": [
            {"name": "Microsoft Edge TTS", "badge": "★ recommended · free", "tag": "no key", "env_vars": [],
             "tts_provider": "edge"},
            {"name": "OpenAI TTS", "badge": "paid", "tag": "voices",
             "env_vars": [{"key": "VOICE_TOOLS_OPENAI_KEY", "prompt": "OpenAI API key",
                           "url": "https://platform.openai.com/api-keys"}],
             "tts_provider": "openai"},
            {"name": "Piper", "badge": "local · free", "tag": "local", "env_vars": [], "tts_provider": "piper",
             "post_setup": "piper"},
            {"name": "Nous Subscription", "badge": "subscription", "tag": "managed", "env_vars": [],
             "tts_provider": "openai", "requires_nous_auth": True, "managed_nous_feature": "tts"},
        ],
    },
}


def _fake_visible_providers(cat, config, *, force_fresh=False, features=None):
    return list(cat.get("providers", []))


def _fake_readiness(provider, config, *, features=None, is_active=None):
    if provider.get("env_vars"):
        return "needs_keys"  # 가짜는 일부러 프로세스 기준으로 틀리게 답한다 — 플러그인은 .env 로 판정해야 한다
    if provider.get("managed_nous_feature") or provider.get("requires_nous_auth"):
        return "needs_auth"
    if provider.get("post_setup"):
        return "needs_setup"
    return "ready"


def _fake_is_active(provider, config, *, force_fresh=False):
    section = config.get("tts") if isinstance(config.get("tts"), dict) else {}
    return bool(provider.get("tts_provider")) and section.get("provider") == provider["tts_provider"] \
        and not provider.get("managed_nous_feature")


def _fake_apply_provider_selection(ts_key, provider_name, config):
    cat = FAKE_TOOL_CATEGORIES.get(ts_key)
    if cat is None:
        raise KeyError(f"Toolset has no configurable category: {ts_key}")
    row = next((p for p in cat["providers"] if p["name"] == provider_name), None)
    if row is None:
        raise KeyError(f"Unknown provider {provider_name!r} for toolset {ts_key!r}")
    config.setdefault("tts", {})["provider"] = row["tts_provider"]


@pytest.fixture
def tmp_api(tmp_path, monkeypatch):
    """카드 제안 저장소·도구가 쓰는 최소 Hermes API.

    `events()` 는 진짜 사건 tail(`events.read_artifact_events`)을 통과시킨다 — 페이로드에서
    `profile` 을 끌어올리는 규칙까지 실제 경로로 확인하려는 것이다. `kanban_writes()` 는
    이 플러그인이 칸반 정본에 쓰지 않았음을 확인하는 데 쓴다 — 칸반 DB 를 열기만 해도 기록된다.
    """
    monkeypatch.delenv("HERMES_DESKRPG_ARTIFACTS_ROOT", raising=False)
    home = tmp_path / "profiles" / "noah"
    home.mkdir(parents=True)
    kanban = tmp_path / "kanban"
    kanban.mkdir()
    writes = []

    class _KanbanDB:
        def __init__(self, *a, **k):
            writes.append(("KanbanDB", a, k))

    api = types.SimpleNamespace(
        get_hermes_home=lambda: str(home),
        kanban_home=lambda: (writes.append(("kanban_home", (), {})), str(kanban))[1],
        get_current_board=lambda: "dev",
        KanbanDB=_KanbanDB,
        MAX_REQUEST_BYTES=50 * 1024 * 1024,
    )
    api.kanban_writes = lambda: list(writes)

    def _events():
        from deskrpg_plugin import events as events_module

        return events_module.read_artifact_events(api, 0, 100)

    api.events = _events
    return api


# --- 공용 저장소 설정 가드 -------------------------------------------------
#
# `GIT_DIR`+`GIT_WORK_TREE` 가 걸린 채(훅 환경, 또는 그것을 흉내 낸 실행) `git init` 을 부르면
# 새 저장소가 생기는 대신 **물려받은 저장소의 공용 `.git/config`** 에 `core.worktree` 가 쓰인다.
# 그 뒤 공용 체크아웃의 모든 git 명령이 조용히 남의 디렉터리를 본다 — `git status` 가 그럴듯한
# diff 를 보여 주고, 거기서 `git add -A` 를 하면 남의 내용이 커밋된다. 증상이 없는 것이 이 결함의
# 핵심이라 사람이 알아차릴 길이 없다(2026-09-21 실측: 공용 config 에 실제로 박혔다).
#
# `core.worktree` 는 워크트리별 설정이 아니라 공용 config 에 들어가므로, 한 번 박히면 그 저장소를
# 쓰는 모든 세션이 영향을 받는다. 그래서 **고치지 않고 실패시킨다** — 조용히 지우면 어느 테스트가
# 박았는지 다음번에 또 모른다.


def _core_worktree() -> str | None:
    """공용 설정의 `core.worktree` 값. 없으면 None.

    설정 **파일의 글자**를 보지 않는다 — `worktree` 라는 낱말은 브랜치 이름 등
    엉뚱한 곳에도 나온다(실측: `[branch "fix/worktree-config-guard"]` 가 걸렸다).
    git 에게 그 키 하나만 묻는다. 환경변수는 넘기지 않는다 — 이 가드가 잡으려는
    그 환경이 답을 바꾸면 안 된다.
    """
    import subprocess

    clean = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    try:
        done = subprocess.run(
            ["git", "config", "--get", "core.worktree"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            env=clean,
            check=False,
        )
    except OSError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def worktree_newly_set(before: str | None, after: str | None) -> bool:
    """스위트가 도는 동안 `core.worktree` 가 새로 생기거나 바뀌었는가.

    이미 있던 값(누군가 의도적으로 둔 것)은 이 스위트의 잘못이 아니다. 값이 **바뀐** 것은
    잡는다 — 다른 워크트리를 가리키게 덮어쓰는 것도 같은 사고다.
    """
    if after is None:
        return False
    return before != after


@pytest.fixture(scope="session", autouse=True)
def 공용_config_에_core_worktree_가_생기지_않는다():
    before = _core_worktree()
    yield
    after = _core_worktree()
    if worktree_newly_set(before, after):
        pytest.fail(
            f"이 스위트가 공용 git 설정의 `core.worktree` 를 바꿨다: {before!r} → {after!r}\n"
            "git 을 부르는 픽스처가 `GIT_DIR`·`GIT_WORK_TREE` 를 끊지 않은 것이다. "
            "지우기 전에 어느 테스트가 그랬는지 먼저 찾아라 — "
            "`git config --unset core.worktree` 로 복구한다."
        )

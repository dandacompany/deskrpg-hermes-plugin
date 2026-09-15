"""인메모리 가짜 크론 — `cron.jobs`·`cron.executions`·`cron.blueprint_catalog`·`hermes_state.SessionDB`.

Hermes 함수와 **같은 시그니처·같은 실패 방식**을 흉내 낸다: `resume_job`/`trigger_job` 의
ValueError, `trigger_job` 이 일시정지를 풀어 버리는 부수효과, `update_job` 이 없는 잡에 None 을
돌려주는 것, 스케줄 문법 오류의 ValueError, 그리고 무엇보다 **저장소가 홈(프로필)별로 따로**라는
사실이다 — `use_cron_store(home)` 스코프 밖에서 부르면 AssertionError 로 K1 위반을 잡는다.

설치: `install_fake_cron(fake_api, tmp_path)` — `fake_api` 의 크론 심볼을 이 인스턴스의 메서드로
바꿔치기하고 인스턴스를 돌려준다. 테스트는 `store.add_job(home, …)` 등으로 상태를 심는다.
"""

import contextlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class FakeCronSchedulerRegistrationError(RuntimeError):
    """`cron.scheduler.CronSchedulerRegistrationError` — 잡은 저장됐고 외부 등록만 실패."""

    def __init__(self, job, cause):
        super().__init__(f"scheduler registration failed: {cause}")
        self.job = job
        self.cause = cause


class FakeBlueprintFillError(ValueError):
    """`cron.blueprint_catalog.BlueprintFillError`."""


class FakeInProcessCronScheduler:
    """내장 프로바이더. `on_jobs_changed` 호출 횟수를 센다."""

    name = "builtin"

    def __init__(self):
        self.on_jobs_changed_calls = 0

    def on_jobs_changed(self):
        self.on_jobs_changed_calls += 1


class FakeExternalCronScheduler:
    """외부 프로바이더 — `InProcessCronScheduler` 의 인스턴스가 **아니다**."""

    name = "external"

    def __init__(self):
        self.on_jobs_changed_calls = 0

    def on_jobs_changed(self):
        self.on_jobs_changed_calls += 1


@dataclass(frozen=True)
class FakeSlot:
    name: str
    type: str
    label: str
    default: Any = None
    options: tuple = ()
    optional: bool = False
    strict: bool = True
    help: str = ""


@dataclass(frozen=True)
class FakeBlueprint:
    key: str
    title: str
    description: str
    category: str
    schedule_template: str
    prompt_template: str
    slots: tuple = ()
    skills: tuple = ()
    tags: tuple = ()
    deliver_default: str = "local"


DEFAULT_CATALOG = (
    FakeBlueprint(
        key="morning-brief",
        title="Morning briefing",
        description="아침 브리핑",
        category="daily",
        schedule_template="{minute} {hour} * * *",
        prompt_template="Brief me at {time}.",
        slots=(
            FakeSlot(name="time", type="time", label="Time", default="08:00"),
            FakeSlot(name="deliver", type="enum", label="Deliver", default="local", options=("origin", "local"), strict=False),
        ),
        skills=("google-workspace",),
        tags=("daily",),
    ),
    FakeBlueprint(
        key="mood",
        title="Mood check",
        description="기분 고르기",
        category="fun",
        schedule_template="0 9 * * *",
        prompt_template="Mood: {mood}",
        slots=(FakeSlot(name="mood", type="enum", label="Mood", default=None, options=("happy", "sad")),),
    ),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _parse_schedule(schedule) -> dict:
    """진짜 `parse_schedule` 의 축소판. `every N m|h`, 5-필드 cron, `once <ISO>` 만 받는다."""
    text = str(schedule or "").strip()
    if not text:
        raise ValueError("Invalid schedule: empty")
    parts = text.split()
    if parts[0] == "every" and len(parts) == 3 and parts[1].isdigit():
        unit = parts[2]
        if unit in ("m", "min", "minutes"):
            minutes = int(parts[1])
        elif unit in ("h", "hours"):
            minutes = int(parts[1]) * 60
        else:
            raise ValueError(f"Invalid schedule: {text}")
        return {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"}
    if parts[0] == "once" and len(parts) == 2:
        try:
            datetime.fromisoformat(parts[1])
        except ValueError as exc:
            raise ValueError(f"Invalid schedule: {text}") from exc
        return {"kind": "once", "run_at": parts[1], "display": f"once at {parts[1]}"}
    if len(parts) == 5:
        return {"kind": "cron", "expr": text, "display": text}
    raise ValueError(f"Invalid schedule: {text}")


def _effective_job_state(job) -> str:
    """`cron.jobs.effective_job_state` 그대로."""
    stored = str(job.get("state") or "").strip()
    if stored in {"completed", "error"}:
        return stored
    if not job.get("enabled", True):
        if job.get("paused_at") or stored == "paused":
            return "paused"
        return stored or "paused"
    if stored == "paused" or job.get("paused_at"):
        return "scheduled"
    return stored or "scheduled"


class FakeSessionDB:
    """`hermes_state.SessionDB` 의 `list_cron_job_runs`/`get_messages`/`close` 만."""

    def __init__(self, store, db_path, read_only=False):
        self.store = store
        self.db_path = Path(db_path)
        self.read_only = read_only
        self.closed = False
        store.opened_session_dbs.append(self)

    def list_cron_job_runs(self, job_id, limit=20, offset=0):
        prefix = f"cron_{job_id}_"
        rows = [
            dict(s)
            for s in self.store.sessions_by_db.get(self.db_path, [])
            if s.get("source") == "cron" and str(s.get("id", "")).startswith(prefix)
        ]
        rows.sort(key=lambda s: (s.get("started_at") or 0, s.get("id")), reverse=True)
        return rows[offset : offset + limit]

    def get_messages(self, session_id, **_kwargs):
        return [dict(m) for m in self.store.messages_by_db.get(self.db_path, {}).get(session_id, [])]

    def close(self):
        self.closed = True


class FakeCronStore:
    """홈별 잡·실행 장부, 세션 DB, 카탈로그, 배달처, 프로바이더를 한 곳에."""

    def __init__(self, tmp_path):
        self.tmp_path = Path(tmp_path)
        self.jobs_by_home: dict = {}
        self.executions_by_home: dict = {}
        self.sessions_by_db: dict = {}
        self.messages_by_db: dict = {}
        self.opened_session_dbs: list = []
        self.current_home: Optional[Path] = None
        self.home_override_stack: list = []
        self.scope_log: list = []  # (home, 최초 진입 시점의 override 홈) — K1 검증용
        self.registration_fails = False
        self.delivery_targets: list = []
        self.catalog: list = list(DEFAULT_CATALOG)
        self.scheduler = FakeInProcessCronScheduler()
        self._seq = 0

    # ----- 상태 심기 ----------------------------------------------------------------

    def add_job(self, home, **fields) -> dict:
        home = Path(home)
        self._seq += 1
        schedule = fields.pop("schedule", "every 5 m")
        parsed = _parse_schedule(schedule) if isinstance(schedule, str) else dict(schedule)
        job = {
            "id": fields.pop("id", f"job{self._seq:03d}"),
            "name": fields.pop("name", f"job {self._seq}"),
            "prompt": fields.pop("prompt", "do the thing"),
            "schedule": parsed,
            "schedule_display": parsed.get("display", ""),
            "repeat": None,
            "enabled": True,
            "state": "scheduled",
            "next_run_at": _now_iso(),
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "deliver": "local",
            "skills": [],
            "model": None,
            "provider": None,
            "created_at": _now_iso(),
            "origin": {"source": "test"},
        }
        job.update(fields)
        self.jobs_by_home.setdefault(home, []).append(job)
        return dict(job)

    def add_execution(self, home, **fields) -> dict:
        """실행 장부 행. `<home>/cron/executions.db` 파일도 만들어 둔다(존재 검사가 경로로 된다)."""
        home = Path(home)
        self._seq += 1
        ledger = home / "cron" / "executions.db"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.touch()
        row = {
            "id": f"exec{self._seq:03d}",
            "job_id": "job001",
            "source": "scheduler",
            "status": "completed",
            "claimed_at": _now_iso(),
            "started_at": _now_iso(),
            "finished_at": None,
            "error": None,
        }
        row.update(fields)
        self.executions_by_home.setdefault(home, []).append(row)
        return dict(row)

    def add_session(self, home, **fields) -> dict:
        """세션 행. `<home>/state.db` 파일도 만든다."""
        home = Path(home)
        db_path = home / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.touch()
        row = {
            "id": fields.get("id", f"cron_job001_{int(time.time())}"),
            "source": "cron",
            "started_at": time.time(),
            "ended_at": None,
            "last_active": None,
            "title": None,
            "preview": "",
        }
        row.update(fields)
        if row["last_active"] is None:
            row["last_active"] = row["started_at"]
        self.sessions_by_db.setdefault(db_path, []).append(row)
        return dict(row)

    def add_message(self, home, session_id, role, content):
        db_path = Path(home) / "state.db"
        msgs = self.messages_by_db.setdefault(db_path, {}).setdefault(session_id, [])
        msgs.append({"id": len(msgs) + 1, "session_id": session_id, "role": role, "content": content})

    def write_output_file(self, home, job_id, stamp: datetime, text: str) -> Path:
        out = Path(home) / "cron" / "output" / job_id / (stamp.strftime("%Y-%m-%d_%H-%M-%S") + ".md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return out

    # ----- 스코프 (hermes_constants / cron.jobs.use_cron_store) --------------------------

    def set_hermes_home_override(self, path):
        self.home_override_stack.append(Path(path))
        return len(self.home_override_stack)

    def reset_hermes_home_override(self, token):
        assert token == len(self.home_override_stack), "홈 오버라이드가 순서대로 풀리지 않았다"
        self.home_override_stack.pop()

    @contextlib.contextmanager
    def use_cron_store(self, home):
        previous = self.current_home
        self.current_home = Path(home)
        self.scope_log.append((self.current_home, self.home_override_stack[-1] if self.home_override_stack else None))
        try:
            yield
        finally:
            self.current_home = previous

    def _jobs(self) -> list:
        assert self.current_home is not None, "use_cron_store 스코프 밖에서 크론 저장소를 건드렸다 (K1)"
        return self.jobs_by_home.setdefault(self.current_home, [])

    def _find(self, job_id):
        return next((j for j in self._jobs() if j["id"] == job_id), None)

    # ----- cron.jobs ------------------------------------------------------------------

    def list_jobs(self, include_disabled=False):
        jobs = [dict(j) for j in self._jobs()]
        if not include_disabled:
            jobs = [j for j in jobs if j.get("enabled", True)]
        return jobs

    def get_job(self, job_id):
        job = self._find(job_id)
        return dict(job) if job else None

    def update_job(self, job_id, updates):
        if "id" in (updates or {}):
            raise ValueError("Cron job field(s) cannot be updated: id")
        job = self._find(job_id)
        if job is None:
            return None
        updates = dict(updates or {})
        if "schedule" in updates and isinstance(updates["schedule"], str):
            parsed = _parse_schedule(updates["schedule"])
            updates["schedule"] = parsed
            updates["schedule_display"] = parsed.get("display", "")
        job.update(updates)
        return dict(job)

    def pause_job(self, job_id, reason=None):
        job = self._find(job_id)
        if job is None:
            return None
        return self.update_job(job_id, {"enabled": False, "state": "paused", "paused_at": _now_iso(), "paused_reason": reason})

    def resume_job(self, job_id):
        job = self._find(job_id)
        if job is None:
            return None
        if job["schedule"].get("kind") == "once" and job.get("past_oneshot"):
            raise ValueError("Cannot resume: one-shot time is in the past and will never fire.")
        return self.update_job(job_id, {"enabled": True, "state": "scheduled", "paused_at": None, "paused_reason": None})

    def trigger_job(self, job_id, extra_prompt=None):
        job = self._find(job_id)
        if job is None:
            return None
        if job.get("state") in {"completed", "error"}:
            raise ValueError(f"Cannot run: job '{job.get('name')}' is {job.get('state')} (terminal).")
        now = _now_iso()
        # 진짜 trigger_job 처럼 일시정지를 풀어 버린다 — 라우트가 먼저 막아야 하는 이유.
        return self.update_job(job_id, {"enabled": True, "state": "scheduled", "paused_at": None, "next_run_at": now, "manual_run_at": now})

    def remove_job(self, job_id):
        if "/" in str(job_id):
            raise ValueError(f"Invalid cron job id for output path: {job_id!r}")
        jobs = self._jobs()
        before = len(jobs)
        jobs[:] = [j for j in jobs if j["id"] != job_id]
        return len(jobs) != before

    def effective_job_state(self, job):
        return _effective_job_state(job)

    def get_cron_output_dir(self):
        assert self.current_home is not None, "use_cron_store 스코프 밖에서 출력 폴더를 물었다 (K1)"
        return self.current_home / "cron" / "output"

    # ----- cron.scheduler -------------------------------------------------------------

    def create_job_with_scheduler_registration(self, **kwargs):
        paused = kwargs.get("paused", False)
        if not isinstance(paused, bool):
            raise ValueError("paused must be a boolean.")
        parsed = _parse_schedule(kwargs.get("schedule"))
        prompt = str(kwargs.get("prompt") or "").strip()
        if not prompt and not kwargs.get("script") and not kwargs.get("skills"):
            raise ValueError("Cron job needs a prompt, script, or skill.")
        repeat = kwargs.get("repeat")
        if parsed["kind"] == "once" and repeat is None:
            repeat = 1
        self._seq += 1
        job = {
            "id": f"job{self._seq:03d}",
            "name": kwargs.get("name") or prompt[:50] or "cron job",
            "prompt": prompt,
            "schedule": parsed,
            "schedule_display": parsed.get("display", ""),
            "repeat": repeat,
            "enabled": not paused,
            "state": "paused" if paused else "scheduled",
            "next_run_at": None if paused else _now_iso(),
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "deliver": kwargs.get("deliver") or "local",
            "skills": list(kwargs.get("skills") or []),
            "model": kwargs.get("model"),
            "provider": kwargs.get("provider"),
            "created_at": _now_iso(),
            "origin": kwargs.get("origin"),
            "script": kwargs.get("script"),
            "paused_at": _now_iso() if paused else None,
        }
        self._jobs().append(job)
        if self.registration_fails and not paused:
            raise FakeCronSchedulerRegistrationError(dict(job), RuntimeError("remote scheduler down"))
        return dict(job)

    # ----- cron.executions ------------------------------------------------------------

    def _executions(self) -> list:
        assert self.current_home is not None, "use_cron_store 스코프 밖에서 실행 장부를 읽었다 (K1)"
        return self.executions_by_home.setdefault(self.current_home, [])

    def list_executions(self, *, job_id=None, limit=50, before_claimed_at=None):
        rows = [dict(r) for r in self._executions()]
        if job_id is not None:
            rows = [r for r in rows if r["job_id"] == str(job_id)]
        if before_claimed_at is not None:
            rows = [r for r in rows if r["claimed_at"] < str(before_claimed_at)]
        rows.sort(key=lambda r: (r["claimed_at"], r["id"]), reverse=True)
        return rows[: max(1, min(int(limit), 500))]

    def get_execution(self, execution_id):
        return next((dict(r) for r in self._executions() if r["id"] == str(execution_id)), None)

    # ----- cron.scheduler_delivery / blueprint_catalog / scheduler_provider -------------

    def cron_delivery_targets(self):
        return [dict(t) for t in self.delivery_targets]

    def get_blueprint(self, key):
        return next((bp for bp in self.catalog if bp.key == key), None)

    def blueprint_catalog_entry(self, bp):
        return {
            "key": bp.key,
            "title": bp.title,
            "description": bp.description,
            "category": bp.category,
            "tags": list(bp.tags),
            "fields": [
                {
                    "name": s.name, "type": s.type, "label": s.label, "default": s.default,
                    "options": list(s.options), "optional": s.optional, "strict": s.strict, "help": s.help,
                }
                for s in bp.slots
            ],
            "schedule": bp.schedule_template,
            "scheduleHuman": "on a schedule",
            "command": f"/blueprint {bp.key}",
            "appUrl": f"hermes://blueprint/{bp.key}",
        }

    def fill_blueprint(self, bp, values, *, origin=None):
        known = {s.name for s in bp.slots}
        unknown = sorted(set(values) - known)
        if unknown:
            raise FakeBlueprintFillError(f"unknown slot: {', '.join(unknown)}")
        resolved = {}
        for s in bp.slots:
            raw = values.get(s.name, s.default)
            if raw in (None, ""):
                if s.optional:
                    continue
                raise FakeBlueprintFillError(f"missing required value: {s.name} ({s.label})")
            if s.type == "enum" and s.strict and s.options and str(raw) not in {str(o) for o in s.options}:
                raise FakeBlueprintFillError(f"{s.name}={raw!r} not allowed")
            resolved[s.name] = raw
        schedule = bp.schedule_template
        if "time" in resolved:
            hour, minute = str(resolved["time"]).split(":")
            schedule = schedule.replace("{hour}", str(int(hour))).replace("{minute}", str(int(minute)))
        spec = {
            "prompt": bp.prompt_template.format(**resolved),
            "schedule": schedule,
            "name": bp.title,
            "deliver": resolved.get("deliver", bp.deliver_default),
        }
        if bp.skills:
            spec["skills"] = list(bp.skills)
        if origin is not None:
            spec["origin"] = origin
        return spec

    def resolve_cron_scheduler(self):
        return self.scheduler


def install_fake_cron(api, tmp_path) -> FakeCronStore:
    """`fake_api` 의 크론 심볼을 이 저장소로 바꿔치기한다."""
    store = FakeCronStore(tmp_path)

    def session_db(db_path=None, read_only=False):
        return FakeSessionDB(store, db_path, read_only=read_only)

    api.__dict__.update(
        set_hermes_home_override=store.set_hermes_home_override,
        reset_hermes_home_override=store.reset_hermes_home_override,
        use_cron_store=store.use_cron_store,
        list_jobs=store.list_jobs,
        get_job=store.get_job,
        update_job=store.update_job,
        pause_job=store.pause_job,
        resume_job=store.resume_job,
        trigger_job=store.trigger_job,
        remove_job=store.remove_job,
        effective_job_state=store.effective_job_state,
        get_cron_output_dir=store.get_cron_output_dir,
        create_job_with_scheduler_registration=store.create_job_with_scheduler_registration,
        CronSchedulerRegistrationError=FakeCronSchedulerRegistrationError,
        cron_delivery_targets=store.cron_delivery_targets,
        CATALOG=store.catalog,
        get_blueprint=store.get_blueprint,
        blueprint_catalog_entry=store.blueprint_catalog_entry,
        fill_blueprint=store.fill_blueprint,
        BlueprintFillError=FakeBlueprintFillError,
        list_executions=store.list_executions,
        get_execution=store.get_execution,
        resolve_cron_scheduler=store.resolve_cron_scheduler,
        InProcessCronScheduler=FakeInProcessCronScheduler,
        SessionDB=session_db,
    )
    return store


# ---------------------------------------------------------------------------
# 테스트용 라우트 마운트 — routes.py 의 테이블은 다른 태스크가 채운다. 여기서는 T4 핸들러만
# 같은 경로 모양으로 붙여 핸들러 자체를 검증한다.
# ---------------------------------------------------------------------------

CRON_ROUTES = (
    ("GET", "/p/{profile}/deskrpg/cron/jobs", "list_jobs_handler"),
    ("POST", "/p/{profile}/deskrpg/cron/jobs", "create_job_handler"),
    ("GET", "/p/{profile}/deskrpg/cron/jobs/{id}", "get_job_handler"),
    ("PUT", "/p/{profile}/deskrpg/cron/jobs/{id}", "update_job_handler"),
    ("DELETE", "/p/{profile}/deskrpg/cron/jobs/{id}", "delete_job_handler"),
    ("GET", "/p/{profile}/deskrpg/cron/jobs/{id}/runs", "list_runs_handler"),
    ("POST", "/p/{profile}/deskrpg/cron/jobs/{id}/pause", "pause_handler"),
    ("POST", "/p/{profile}/deskrpg/cron/jobs/{id}/resume", "resume_handler"),
    ("POST", "/p/{profile}/deskrpg/cron/jobs/{id}/run", "run_handler"),
    ("GET", "/p/{profile}/deskrpg/cron/delivery-targets", "delivery_targets_handler"),
    ("GET", "/p/{profile}/deskrpg/cron/blueprints", "blueprints_handler"),
    ("POST", "/p/{profile}/deskrpg/cron/blueprints/instantiate", "instantiate_blueprint_handler"),
)


def mount_cron_routes(app, adapter, api):
    from deskrpg_plugin import cron
    from deskrpg_plugin.auth import Scope, require_auth

    for method, path, factory in CRON_ROUTES:
        app.router.add_route(method, path, require_auth(adapter, Scope.PROFILE, getattr(cron, factory)(api)))

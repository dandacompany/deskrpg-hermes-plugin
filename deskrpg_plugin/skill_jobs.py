"""스킬 설치·업데이트·curator 실행 작업 표 — Hermes REST 의 `spawn_profile_action` 과 같은 하위 프로세스 방식.

- 명령: `<python> -m hermes_cli.main -p <profile> <argv…>`. 실행 파일은 Hermes 의 `_dashboard_spawn_executable()`.
- 환경: Hermes 의 `_profile_action_environment` — 게이트웨이(default) 프로필의 자격증명을 자식에게 넘기지 않는다.
- 작업 표는 **프로세스 메모리**다. 게이트웨이가 재시작되면 사라지고, 그때 모르는 jobId 는 404 `job_unknown`.
- 한 프로필에서 동시에 하나만(409 `job_busy`) — 설치와 curator 가 같은 스킬 폴더를 동시에 바꾸지 않게.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time

from .common import RequestError, log_event

TAIL_BYTES = 4096
DONE_TTL_SECONDS = 3600
# Hermes `skills install`/`uninstall` 은 차단·가져오기 실패에도 종료 코드 0 으로 끝난다(`do_install` 이 return 만 한다).
# 그래서 작업마다 결과 확인 함수(`verify`)를 받아, 0 으로 끝나도 확인이 거짓이면 실패로 적는다.
UNVERIFIED_NOTE = "\n[deskrpg] command exited 0 but the expected change is not present"

_SECRET_PATTERNS = (
    (re.compile(r"(sk-[A-Za-z0-9_-]{12,})"), "***"),
    (re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,})"), "***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD))=\S+"), r"\1=***"),
)


def mask_secrets(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _tail(out: bytes) -> str:
    """끝 4096 바이트를 마스킹해 돌려준다. 잘린 첫 글자가 대체 문자(3바이트)로 늘어나도 한도를 넘지 않게 다시 깎는다."""
    text = mask_secrets(out[-TAIL_BYTES:].decode("utf-8", errors="replace"))
    while len(text.encode("utf-8")) > TAIL_BYTES:
        text = text[1:]
    return text


async def _default_spawn(*cmd, env=None):
    return await asyncio.create_subprocess_exec(
        *cmd, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)


class JobTable:
    def __init__(self, spawn=None, clock=time.monotonic):
        self._spawn = spawn or _default_spawn
        self._clock = clock
        self._jobs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def _gc(self) -> None:
        now = self._clock()
        for jid in [j for j, v in self._jobs.items() if v["doneAt"] is not None and now - v["doneAt"] > DONE_TTL_SECONDS]:
            self._jobs.pop(jid, None)
            self._tasks.pop(jid, None)

    def _busy(self, profile: str) -> bool:
        return any(v["profile"] == profile and v["state"] == "running" for v in self._jobs.values())

    def start(self, api, profile: str, kind: str, argv: list[str], verify=None) -> str:
        """`verify` 는 끝난 뒤 워커 스레드에서 부르는 동기 함수(`() -> bool`). 거짓이면 종료 코드 0 이어도 failed."""
        self._gc()
        if self._busy(profile):
            raise RequestError(409, "job_busy", profile)
        sub = ["-p", profile, *argv]
        # 환경·명령을 먼저 만든다 — 여기서 실패하면 작업을 등록하지 않아 프로필이 "진행 중" 에 갇히지 않는다.
        env = api._profile_action_environment(sub)
        cmd = [api._dashboard_spawn_executable(), "-m", "hermes_cli.main", *sub]
        job_id = secrets.token_hex(8)
        self._jobs[job_id] = {"jobId": job_id, "profile": profile, "kind": kind, "state": "running",
                              "exitCode": None, "outputTail": "", "doneAt": None}
        self._tasks[job_id] = asyncio.get_running_loop().create_task(self._run(job_id, cmd, env, verify))
        log_event("skill_job_start", profile=profile, kind=kind, job_id=job_id)
        return job_id

    async def _run(self, job_id: str, cmd: list[str], env: dict, verify=None) -> None:
        job = self._jobs[job_id]
        try:
            proc = await self._spawn(*cmd, env=env)
            out, _ = await proc.communicate()
            code = proc.returncode
        except Exception as exc:  # noqa: BLE001 — 실행 실패도 작업 결과로 남긴다
            out, code = f"spawn failed: {type(exc).__name__}".encode(), -1
        ok = code == 0
        if ok and verify is not None:
            try:
                ok = bool(await asyncio.to_thread(verify))
            except Exception:  # noqa: BLE001 — 확인하지 못한 것은 성공으로 적지 않는다
                ok = False
            if not ok:
                out = (out or b"") + UNVERIFIED_NOTE.encode()
        job.update(state="succeeded" if ok else "failed", exitCode=code,
                   outputTail=_tail(out or b""), doneAt=self._clock())
        log_event("skill_job_done", profile=job["profile"], kind=job["kind"], job_id=job_id, status=job["state"])

    async def wait(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task is not None:
            await task

    def get(self, profile: str, job_id: str) -> dict:
        self._gc()
        job = self._jobs.get(job_id)
        if job is None or job["profile"] != profile:
            raise RequestError(404, "job_unknown", job_id)
        return {k: job[k] for k in ("jobId", "kind", "state", "exitCode", "outputTail")}


TABLE = JobTable()

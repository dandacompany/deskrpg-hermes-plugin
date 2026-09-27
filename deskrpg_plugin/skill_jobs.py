"""Skill jobs — hub install/uninstall/update and curator runs — as the documented Hermes CLI in a child process.

- Command: `<hermes> -p <profile> skills|curator …` (the CLI documented in Hermes' `cli-commands.md` and
  `curator.md`). `<hermes>` is resolved by the plugin, never through a Hermes internal: `DESKRPG_HERMES_BIN` or
  `HERMES_BIN` when set, else `hermes` on PATH, else this interpreter with `-m hermes_cli.main` (the gateway's own
  install, whose entry point loads Hermes' package-manager bootstrap).
- Environment: a copy of the gateway's with every secret removed — the gateway (default) profile's `.env` keys and
  anything named like a key, token, secret or password — so the child only has what the target profile's own home
  gives it. `HERMES_HOME` points at the target profile, `HERMES_NONINTERACTIVE=1`.
- These commands have no JSON output; the result is the exit code, the masked output tail, and a `verify` check
  (Hermes `skills install`/`uninstall` exit 0 even when blocked).
- A job past `JOB_TIMEOUT_SECONDS` is killed with its process group and reported failed.
- The job table is process memory: after a gateway restart an unknown jobId is 404 `job_unknown`.
- One job per profile at a time (409 `job_busy`), so an install and a curator run never change the same skill
  folder at once.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import signal
import sys
import time
from pathlib import Path

from .common import RequestError, log_event
from .envfile import read_assignments

TAIL_BYTES = 4096
JOB_TIMEOUT_SECONDS = 900
KILL_GRACE_SECONDS = 5
TIMEOUT_NOTE = "\n[deskrpg] timed out after {seconds}s; the process group was stopped"
HERMES_BIN_ENVS = ("DESKRPG_HERMES_BIN", "HERMES_BIN")
# Variables a child must never inherit from the gateway, whatever the gateway's .env says.
_SECRET_NAME = re.compile(r"(?i)(^|_)(API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|COOKIE)S?$")
_ALWAYS_DROP = frozenset({"API_SERVER_KEY", "_HERMES_GATEWAY", "HERMES_DASHBOARD_SESSION_TOKEN"})
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


def hermes_command() -> list[str]:
    """How to run the Hermes CLI from here. See the module docstring for the order."""
    for name in HERMES_BIN_ENVS:
        value = (os.environ.get(name) or "").strip()
        if value and os.access(value, os.X_OK) and Path(value).is_file():
            return [value]
    found = shutil.which("hermes")
    if found:
        return [found]
    return [sys.executable, "-m", "hermes_cli.main"]


def child_environment(api, profile: str) -> dict:
    """The gateway's environment minus its secrets, pointed at the target profile's home."""
    env = dict(os.environ)
    gateway_env_keys = set()
    try:
        gateway_env_keys = set(read_assignments(Path(api.get_hermes_home()) / ".env"))
    except Exception:  # noqa: BLE001 — without the file, the name rules below still apply
        pass
    for name in list(env):
        if name in _ALWAYS_DROP or name in gateway_env_keys or _SECRET_NAME.search(name):
            env.pop(name, None)
    env["HERMES_HOME"] = str(api.get_profile_dir(profile))
    env["HERMES_NONINTERACTIVE"] = "1"
    return env


async def _default_spawn(*cmd, env=None):
    return await asyncio.create_subprocess_exec(
        *cmd, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)


async def _stop(proc) -> tuple[bytes, int]:
    """Stop a timed-out child and everything it started (it runs in its own session)."""
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_SECONDS), (signal.SIGKILL, None)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, AttributeError):
            pass
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=wait)
            return out or b"", proc.returncode if proc.returncode is not None else -1
        except asyncio.TimeoutError:
            continue
    return b"", -1


class JobTable:
    def __init__(self, spawn=None, clock=time.monotonic, timeout=JOB_TIMEOUT_SECONDS):
        self._spawn = spawn or _default_spawn
        self._timeout = timeout
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
        # Build the command and environment first: if that fails no job is registered, so the profile is not left
        # stuck "busy".
        env = child_environment(api, profile)
        cmd = [*hermes_command(), "-p", profile, *argv]
        job_id = secrets.token_hex(8)
        self._jobs[job_id] = {"jobId": job_id, "profile": profile, "kind": kind, "state": "running",
                              "exitCode": None, "outputTail": "", "doneAt": None}
        self._tasks[job_id] = asyncio.get_running_loop().create_task(self._run(job_id, cmd, env, verify))
        log_event("skill_job_start", profile=profile, kind=kind, job_id=job_id)
        return job_id

    async def _run(self, job_id: str, cmd: list[str], env: dict, verify=None) -> None:
        job = self._jobs[job_id]
        timed_out = False
        try:
            proc = await self._spawn(*cmd, env=env)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
                code = proc.returncode
            except asyncio.TimeoutError:
                timed_out = True
                out, code = await _stop(proc)
        except Exception as exc:  # noqa: BLE001 — a failure to start is a job result too
            out, code = f"spawn failed: {type(exc).__name__}".encode(), -1
        if timed_out:
            out = (out or b"") + TIMEOUT_NOTE.format(seconds=self._timeout).encode()
        ok = code == 0 and not timed_out
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

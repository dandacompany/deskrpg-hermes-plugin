import asyncio

import pytest

from deskrpg_plugin.common import RequestError
from deskrpg_plugin.skill_jobs import JobTable, mask_secrets


class FakeProc:
    def __init__(self, out: bytes, code: int, wait_event: asyncio.Event | None = None):
        self._out, self._code, self._ev = out, code, wait_event
        self.returncode = None

    async def communicate(self):
        if self._ev:
            await self._ev.wait()
        self.returncode = self._code
        return self._out, b""


def _spawn_recorder(procs):
    calls = []

    async def spawn(*cmd, env=None, **kw):
        calls.append({"cmd": list(cmd), "env": env})
        return procs.pop(0)

    return spawn, calls


async def test_성공_작업은_끝나면_succeeded_와_출력_끝부분(fake_api):
    spawn, calls = _spawn_recorder([FakeProc(b"installed pdf-extract\n", 0)])
    table = JobTable(spawn=spawn)
    job = table.start(fake_api, "sophie", "hub_install", ["skills", "install", "x", "--yes"])
    await table.wait(job)
    got = table.get("sophie", job)
    assert got["state"] == "succeeded" and got["exitCode"] == 0
    assert "installed pdf-extract" in got["outputTail"]
    assert calls[0]["cmd"] == ["/usr/bin/python3", "-m", "hermes_cli.main", "-p", "sophie",
                               "skills", "install", "x", "--yes"]


async def test_작업_환경은_hermes_의_정리_함수를_거친다(fake_api):
    seen = []
    fake_api._profile_action_environment = lambda sub, env_overrides=None: (seen.append(sub), {"PATH": "/p"})[1]
    spawn, calls = _spawn_recorder([FakeProc(b"", 0)])
    table = JobTable(spawn=spawn)
    job = table.start(fake_api, "sophie", "curator_run", ["curator", "run"])
    await table.wait(job)
    assert seen == [["-p", "sophie", "curator", "run"]]
    assert calls[0]["env"] == {"PATH": "/p"}


async def test_같은_프로필의_두번째_작업은_409(fake_api):
    ev = asyncio.Event()
    spawn, _ = _spawn_recorder([FakeProc(b"", 0, ev), FakeProc(b"", 0)])
    table = JobTable(spawn=spawn)
    first = table.start(fake_api, "sophie", "hub_install", ["skills", "install", "a", "--yes"])
    with pytest.raises(RequestError) as exc:
        table.start(fake_api, "sophie", "curator_run", ["curator", "run"])
    assert exc.value.status == 409 and exc.value.code == "job_busy"
    other = table.start(fake_api, "bob", "hub_install", ["skills", "install", "a", "--yes"])
    ev.set()
    await table.wait(first)
    await table.wait(other)


async def test_실패와_비밀값_마스킹(fake_api):
    out = b"error: OPENAI_API_KEY=sk-abcdef0123456789abcdef token ghp_ABCDEFGHIJKLMNOPQRSTUVWX\n"
    spawn, _ = _spawn_recorder([FakeProc(out, 2)])
    table = JobTable(spawn=spawn)
    job = table.start(fake_api, "sophie", "hub_install", ["skills", "install", "x", "--yes"])
    await table.wait(job)
    got = table.get("sophie", job)
    assert got["state"] == "failed" and got["exitCode"] == 2
    assert "sk-abcdef" not in got["outputTail"] and "ghp_" not in got["outputTail"]


async def test_출력은_끝_4096바이트만(fake_api):
    spawn, _ = _spawn_recorder([FakeProc(b"a" * 10000 + b"END", 0)])
    table = JobTable(spawn=spawn)
    job = table.start(fake_api, "sophie", "hub_install", ["skills", "install", "x", "--yes"])
    await table.wait(job)
    tail = table.get("sophie", job)["outputTail"]
    assert tail.endswith("END") and len(tail.encode()) <= 4096


async def test_모르는_작업과_다른_프로필의_작업은_404(fake_api):
    spawn, _ = _spawn_recorder([FakeProc(b"", 0)])
    table = JobTable(spawn=spawn)
    job = table.start(fake_api, "sophie", "hub_install", ["skills", "install", "x", "--yes"])
    await table.wait(job)
    for prof, jid in (("sophie", "nope"), ("bob", job)):
        with pytest.raises(RequestError) as exc:
            table.get(prof, jid)
        assert exc.value.status == 404 and exc.value.code == "job_unknown"


async def test_끝난_작업은_한시간_뒤_사라진다(fake_api):
    now = [1000.0]
    spawn, _ = _spawn_recorder([FakeProc(b"", 0)])
    table = JobTable(spawn=spawn, clock=lambda: now[0])
    job = table.start(fake_api, "sophie", "hub_install", ["skills", "install", "x", "--yes"])
    await table.wait(job)
    now[0] += 3601
    with pytest.raises(RequestError):
        table.get("sophie", job)


def test_마스킹_규칙():
    assert mask_secrets("key sk-0123456789abcdef0123") == "key ***"
    assert mask_secrets("FOO_API_KEY=abc123") == "FOO_API_KEY=***"
    assert mask_secrets("Bearer abcdefghijklmnop") == "Bearer ***"


async def test_환경_준비가_실패하면_작업을_등록하지_않아_프로필이_잠기지_않는다(fake_api):
    def boom(sub, env_overrides=None):
        raise RuntimeError("no profile")

    fake_api._profile_action_environment = boom
    spawn, calls = _spawn_recorder([FakeProc(b"", 0)])
    table = JobTable(spawn=spawn)
    with pytest.raises(RuntimeError):
        table.start(fake_api, "sophie", "hub_install", ["skills", "install", "x", "--yes"])
    fake_api._profile_action_environment = lambda sub, env_overrides=None: {}
    job = table.start(fake_api, "sophie", "hub_install", ["skills", "install", "x", "--yes"])
    await table.wait(job)
    assert calls and table.get("sophie", job)["state"] == "succeeded"


async def test_멀티바이트_경계에서_잘려도_4096바이트를_넘지_않는다(fake_api):
    spawn, _ = _spawn_recorder([FakeProc("가".encode() * 3000, 0)])
    table = JobTable(spawn=spawn)
    job = table.start(fake_api, "sophie", "hub_install", ["skills", "install", "x", "--yes"])
    await table.wait(job)
    tail = table.get("sophie", job)["outputTail"]
    assert len(tail.encode()) <= 4096 and tail.endswith("가")


async def test_종료_코드가_0이어도_확인이_거짓이면_failed(fake_api):
    spawn, _ = _spawn_recorder([FakeProc(b"Not installed: blocked", 0), FakeProc(b"Installed", 0)])
    table = JobTable(spawn=spawn)
    job = table.start(fake_api, "sophie", "hub_install", ["skills", "install", "x", "--yes"], verify=lambda: False)
    await table.wait(job)
    got = table.get("sophie", job)
    assert got["state"] == "failed" and got["exitCode"] == 0
    assert "Not installed" in got["outputTail"] and "[deskrpg]" in got["outputTail"]
    ok = table.start(fake_api, "sophie", "hub_install", ["skills", "install", "x", "--yes"], verify=lambda: True)
    await table.wait(ok)
    assert table.get("sophie", ok)["state"] == "succeeded"

"""무인 실행 막힘 사건(0.18.0) — 크론·칸반 워커의 도구 결과에서 승인 거부를 찾아 `approval.blocked` 로 남긴다.

Hermes 의 거부 결과는 JSON 문자열이다: 터미널 `{"output":"","exit_code":-1,"error":"BLOCKED: …","status":"blocked"}`
(`tools/terminal_tool.py` `_error_json`), MCP `{"error":"The user did not approve …"}`(`tools/registry.py` `tool_error`).
"""

import json
from pathlib import Path

import pytest

from deskrpg_plugin import approval_blocked as ab
from deskrpg_plugin import contract_fields, events
from tests.fakes_cron import install_fake_cron
from tests.fakes_events import events_client, install_fake_events

CRON_BLOCK = ("BLOCKED: Command flagged as dangerous (recursive delete) but cron jobs run without a user present "
              "to approve it. Find an alternative approach that avoids this command. To allow dangerous commands "
              "in cron jobs, set approvals.cron_mode: approve in config.yaml.")
QUERY_BLOCK = CRON_BLOCK.replace("cron jobs run", "single-query mode (-q) runs").replace(
    "in cron jobs", "in single-query mode").replace("cron_mode", "single_query_mode")
EXEC_BLOCK = ("BLOCKED: execute_code runs arbitrary local Python (including subprocess calls that bypass shell-string "
              "approval checks). Cron jobs run without a user present to approve it. Use normal tools instead, or set "
              "approvals.cron_mode: approve only if this cron profile is intentionally trusted.")
MCP_DENY = ("The user did not approve running write-capable MCP tool 'write_note' on untrusted server 'notes'. "
            "The command was NOT run. Do not retry without explicit user direction.")
MCP_FAIL = "MCP tool 'write_note' on untrusted server 'notes' was blocked: the approval system was unavailable (fail-closed)."


def term(text):
    return json.dumps({"output": "", "exit_code": -1, "error": text, "status": "blocked"})


# ---- 분류(순수 함수) ---------------------------------------------------------------------------------------------


def test_cron_block_from_terminal_json():
    assert ab.classify_blocked("terminal", {"command": "rm -rf x"}, term(CRON_BLOCK)) == {
        "kind": "command", "mode": "cron"}


def test_single_query_block_is_kanban_mode():
    assert ab.classify_blocked("terminal", {}, term(QUERY_BLOCK)) == {"kind": "command", "mode": "single_query"}


def test_execute_code_block():
    assert ab.classify_blocked("execute_code", {"code": "x"}, json.dumps({"error": EXEC_BLOCK})) == {
        "kind": "command", "mode": "cron"}


def test_plain_string_result_is_also_read():
    assert ab.classify_blocked("terminal", {}, CRON_BLOCK)["mode"] == "cron"


def test_mcp_denial_and_fail_closed():
    expected = {"kind": "mcp", "mcpServer": "notes", "mcpTool": "write_note"}
    assert ab.classify_blocked("mcp_notes_write_note", {}, json.dumps({"error": MCP_DENY})) == expected
    assert ab.classify_blocked("mcp_notes_write_note", {}, json.dumps({"error": MCP_FAIL})) == expected


@pytest.mark.parametrize("result", [
    # 다른 이유의 BLOCKED(무인 실행 정책 문구가 없다) — 정책으로 풀 수 없으니 알리지 않는다.
    term("BLOCKED: this command matches a hardline pattern and never runs."),
    term("BLOCKED: approvals.deny matched 'git push --force*'."),
    # 대화 플랫폼의 무인 모드는 DeskRPG 대화 경로 — 이번 범위 밖.
    term(CRON_BLOCK.replace("cron_mode", "unattended_mode")),
    json.dumps({"output": "ok", "exit_code": 0}),
    "plain success text",
    "",
    None,
    {"error": 5},
])
def test_other_results_are_not_blocked(result):
    assert ab.classify_blocked("terminal", {}, result) is None


# ---- 훅 ----------------------------------------------------------------------------------------------------------


@pytest.fixture
def api(tmp_api, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    tmp_api.detect_dangerous_command = lambda cmd: (True, "recursive delete", "recursive delete") \
        if "rm -rf" in cmd else (False, None, None)
    tmp_api.redact_sensitive_text = lambda text, force=False: text.replace("sk-secret", "***")
    ab._SEEN.clear()
    return tmp_api


def _blocked(api):
    return events.read_artifact_events(api, 0, 50, ("approval.blocked",))


def test_kanban_worker_block_becomes_event(api, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "r_9")
    hook = ab.make_hook(api)
    hook(tool_name="terminal", args={"command": "rm -rf /tmp/x --token sk-secret"}, result=term(QUERY_BLOCK),
         task_id="t_1", session_id="s")
    rows = _blocked(api)
    assert len(rows) == 1 and rows[0]["profile"] == "noah"
    payload = rows[0]["payload"]
    assert payload["source"] == "kanban" and payload["taskId"] == "t_1" and payload["runId"] == "r_9"
    assert payload["board"] == "dev" and payload["tool"] == "terminal" and payload["kind"] == "command"
    assert payload["patternKey"] == "recursive delete" and payload["patternDescription"] == "recursive delete"
    assert payload["command"] == "rm -rf /tmp/x --token ***" and "sk-secret" not in json.dumps(payload)
    assert payload["at"].endswith("Z")


def test_cron_block_takes_job_id_from_task_id(api):
    hook = ab.make_hook(api)
    hook(tool_name="terminal", args={"command": "rm -rf /tmp/x"}, result=term(CRON_BLOCK),
         task_id="cron:job42:exec7", session_id="s")
    payload = _blocked(api)[0]["payload"]
    assert payload["source"] == "cron" and payload["jobId"] == "job42" and "taskId" not in payload


def test_cron_block_carries_the_job_name_from_jobs_json(api):
    home = Path(api.get_hermes_home())
    (home / "cron").mkdir(parents=True, exist_ok=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "job42", "name": "야간 정리"}]}))
    ab.make_hook(api)(tool_name="terminal", args={"command": "rm -rf /tmp/x"}, result=term(CRON_BLOCK),
                      task_id="cron:job42:exec7", session_id="s")
    assert _blocked(api)[0]["payload"]["jobName"] == "야간 정리"


def test_cron_job_name_is_omitted_when_unknown(api):
    ab.make_hook(api)(tool_name="terminal", args={"command": "rm -rf /tmp/x"}, result=term(CRON_BLOCK),
                      task_id="cron:missing:exec7", session_id="s")
    assert "jobName" not in _blocked(api)[0]["payload"]


def test_mcp_denial_in_cron_is_recorded_with_server(api):
    ab.make_hook(api)(tool_name="mcp_notes_write_note", args={"text": "hi"}, result=json.dumps({"error": MCP_DENY}),
                      task_id="cron:job42:exec7", session_id="s")
    payload = _blocked(api)[0]["payload"]
    assert payload["kind"] == "mcp" and payload["mcpServer"] == "notes" and payload["mcpTool"] == "write_note"
    assert "command" not in payload and "patternKey" not in payload


def test_mcp_denial_in_chat_is_not_recorded(api):
    # 대화에서 사람이 [거절] 한 것 — 무인 실행 막힘이 아니다.
    ab.make_hook(api)(tool_name="mcp_notes_write_note", args={}, result=json.dumps({"error": MCP_DENY}),
                      task_id=None, session_id="s")
    assert _blocked(api) == []


def test_mode_must_match_context(api):
    # 크론 문구인데 칸반 워커(-q) 맥락이 아니다 → 문구가 맥락을 정한다: 크론 task_id 가 없어도 cron.
    ab.make_hook(api)(tool_name="terminal", args={"command": "rm -rf a"}, result=term(CRON_BLOCK),
                      task_id=None, session_id="s")
    payload = _blocked(api)[0]["payload"]
    assert payload["source"] == "cron" and "jobId" not in payload


def test_same_block_in_one_process_is_recorded_once(api):
    hook = ab.make_hook(api)
    for _ in range(3):
        hook(tool_name="terminal", args={"command": "rm -rf /tmp/x"}, result=term(CRON_BLOCK),
             task_id="cron:job42:exec7", session_id="s")
    hook(tool_name="terminal", args={"command": "rm -rf /tmp/y"}, result=term(CRON_BLOCK),
         task_id="cron:job42:exec7", session_id="s")
    assert len(_blocked(api)) == 1  # 같은 작업·같은 패턴은 한 번이면 충분하다


def test_command_is_truncated(api):
    ab.make_hook(api)(tool_name="terminal", args={"command": "rm -rf " + "a" * 1000}, result=term(CRON_BLOCK),
                      task_id="cron:j:e", session_id="s")
    assert len(_blocked(api)[0]["payload"]["command"]) <= 300


def test_without_redactor_the_command_is_omitted(api):
    del api.redact_sensitive_text
    ab.make_hook(api)(tool_name="terminal", args={"command": "rm -rf x"}, result=term(CRON_BLOCK),
                      task_id="cron:j:e", session_id="s")
    payload = _blocked(api)[0]["payload"]
    assert "command" not in payload and payload["patternKey"] == "recursive delete"


def test_without_detector_there_is_no_pattern(api):
    del api.detect_dangerous_command
    ab.make_hook(api)(tool_name="terminal", args={"command": "rm -rf x"}, result=term(CRON_BLOCK),
                      task_id="cron:j:e", session_id="s")
    assert "patternKey" not in _blocked(api)[0]["payload"]


def test_hook_never_raises(api, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ab, "_record", boom)
    ab.make_hook(api)(tool_name="terminal", args={"command": "rm -rf x"}, result=term(CRON_BLOCK),
                      task_id="cron:j:e", session_id="s")
    ab.make_hook(api)(tool_name=None, args=None, result=object())


def test_success_results_do_not_open_the_registry(api, monkeypatch):
    monkeypatch.setattr(ab, "_record", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened")))
    ab.make_hook(api)(tool_name="terminal", args={"command": "ls"}, result=json.dumps({"output": "a", "exit_code": 0}),
                      task_id="cron:j:e", session_id="s")


# ---- 사건 계약·옵트인 ----------------------------------------------------------------------------------------------


def test_kind_string_is_the_same_everywhere():
    assert ab.EVENT_KIND == "approval.blocked"
    assert events.APPROVAL_EVENT_KINDS == (ab.EVENT_KIND,)
    assert ab.EVENT_KIND in contract_fields.EVENT_KINDS


def test_include_token_is_independent():
    assert events.wants_approvals("approvals") is True
    assert events.wants_approvals("artifacts,card_proposals") is False
    assert events.artifact_kind_filter(include_artifacts=False, include_card_proposals=False,
                                       include_approvals=True) == ("approval.blocked",)
    assert "approval.blocked" not in events.artifact_kind_filter(include_artifacts=True, include_card_proposals=True)


@pytest.fixture
def kanban(fake_api, tmp_path):
    return install_fake_events(fake_api, tmp_path / "kanban")


@pytest.fixture
def cron_store(fake_api, tmp_path):
    return install_fake_cron(fake_api, tmp_path)


async def test_events_endpoint_carries_blocked_only_with_opt_in(aiohttp_client, fake_api, kanban, cron_store):
    ab._SEEN.clear()
    client = await events_client(aiohttp_client, fake_api)
    start = await (await client.get("/deskrpg/events?board=default&include=approvals")).json()
    plain = await (await client.get("/deskrpg/events?board=default&include=artifacts")).json()
    assert events.decode_cursor(start["cursor"])["a"] == 0

    ab.make_hook(fake_api)(tool_name="terminal", args={"command": "rm -rf x"}, result=term(CRON_BLOCK),
                           task_id="cron:job42:exec7", session_id="s")

    body = await (await client.get(f"/deskrpg/events?board=default&include=approvals&cursor={start['cursor']}")).json()
    assert [e["kind"] for e in body["events"]] == ["approval.blocked"]
    assert body["events"][0]["payload"]["jobId"] == "job42"
    other = await (await client.get(f"/deskrpg/events?board=default&include=artifacts&cursor={plain['cursor']}")).json()
    assert other["events"] == []

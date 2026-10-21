"""Shared DeskRPG education-workflow tools stay narrowly scoped and secret-safe."""

import json

import deskrpg_plugin.education_workflows_tool as tool


def test_api_and_public_urls_are_separate(monkeypatch):
    monkeypatch.setenv("DESKRPG_EDUCATION_WORKFLOW_API_URL", "http://deskrpg:3000")
    monkeypatch.setenv("DESKRPG_EDUCATION_WORKFLOW_URL", "https://office.example.com")
    monkeypatch.setenv("DESKRPG_EDUCATION_WORKFLOW_TOKEN", "a" * 64)
    url, token = tool._api_settings()
    assert url == "http://deskrpg:3000/api/internal/education-workflows"
    assert token == "a" * 64
    assert tool._task_link("abc-123") == "https://office.example.com/workflows/education?taskId=abc-123"


def test_internal_hostname_never_becomes_a_slack_link(monkeypatch):
    monkeypatch.delenv("DESKRPG_EDUCATION_WORKFLOW_URL", raising=False)
    assert tool._task_link("abc") is None
    monkeypatch.setenv("DESKRPG_EDUCATION_WORKFLOW_URL", "http://deskrpg:3000")
    assert tool._task_link("abc") is None


def test_create_uses_idempotency_and_redacted_api_errors(monkeypatch):
    monkeypatch.setenv("DESKRPG_EDUCATION_WORKFLOW_API_URL", "http://deskrpg:3000")
    monkeypatch.setenv("DESKRPG_EDUCATION_WORKFLOW_URL", "https://office.example.com")
    monkeypatch.setenv("DESKRPG_EDUCATION_WORKFLOW_TOKEN", "b" * 64)
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size=-1):
            return json.dumps({"workflow": {"id": "workflow-1"}}).encode()

    def fake_open(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(tool.urllib.request, "urlopen", fake_open)
    handler = tool.make_handler(None, "create")
    result = json.loads(handler({"title": "보안 교육", "requestText": "공개 자료 조사"}, profile="training", session_id="thread-1", task_id="task-1"))
    body = json.loads(requests[0][0].data)
    assert requests[0][0].full_url.endswith(tool.API_PATH)
    assert requests[0][0].get_header("Authorization") == f"Bearer {'b' * 64}"
    assert body["idempotencyKey"]
    assert result["deskRpgUrl"] == "https://office.example.com/workflows/education?taskId=workflow-1"


def test_toolset_only_declares_workflow_operations():
    assert tool.TOOLSET == "education_workflow"
    assert {schema["name"] for schema in (tool.LIST_SCHEMA, tool.CREATE_SCHEMA, tool.GET_SCHEMA, tool.UPDATE_SCHEMA)} == {
        "education_workflow_list", "education_workflow_create", "education_workflow_get", "education_workflow_update"
    }
    assert tool.UPDATE_SCHEMA["parameters"]["properties"]["action"]["enum"] == [
        "save_candidates", "choose_topic", "save_brief", "select_team", "start_research", "save_research",
        "approve_research", "save_outline", "approve_outline", "save_cards", "save_document", "approve_final",
        "pause", "resume", "request_revision",
    ]


def test_registers_only_narrow_tools_and_prompt_in_dedicated_toolset():
    import deskrpg_plugin

    calls = []

    class Context:
        def register_tool(self, *args, **kwargs):
            calls.append(("tool", args, kwargs))

        def register_system_prompt_section(self, *args, **kwargs):
            calls.append(("prompt", args, kwargs))

    deskrpg_plugin._register_education_workflows(Context(), object())
    tools = [call for call in calls if call[0] == "tool"]
    assert {call[1][0] for call in tools} == {
        "education_workflow_list", "education_workflow_create", "education_workflow_get", "education_workflow_update"
    }
    assert all(call[1][1] == "education_workflow" for call in tools)
    prompt = next(call for call in calls if call[0] == "prompt")
    assert prompt[1][0] == "deskrpg_education_workflows"

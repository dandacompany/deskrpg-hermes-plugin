"""`deskrpg_ask_user` — a tool that waits for the person DeskRPG put in the chat, and its routes."""

import json
import threading
import time

import pytest
from aiohttp import web

from deskrpg_plugin import ask_user
from deskrpg_plugin.auth import Scope, require_auth
from tests.conftest import FakeAdapter


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    ask_user.reset_for_tests()
    monkeypatch.setattr(ask_user, "REGISTRATION_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(ask_user, "_session_kind", lambda api, session_id: "chat")
    monkeypatch.setattr(ask_user, "_interrupted", lambda: False)
    yield
    ask_user.reset_for_tests()


ARGS = {"question": "어떤 형식으로 만들까요?", "choices": ["1페이지 요약", "표 중심", "상세 보고서"]}


def _run_in_thread(handler, args, **kwargs):
    out = {}
    thread = threading.Thread(target=lambda: out.setdefault("value", handler(args, **kwargs)), daemon=True)
    thread.start()
    return thread, out


def _wait_pending(profile="noah", timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = ask_user.pending(profile)
        if rows:
            return rows
        time.sleep(0.02)
    raise AssertionError("no pending question")


def test_schema_requires_question_and_two_to_four_choices():
    params = ask_user.TOOL_SCHEMA["parameters"]
    assert ask_user.TOOL_NAME == "deskrpg_ask_user"
    assert ask_user.TOOLSET == "deskrpg"
    assert params["required"] == ["question", "choices"]
    assert (params["properties"]["choices"]["minItems"], params["properties"]["choices"]["maxItems"]) == (2, 4)
    assert "only when" in ask_user.TOOL_SCHEMA["description"]


def test_registered_chat_session_waits_and_returns_the_answer(tmp_api):
    ask_user.register_session("noah", "sess-1")
    thread, out = _run_in_thread(ask_user.make_handler(tmp_api), ARGS, session_id="sess-1")
    [row] = _wait_pending()
    assert row["session_id"] == "sess-1"
    assert row["choices"] == ARGS["choices"]
    assert ask_user.answer("noah", row["id"], "표 중심") == "answered"
    thread.join(3)
    body = json.loads(out["value"])
    assert body["user_response"] == "표 중심"
    assert ask_user.pending("noah") == []


def test_an_unregistered_session_falls_back_after_the_grace(tmp_api):
    started = time.monotonic()
    body = json.loads(ask_user.make_handler(tmp_api)(ARGS, session_id="sess-meeting"))
    assert body["unattended"] is True and body["user_response"] is None
    assert time.monotonic() - started < 2.0
    assert ask_user.pending("noah") == []


def test_registration_arriving_during_the_grace_is_honoured(tmp_api):
    thread, out = _run_in_thread(ask_user.make_handler(tmp_api), ARGS, session_id="sess-late")
    time.sleep(0.05)
    ask_user.register_session("noah", "sess-late")
    [row] = _wait_pending()
    ask_user.answer("noah", row["id"], "상세 보고서")
    thread.join(3)
    assert json.loads(out["value"])["user_response"] == "상세 보고서"


@pytest.mark.parametrize("kind", ["kanban", "cron"])
def test_unattended_runs_never_wait(tmp_api, monkeypatch, kind):
    monkeypatch.setattr(ask_user, "_session_kind", lambda api, session_id: kind)
    ask_user.register_session("noah", "sess-1")
    body = json.loads(ask_user.make_handler(tmp_api)(ARGS, session_id="sess-1"))
    assert body["unattended"] is True
    assert ask_user.pending("noah") == []


def test_stop_interrupts_the_wait_and_clears_the_question(tmp_api, monkeypatch):
    flag = threading.Event()
    monkeypatch.setattr(ask_user, "_interrupted", flag.is_set)
    ask_user.register_session("noah", "sess-1")
    thread, out = _run_in_thread(ask_user.make_handler(tmp_api), ARGS, session_id="sess-1")
    _wait_pending()
    flag.set()
    thread.join(3)
    assert json.loads(out["value"])["cancelled"] is True
    assert ask_user.pending("noah") == []


def test_missing_choices_is_an_error_string_so_the_model_asks_in_plain_text(tmp_api):
    ask_user.register_session("noah", "sess-1")
    body = json.loads(ask_user.make_handler(tmp_api)({"question": "무엇을 할까요?"}, session_id="sess-1"))
    assert body["error"] == "invalid_arguments"
    assert "plain text" in body["detail"]


def test_an_answer_outside_the_choices_is_refused_unless_other_is_allowed(tmp_api):
    ask_user.register_session("noah", "sess-1")
    args = {**ARGS, "allow_other": False}
    thread, _out = _run_in_thread(ask_user.make_handler(tmp_api), args, session_id="sess-1")
    [row] = _wait_pending()
    assert ask_user.answer("noah", row["id"], "아무거나") == "invalid"
    assert ask_user.answer("noah", row["id"], "표 중심") == "answered"
    assert ask_user.answer("noah", row["id"], "상세 보고서") == "not_found"
    thread.join(3)


def test_another_profile_cannot_see_or_answer_the_question(tmp_api):
    ask_user.register_session("noah", "sess-1")
    thread, _out = _run_in_thread(ask_user.make_handler(tmp_api), ARGS, session_id="sess-1")
    [row] = _wait_pending()
    assert ask_user.pending("mia") == []
    assert ask_user.answer("mia", row["id"], "표 중심") == "not_found"
    ask_user.answer("noah", row["id"], "표 중심")
    thread.join(3)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(aiohttp_client, tmp_api):
    app = web.Application()
    adapter = FakeAdapter(authorized=True)
    table = (
        ("POST", "/p/{profile}/deskrpg/ask-user/sessions", ask_user.register_handler),
        ("GET", "/p/{profile}/deskrpg/questions", ask_user.list_handler),
        ("POST", "/p/{profile}/deskrpg/questions/{question_id}/answer", ask_user.answer_handler),
    )
    for method, path, factory in table:
        app.router.add_route(method, path, require_auth(adapter, Scope.PROFILE, factory(tmp_api)))
    return await aiohttp_client(app)


async def test_routes_register_list_and_answer(client, tmp_api):
    resp = await client.post("/p/noah/deskrpg/ask-user/sessions", json={"session_id": "sess-1"})
    assert resp.status == 200
    thread, out = _run_in_thread(ask_user.make_handler(tmp_api), ARGS, session_id="sess-1")
    _wait_pending()

    listed = await (await client.get("/p/noah/deskrpg/questions?session_id=sess-1")).json()
    [question] = listed["questions"]
    assert set(question) == {"id", "session_id", "question", "choices", "allow_other", "created_at", "context"}
    assert (await (await client.get("/p/noah/deskrpg/questions?session_id=other")).json())["questions"] == []
    assert (await (await client.get("/p/mia/deskrpg/questions")).json())["questions"] == []

    resp = await client.post(f"/p/noah/deskrpg/questions/{question['id']}/answer", json={"response": "표 중심"})
    assert resp.status == 200
    thread.join(3)
    assert json.loads(out["value"])["user_response"] == "표 중심"

    resp = await client.post(f"/p/noah/deskrpg/questions/{question['id']}/answer", json={"response": "표 중심"})
    assert resp.status == 404
    assert (await resp.json())["error"] == "question_not_found"


async def test_answer_route_rejects_an_empty_or_off_list_response(client, tmp_api):
    await client.post("/p/noah/deskrpg/ask-user/sessions", json={"session_id": "sess-1"})
    thread, _out = _run_in_thread(ask_user.make_handler(tmp_api), {**ARGS, "allow_other": False}, session_id="sess-1")
    [row] = _wait_pending()
    for body in ({"response": ""}, {"response": "없는 선택지"}, {}):
        resp = await client.post(f"/p/noah/deskrpg/questions/{row['id']}/answer", json=body)
        assert resp.status == 400
    ask_user.answer("noah", row["id"], "표 중심")
    thread.join(3)


async def test_registration_context_is_echoed_with_the_question(client, tmp_api):
    ctx = {"userId": "u-1", "npcId": "npc-9"}
    resp = await client.post("/p/noah/deskrpg/ask-user/sessions", json={"session_id": "sess-1", "context": ctx})
    assert resp.status == 200
    thread, _out = _run_in_thread(ask_user.make_handler(tmp_api), ARGS, session_id="sess-1")
    [row] = _wait_pending()
    listed = await (await client.get("/p/noah/deskrpg/questions")).json()
    assert listed["questions"][0]["context"] == ctx
    ask_user.answer("noah", row["id"], "표 중심")
    thread.join(3)


async def test_registration_context_must_be_a_small_object(client):
    for bad in ("text", ["x"], {"blob": "x" * 2000}):
        resp = await client.post("/p/noah/deskrpg/ask-user/sessions", json={"session_id": "s", "context": bad})
        assert resp.status == 400


async def test_register_route_requires_a_session_id(client):
    resp = await client.post("/p/noah/deskrpg/ask-user/sessions", json={})
    assert resp.status == 400


def test_registration_adds_the_tool_and_a_prompt_section_naming_it(monkeypatch):
    """Hermes' Tool Search hides plugin tools behind tool_search, so the model only knows the tool
    exists if a prompt section names it (the card proposal and artifact tools do the same)."""
    import types

    import deskrpg_plugin
    from deskrpg_plugin import ask_user_prompt

    calls = []

    class _Ctx:
        def __getattr__(self, name):
            if not name.startswith("register_"):
                raise AttributeError(name)
            return lambda *a, **k: calls.append((name, a, k))

    monkeypatch.setattr(deskrpg_plugin, "load", lambda: types.SimpleNamespace())
    deskrpg_plugin.register(_Ctx())
    assert ask_user.TOOL_NAME in {c[1][0] for c in calls if c[0] == "register_tool"}
    sections = {c[1][0]: c[1][1] for c in calls if c[0] == "register_system_prompt_section"}
    text = sections[ask_user_prompt.SECTION_ID]
    assert ask_user.TOOL_NAME in text
    assert "tool_search" in text
    assert "2-4" in text and "plain text" in text

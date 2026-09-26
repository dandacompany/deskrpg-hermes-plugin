"""`deskrpg_ask_user` — 대화 중 NPC 가 사람에게 선택지로 묻고 **답을 기다리는** 도구와 그 라우트.

Hermes 의 clarify 는 api_server 경로에 없다(툴셋에서 빠지고, 콜백·이벤트·응답 라우트가 없다).
그래서 이 도구가 스스로 기다린다. clarify_gateway.wait_for_response 와 같은 방식이다 — 1초 조각으로
기다리며 활동 신호를 보내고(워치독), 중단(interrupt)을 확인한다.

**누가 답하는가.** DeskRPG 가 1:1 대화 run 을 시작할 때 그 Hermes 세션을 여기 등록한다
(`register_session`). 등록된 세션에서만 기다린다 — 회의 run, 이 게이트웨이를 쓰는 다른 api_server
클라이언트, delegate 자식 세션은 짧은 유예 뒤 "알아서 가정" 으로 바로 답한다. 기다리는 시간에
상한이 없으므로(단테 결정, 2026-09-26), 답할 사람이 없는 곳에서 매달리지 않게 하는 장치가 이것이다.

**저장은 메모리뿐이다.** 게이트웨이가 재시작하면 run 도 함께 죽으므로 영속할 이유가 없고,
DeskRPG 는 대기 목록을 사본으로 저장하지 않는다(Hermes 정본). 도구는 예외를 밖으로 내지 않는다 —
실패는 `{"error", "detail"}` 문자열이다(`card_proposal_tool.py` 와 같은 규약).
"""

import json
import logging
import sys
import threading
import time
import types
import uuid

from aiohttp import web

from . import artifacts_context as context
from .common import guarded, json_error, read_json_object

logger = logging.getLogger("deskrpg_plugin")

TOOL_NAME = "deskrpg_ask_user"
TOOLSET = "deskrpg"

QUESTION_MAX = 500
CHOICE_MAX = 80
# 등록이 run 시작 직후에 오므로 도구가 먼저 불리는 경쟁을 흡수한다. 그 뒤로도 없으면 답할 사람이 없는 것이다.
REGISTRATION_GRACE_SECONDS = 5.0
REGISTRATION_TTL_SECONDS = 24 * 3600
# DeskRPG 가 등록 때 붙이는 불투명 식별자(누구의 대화인가). 해석하지 않고 질문과 함께 돌려준다.
CONTEXT_MAX_BYTES = 1024
WAIT_SLICE_SECONDS = 1.0

UNATTENDED = {
    "user_response": None,
    "unattended": True,
    "note": "No user is available to answer here; make the most reasonable assumption and say what you assumed.",
}

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Ask the user one question and wait for their answer. Use it only when you need information only the "
        "user has AND the answer can be narrowed to 2-4 choices; ask open questions in plain text instead. "
        "One question per call. The user may also type their own answer unless allow_other is false."
    ),
    "parameters": {
        "type": "object",
        "required": ["question", "choices"],
        "properties": {
            "question": {"type": "string", "maxLength": QUESTION_MAX},
            "choices": {"type": "array", "minItems": 2, "maxItems": 4,
                        "items": {"type": "string", "maxLength": CHOICE_MAX}},
            "allow_other": {"type": "boolean", "description": "let the user type an answer outside the choices "
                                                              "(default true)"},
        },
        "additionalProperties": False,
    },
}

_STATE_MODULE = "deskrpg_ask_user_shared_state"


def _shared_state():
    """Process-wide state shared by every loaded copy of this plugin.

    Hermes imports a directory plugin once per profile scope, under different module names
    (``hermes_plugins.deskrpg``, ``hermes_plugins.deskrpg__home_<digest>``). The api_server routes are
    wired from one copy and a profile's tool runs from another, so module globals would split the
    registrations and questions in two — the tool would never see a registration (measured on
    staging, 0.24.0-0.24.2). One anchor module in ``sys.modules`` holds them instead.
    """
    state = sys.modules.get(_STATE_MODULE)
    if state is None:
        fresh = types.ModuleType(_STATE_MODULE)
        fresh.lock = threading.Lock()
        # session_id -> (registered_at monotonic, profile, context). Keyed by session alone: the
        # profile comes from the registration, which the profile's own key authenticated.
        fresh.sessions = {}
        fresh.questions = {}  # question_id -> entry
        state = sys.modules.setdefault(_STATE_MODULE, fresh)
    return state


_state = _shared_state()
_lock = _state.lock
_sessions: dict = _state.sessions
_questions: dict = _state.questions


def reset_for_tests() -> None:
    with _lock:
        _sessions.clear()
        _questions.clear()


def _interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted
    except Exception:  # noqa: BLE001 — Hermes 밖(테스트·구버전)에서는 중단 신호가 없다
        return False
    return bool(is_interrupted())


def _touch(state: dict) -> None:
    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:  # noqa: BLE001
        return
    touch_activity_if_due(state, "waiting for the user's answer")


def _session_kind(api, session_id: str) -> str:
    return context.resolve_context(api, session_id=session_id).source_kind


def register_session(profile: str, session_id: str, session_context=None) -> None:
    now = time.monotonic()
    with _lock:
        for key, (at, _profile, _ctx) in list(_sessions.items()):
            if now - at > REGISTRATION_TTL_SECONDS:
                del _sessions[key]
        _sessions[session_id] = (now, profile, dict(session_context or {}))


def _registration(session_id: str):
    """등록돼 있으면 (profile, context), 없으면 None."""
    with _lock:
        found = _sessions.get(session_id)
    if found is None or time.monotonic() - found[0] > REGISTRATION_TTL_SECONDS:
        return None
    return found[1], found[2]


def _public(entry: dict) -> dict:
    keys = ("id", "session_id", "question", "choices", "allow_other", "created_at", "context")
    return {key: entry[key] for key in keys}


def pending(profile: str, session_id=None) -> list:
    with _lock:
        rows = [_public(e) for e in _questions.values()
                if e["profile"] == profile and (session_id is None or e["session_id"] == session_id)]
    return sorted(rows, key=lambda r: r["created_at"])


def answer(profile: str, question_id: str, response: str) -> str:
    """`answered` · `not_found` · `invalid`. 받는 순간 목록에서 뺀다 — 두 번째 답은 `not_found` 다."""
    response = response.strip() if isinstance(response, str) else ""
    with _lock:
        entry = _questions.get(question_id)
        if entry is None or entry["profile"] != profile:
            return "not_found"
        if not response or (not entry["allow_other"] and response not in entry["choices"]):
            return "invalid"
        del _questions[question_id]
        entry["answer"] = response
    entry["event"].set()
    return "answered"


def _err(code: str, detail: str) -> str:
    return json.dumps({"error": code, "detail": detail}, ensure_ascii=False)


def _arguments(args: dict):
    question = args.get("question")
    choices = args.get("choices")
    if not isinstance(question, str) or not question.strip():
        return None, _err("invalid_arguments", "question is required")
    if (not isinstance(choices, list) or not 2 <= len(choices) <= 4
            or not all(isinstance(c, str) and c.strip() for c in choices)):
        return None, _err("invalid_arguments", "choices must be 2-4 non-empty strings; "
                                               "if the answer can't be narrowed to choices, ask in plain text")
    allow_other = args.get("allow_other", True)
    return {
        "question": question.strip()[:QUESTION_MAX],
        "choices": [c.strip()[:CHOICE_MAX] for c in choices],
        "allow_other": allow_other is not False,
    }, None


def _wait_for_registration(session_id: str):
    """(profile, context), 유예 안에 등록이 오지 않으면 None."""
    deadline = time.monotonic() + REGISTRATION_GRACE_SECONDS
    while True:
        found = _registration(session_id)
        if found is not None:
            return found
        if time.monotonic() >= deadline or _interrupted():
            return None
        time.sleep(0.05)


def _ask(api, args: dict, kwargs: dict) -> str:
    parsed, error = _arguments(args)
    if error:
        return error
    session_id = str(kwargs.get("session_id") or "")
    kind = _session_kind(api, session_id) if session_id else "none"
    if kind != "chat":
        logger.info("[deskrpg] ask_user unattended: session=%s kind=%s", session_id or "-", kind)
        return json.dumps(UNATTENDED, ensure_ascii=False)
    registration = _wait_for_registration(session_id)
    if registration is None:
        logger.info("[deskrpg] ask_user unattended: session=%s not registered", session_id)
        return json.dumps(UNATTENDED, ensure_ascii=False)
    profile, session_context = registration

    entry = {
        "id": uuid.uuid4().hex, "profile": profile, "session_id": session_id, "context": session_context,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": threading.Event(), "answer": None, **parsed,
    }
    with _lock:
        _questions[entry["id"]] = entry
    logger.info("[deskrpg] ask_user waiting profile=%s session=%s", profile, session_id)

    # No deadline (Dante's decision): wait until answered or the run is stopped.
    state = {"last_touch": time.monotonic(), "start": time.monotonic()}
    while not entry["event"].wait(timeout=WAIT_SLICE_SECONDS):
        if _interrupted():
            with _lock:
                _questions.pop(entry["id"], None)
            return json.dumps({"user_response": None, "cancelled": True}, ensure_ascii=False)
        _touch(state)
    return json.dumps({"question": entry["question"], "user_response": entry["answer"]}, ensure_ascii=False)


def make_handler(api):
    def handler(args, **kwargs) -> str:
        try:
            return _ask(api, args if isinstance(args, dict) else {}, kwargs)
        except Exception as exc:  # noqa: BLE001 — 도구는 예외를 밖으로 내지 않는다
            logger.exception("[deskrpg] deskrpg_ask_user exception: %s", type(exc).__name__)
            return _err("internal_error", type(exc).__name__)

    return handler


# ---------------------------------------------------------------------------
# Routes (profile scope — the profile key only sees and answers its own questions)
# ---------------------------------------------------------------------------


def register_handler(api):
    @guarded
    async def handler(request):
        body = await read_json_object(request)
        session_id = body.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return json_error(400, "invalid_field", "session_id is required")
        session_context = body.get("context", {})
        if (not isinstance(session_context, dict)
                or len(json.dumps(session_context, ensure_ascii=False).encode()) > CONTEXT_MAX_BYTES):
            return json_error(400, "invalid_field", f"context must be an object of at most {CONTEXT_MAX_BYTES} bytes")
        register_session(request.match_info["profile"], session_id.strip(), session_context)
        return web.json_response({"registered": True})

    return handler


def list_handler(api):
    @guarded
    async def handler(request):
        session_id = request.query.get("session_id") or None
        return web.json_response({"questions": pending(request.match_info["profile"], session_id)})

    return handler


def answer_handler(api):
    @guarded
    async def handler(request):
        body = await read_json_object(request)
        outcome = answer(request.match_info["profile"], request.match_info["question_id"], body.get("response"))
        if outcome == "not_found":
            return json_error(404, "question_not_found")
        if outcome == "invalid":
            return json_error(400, "invalid_response")
        return web.json_response({"answered": True})

    return handler

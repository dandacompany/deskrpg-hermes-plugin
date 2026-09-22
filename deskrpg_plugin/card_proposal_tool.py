"""`propose_kanban_card` — 모델이 "카드로 남길 만한 요청" 을 발견했을 때 부르는 도구.

**카드를 만들지 않는다.** 제안만 기록하고, 등록 여부는 사용자가 DeskRPG 에서 버튼으로 고른다.
실패는 예외가 아니라 `{"error", "detail"}` 문자열이다 — Hermes 가 그 문자열을 모델에게 그대로 주고
모델이 읽고 고쳐 다시 부른다(`artifacts_tool.py` 와 같은 규약).
`session_id`·`task_id` 는 Hermes 가 kwargs 로 준다. 모델 인자에 같은 이름이 있어도 무시한다.
"""

import json
import logging

from . import artifacts_context as context
from . import card_proposal_store as store
from .common import log_event

logger = logging.getLogger("deskrpg_plugin")

TOOL_NAME = "propose_kanban_card"
TOOLSET = "deskrpg"

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "사용자의 말에서 '지금 이 대화에서 끝내기보다 업무 카드로 남겨 추적하는 편이 나은 요청'을 "
        "발견했을 때 부른다. 이 도구는 카드를 만들지 않는다 — 사용자에게 보여 줄 제안만 기록하고, "
        "등록 여부는 사용자가 버튼으로 고른다. 단순 질문·잡담·즉답 가능한 부탁에는 부르지 않는다."
    ),
    "parameters": {
        "type": "object",
        "required": ["title", "summary"],
        "properties": {
            "title": {"type": "string", "maxLength": 200},
            "summary": {"type": "string", "maxLength": 600,
                        "description": "1~2문장. 무엇을 하는 일이고 왜 카드로 남기는가"},
            "body": {"type": "string", "maxLength": 4000,
                     "description": "카드 본문. 없으면 summary 를 쓴다"},
            "acceptance": {"type": "string", "maxLength": 1000,
                           "description": "무엇이 참이면 끝인가"},
        },
        "additionalProperties": False,
    },
}


def _err(code: str, detail: str) -> str:
    return json.dumps({"error": code, "detail": detail}, ensure_ascii=False)


def _str(args: dict, key: str, limit: int) -> str:
    value = args.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


def make_handler(api):
    def handler(args, **kwargs) -> str:
        try:
            return _propose(api, args if isinstance(args, dict) else {}, kwargs)
        except Exception as exc:  # noqa: BLE001 — 도구는 예외를 밖으로 내지 않는다
            logger.exception("[deskrpg] propose_kanban_card 예외: %s", type(exc).__name__)
            return _err("internal_error", type(exc).__name__)

    return handler


def _propose(api, args: dict, kwargs: dict) -> str:
    title, summary = _str(args, "title", 200), _str(args, "summary", 600)
    missing = [name for name, value in (("title", title), ("summary", summary)) if not value]
    if missing:
        return _err("invalid_arguments", f"{', '.join(missing)} 는 필수다")
    # 담당은 모델이 고르지 않는다 — 제안한 프로필이 담당 기본값이고 그 판정은 DeskRPG 가 한다.
    # 프로필도 모델 인자가 아니라 실행 중인 홈(또는 Hermes 가 준 kwargs)에서 읽는다.
    profile = str(kwargs.get("profile") or "").strip() or context.current_profile(api)
    proposal_id = store.create(
        api, profile=profile, title=title, summary=summary,
        body=_str(args, "body", 4000) or None, acceptance=_str(args, "acceptance", 1000) or None,
    )
    log_event("card_proposal.created", proposal_id=proposal_id, profile=profile)
    return json.dumps({"proposed": True, "proposal_id": proposal_id}, ensure_ascii=False)

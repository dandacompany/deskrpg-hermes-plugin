"""수집 컨텍스트 — 출처(채팅·칸반·크론)와 카드 id 를 Hermes 가 정한 값으로만 정한다.

**칸반 카드 id 는 환경변수 `HERMES_KANBAN_TASK` 에서만 읽는다.** 디스패처가 워커를 띄울 때 넣는 값이고
(`hermes_cli/kanban_db_dispatch.py` `env["HERMES_KANBAN_TASK"] = task.id`), Hermes 자신의 칸반 도구도
이것으로 카드를 식별한다. 훅·도구에 넘어오는 `task_id` 인자는 카드 id 가 **아니다** — 에이전트의 실행 범위
id(`effective_task_id`, 터미널·파일 격리용)이고 채팅에서도 늘 차 있다(api_server: `session_id or uuid4()`).
예전에 그 값을 카드 id 로 믿어, 칸반 결과물에는 세션 id 가 `task_id` 로 붙고(카드 필터에 안 잡혔다) 채팅
결과물은 전부 "칸반" 으로 분류됐다.

모델이 넘긴 인자로 출처를 정하지 않는다(위조 가능). `profile` 은 실행 중인 홈에서 읽는다 — Hermes 는
프로필 세션을 `HERMES_HOME=<…>/profiles/<name>` 으로 돌린다(0.21.x, `hermes_cli/profiles.py`).
크론 세션 판정은 세션 DB 의 `source` 열이다(Task 1 스파이크 결론; 값이 다르면 `CRON_SOURCES` 를 고친다).
"""

import dataclasses
import logging
import os
from pathlib import Path

logger = logging.getLogger("deskrpg_plugin")

CRON_SOURCES = frozenset({"cron", "scheduler", "cron_scheduler"})


@dataclasses.dataclass(frozen=True)
class CaptureContext:
    session_id: str
    profile: str
    source_kind: str
    task_id: str | None = None
    board: str | None = None
    job_id: str | None = None
    run_id: str | None = None


def current_profile(api) -> str:
    home = Path(api.get_hermes_home())
    if home.parent.name == "profiles":
        return home.name
    return "default"


def _session_source(api, session_id: str):
    try:
        db = api.SessionDB(str(Path(api.get_hermes_home()) / "state.db"), read_only=True)
    except Exception:  # noqa: BLE001 — 세션 DB 부재·잠금은 출처 판정을 chat 으로 떨어뜨릴 뿐이다
        return None
    try:
        row = db.get_session(session_id)
        return (row or {}).get("source") if isinstance(row, dict) else getattr(row, "source", None)
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


KANBAN_TASK_ENV = "HERMES_KANBAN_TASK"


def resolve_context(api, *, session_id: str) -> CaptureContext:
    profile = current_profile(api)
    task_id = os.environ.get(KANBAN_TASK_ENV) or None
    if task_id:
        try:
            board = api.get_current_board()
        except Exception:  # noqa: BLE001
            board = None
        return CaptureContext(session_id or "", profile, "kanban", task_id=str(task_id), board=board)
    source = _session_source(api, session_id or "")
    kind = "cron" if source and str(source).lower() in CRON_SOURCES else "chat"
    return CaptureContext(session_id or "", profile, kind)

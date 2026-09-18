"""수집 컨텍스트 — Hermes 가 도구·훅에 넘긴 `session_id`·`task_id` 만으로 출처를 정한다.

모델이 넘긴 인자로 출처를 정하지 않는다(위조 가능). `profile` 은 실행 중인 홈에서 읽는다 — Hermes 는
프로필 세션을 `HERMES_HOME=<…>/profiles/<name>` 으로 돌린다(0.21.x, `hermes_cli/profiles.py`).
크론 세션 판정은 세션 DB 의 `source` 열이다(Task 1 스파이크 결론; 값이 다르면 `CRON_SOURCES` 를 고친다).
"""

import dataclasses
import logging
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


def resolve_context(api, *, session_id: str, task_id) -> CaptureContext:
    profile = current_profile(api)
    if task_id:
        try:
            board = api.get_current_board()
        except Exception:  # noqa: BLE001
            board = None
        return CaptureContext(session_id or "", profile, "kanban", task_id=str(task_id), board=board)
    source = _session_source(api, session_id or "")
    kind = "cron" if source and str(source).lower() in CRON_SOURCES else "chat"
    return CaptureContext(session_id or "", profile, kind)

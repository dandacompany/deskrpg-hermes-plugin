"""크론 실행의 결과 본문·상태를 알아내는 도우미 (K3 · E4).

크론 라우트(`cron.py`)와 통합 사건 스트림(`events.py`)이 같은 규칙으로 실행을 읽어야 한다 —
같은 실행이 "실행 이력" 화면과 "사건" 화면에서 다른 본문·다른 상태로 보이면 안 된다. 그래서
규칙을 여기 한 곳에 두고 양쪽이 가져다 쓴다.

세 가지 출처를 이 순서로 본다(E4):
1. 세션 DB(`<프로필 홈>/state.db`)에서 그 실행에 대응하는 세션의 **마지막 assistant 메시지**
2. 없으면 잡 출력 폴더(`get_cron_output_dir()/<job_id>/`)에서 그 실행 시각에 해당하는 `.md`
3. 그것도 없으면 `""`

세션 대응 규칙: id 가 `cron_<job_id>_` 로 시작하고 `source='cron'` 이며, 시작 시각이 실행의
`started_at` 과 `SESSION_MATCH_WINDOW_S` 안에 드는 것.

**전부 블로킹(sqlite·파일)이다.** 호출자는 `run_blocking` 안에서, 그리고 크론 저장소 스코프
(`cron.cron_scope`) 안에서 불러야 한다 — `get_cron_output_dir()` 은 현재 스코프의 홈을 본다.
"""

import logging
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("deskrpg_plugin")

# 결과 본문 상한. 초과분은 잘라내고 끝에 "…" 를 붙인다(E4, tunable).
RESULT_TEXT_MAX_CHARS = 20_000
# 실행 started_at 과 세션 started_at 의 허용 오차(초). 세션은 실행 직후 열리므로 보통 몇 초 차이다.
SESSION_MATCH_WINDOW_S = 120
# 세션 행의 ended_at 이 없고 last_active 가 이보다 최근이면 "running" 으로 본다(K3).
RUN_ACTIVE_WINDOW_S = 300
# K3 의 limit 상한 — 세션마다 메시지를 읽으므로 무한정 키우지 않는다.
RUNS_LIMIT_MAX = 50
RUNS_LIMIT_DEFAULT = 20
# 실행 장부에서 잡 하나의 실행을 읽을 때의 페이지 크기(Hermes 상한과 같다).
EXECUTIONS_PAGE_LIMIT = 500

STATE_DB_FILENAME = "state.db"
EXECUTIONS_DB_RELPATH = Path("cron") / "executions.db"
_OUTPUT_FILE_TIME_FORMAT = "%Y-%m-%d_%H-%M-%S"  # cron/jobs.py save_job_output 의 파일명


def open_session_db(api, profile_home):
    """`<profile_home>/state.db` 를 **읽기 전용**으로 연다. 호출자가 `close()` 한다.

    쓰기 모드로 열면 Hermes 가 스키마 마이그레이션·WAL 전환을 시도한다 — 게이트웨이의 다른
    프로세스가 쓰고 있는 파일을 우리가 건드릴 이유가 없다.
    """
    return api.SessionDB(Path(profile_home) / STATE_DB_FILENAME, read_only=True)


def executions_db_exists(profile_home) -> bool:
    """실행 장부 파일이 있는가. **경로로만** 검사한다 — 열면 Hermes 가 파일을 만들어 버린다(E4)."""
    return (Path(profile_home) / EXECUTIONS_DB_RELPATH).is_file()


def to_epoch(value, tz=None):
    """epoch 초(float)·ISO 문자열·datetime 을 epoch 초로. 해석 불가면 None.

    실행 장부의 `started_at` 은 tz 가 붙은 ISO 문자열이고, 세션 행의 `started_at` 은 epoch REAL
    이다. 둘을 비교하려면 한쪽으로 맞춰야 한다. tz 없는 문자열은 `tz`(Hermes 설정 시간대)
    또는 서버 로컬로 본다.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz) if tz is not None else dt.astimezone()
    return dt.timestamp()


def find_session_for_execution(sdb, job_id, started_at, window_s=SESSION_MATCH_WINDOW_S, *, tz=None):
    """실행(started_at)에 대응하는 세션 id. 없으면 None.

    `list_cron_job_runs` 는 id 접두사 `cron_<job_id>_` + `source='cron'` 을 이미 보장하고 최신순이다.
    시작 시각 차이가 가장 작은 것을 고른다 — 같은 잡이 창 안에서 두 번 돌면(수동 실행 직후
    스케줄 실행) 가까운 쪽이 맞다.
    """
    target = to_epoch(started_at, tz)
    if target is None:
        return None
    best_id = None
    best_diff = None
    for row in sdb.list_cron_job_runs(job_id, limit=RUNS_LIMIT_MAX):
        row_start = to_epoch(row.get("started_at"), tz)
        if row_start is None:
            continue
        diff = abs(row_start - target)
        if diff <= window_s and (best_diff is None or diff < best_diff):
            best_id, best_diff = row.get("id"), diff
    return best_id


def _content_text(content) -> str:
    """메시지 content 가 문자열이면 그대로, 블록 리스트면 text 블록만 이어 붙인다."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(p for p in parts if p)
    return ""


def last_assistant_text(sdb, session_id) -> str:
    """세션의 마지막 assistant 메시지 본문. 없으면 `""`.

    도구 호출만 있고 본문이 빈 assistant 행은 건너뛴다 — 사용자에게 보여줄 "결과" 는 말로 된
    마지막 응답이다.
    """
    if not session_id:
        return ""
    for msg in reversed(sdb.get_messages(session_id)):
        if msg.get("role") != "assistant":
            continue
        text = _content_text(msg.get("content")).strip()
        if text:
            return text
    return ""


def output_file_text(api, job_id, started_at, finished_at=None, *, window_s=SESSION_MATCH_WINDOW_S, tz=None) -> str:
    """잡 출력 폴더에서 이 실행의 결과 파일 본문. 없으면 `""`.

    파일명은 저장 시각(`YYYY-MM-DD_HH-MM-SS.md`, Hermes 설정 시간대)이고 저장은 실행이 **끝난 뒤**
    라, `[started_at - window, finished_at(없으면 지금) + window]` 안에 드는 것 중 가장 늦은
    파일을 고른다. 폴더가 없거나 읽기 실패면 빈 문자열 — 결과를 못 찾는 것은 오류가 아니다.
    """
    start = to_epoch(started_at, tz)
    if start is None:
        return ""
    end = to_epoch(finished_at, tz)
    if end is None:
        end = time.time()
    try:
        job_dir = Path(api.get_cron_output_dir()) / str(job_id)
        if not job_dir.is_dir():
            return ""
        candidates = []
        for path in job_dir.glob("*.md"):
            if not path.is_file():
                continue
            try:
                stamp = datetime.strptime(path.stem, _OUTPUT_FILE_TIME_FORMAT)
            except ValueError:
                continue
            stamp_epoch = to_epoch(stamp, tz)
            if stamp_epoch is None:
                continue
            if start - window_s <= stamp_epoch <= end + window_s:
                candidates.append((stamp_epoch, path))
        if not candidates:
            return ""
        _stamp, chosen = max(candidates, key=lambda item: item[0])
        return chosen.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("[deskrpg] 크론 출력 파일 읽기 실패 job_id=%s: %s", job_id, exc)
        return ""


def truncate_result(text: str, max_chars=None) -> str:
    """상한을 넘는 본문은 잘라내고 끝에 "…" 를 붙인다. `max_chars` 생략 시 모듈 상수(tunable)를 호출 시점에 읽는다."""
    if text is None:
        return ""
    if max_chars is None:
        max_chars = RESULT_TEXT_MAX_CHARS
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def result_text_for(api, sdb, job_id, started_at, *, session_id=None, finished_at=None, tz=None) -> str:
    """E4 규칙으로 실행의 결과 본문을 만든다(세션 → 출력 파일 → `""`), 상한 적용.

    `session_id` 를 이미 알면(K3 의 세션 행) 넘긴다. 모르면 `started_at` 으로 찾는다.
    `sdb` 가 None 이면 세션 단계를 건너뛴다(state.db 가 없는 프로필).
    """
    text = ""
    if sdb is not None:
        sid = session_id or find_session_for_execution(sdb, job_id, started_at, tz=tz)
        if sid:
            text = last_assistant_text(sdb, sid)
    if not text:
        text = output_file_text(api, job_id, started_at, finished_at, tz=tz)
    return truncate_result(text or "")


def run_status_for(execution):
    """실행 장부 상태 → 계약 상태. completed → "ok", failed|unknown → "error", 그 외·None → None.

    None 은 "아직 판정 불가"(claimed/running 이거나 대응 실행 없음)다 — 호출자가 문맥에 맞게
    `running`/`unknown` 으로 채운다.
    """
    if not execution:
        return None
    status = execution.get("status")
    if status == "completed":
        return "ok"
    if status in ("failed", "unknown"):
        return "error"
    return None


def match_execution(executions, started_at, window_s=SESSION_MATCH_WINDOW_S, *, tz=None):
    """실행 목록에서 `started_at` 과 창 안에서 가장 가까운 실행. 없으면 None.

    실행의 `started_at` 이 비어 있으면(claimed 만 되고 못 뜬 것) `claimed_at` 으로 대신 잰다.
    """
    target = to_epoch(started_at, tz)
    if target is None:
        return None
    best = None
    best_diff = None
    for execution in executions:
        stamp = to_epoch(execution.get("started_at") or execution.get("claimed_at"), tz)
        if stamp is None:
            continue
        diff = abs(stamp - target)
        if diff <= window_s and (best_diff is None or diff < best_diff):
            best, best_diff = execution, diff
    return best


def clamp_runs_limit(raw, default=RUNS_LIMIT_DEFAULT, maximum=RUNS_LIMIT_MAX) -> int:
    """`?limit=` 을 1..maximum 으로 죈다. 숫자가 아니면 기본값."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def _timezone_of(api):
    try:
        return api.get_timezone()
    except Exception:
        return None


def cron_runs_for_job(api, profile_home, job_id, limit, *, now=None):
    """K3 — 잡의 실행 이력 행들. `[{id, started_at, ended_at, status, summary, result_text}]`.

    크론 저장소 스코프 안·워커 스레드 안에서 부른다. state.db 는 읽기 전용으로 열고 여기서 닫는다.
    state.db 가 없으면 빈 목록 — 아직 한 번도 돈 적 없는 프로필이다.

    status 규칙: 행의 ended_at 이 없고 now - last_active < RUN_ACTIVE_WINDOW_S 면 "running";
    아니면 실행 장부(파일이 있을 때만 연다)에서 ±window 로 대응되는 실행의 상태(ok/error);
    대응 없으면 "unknown".
    """
    home = Path(profile_home)
    if not (home / STATE_DB_FILENAME).is_file():
        return []
    tz = _timezone_of(api)
    now_epoch = time.time() if now is None else float(now)

    executions = []
    if executions_db_exists(home):
        try:
            executions = api.list_executions(job_id=job_id, limit=EXECUTIONS_PAGE_LIMIT)
        except Exception as exc:
            # 장부를 못 읽어도 이력은 보여 준다 — 상태만 unknown 으로 떨어진다.
            logger.warning("[deskrpg] 크론 실행 장부 읽기 실패 job_id=%s: %s", job_id, exc)
            executions = []

    sdb = open_session_db(api, home)
    try:
        rows = sdb.list_cron_job_runs(job_id, limit=limit)
        out = []
        for row in rows:
            session_id = row.get("id")
            started_at = row.get("started_at")
            ended_at = row.get("ended_at")
            last_active = row.get("last_active", started_at)
            last_active_epoch = to_epoch(last_active, tz) or 0.0
            if ended_at is None and (now_epoch - last_active_epoch) < RUN_ACTIVE_WINDOW_S:
                status = "running"
            else:
                status = run_status_for(match_execution(executions, started_at, tz=tz)) or "unknown"
            out.append(
                {
                    "id": session_id,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "status": status,
                    "summary": row.get("title") or row.get("preview") or "",
                    "result_text": result_text_for(
                        api, sdb, job_id, started_at, session_id=session_id, finished_at=ended_at, tz=tz
                    ),
                }
            )
        return out
    finally:
        sdb.close()

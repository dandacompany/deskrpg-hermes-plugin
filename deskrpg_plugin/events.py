"""통합 사건 스트림 — `GET /deskrpg/events?board=&cursor=&limit=` (E1–E7).

네 출처를 하나의 시간순 목록으로 합친다:

1. **칸반** — 보드의 `task_events` 를 `id > k` 로 tail 하고 Hermes kind 를 계약 kind 로 매핑한다(E3).
2. **삭제 기록** — 플러그인이 카드를 지울 때 `board_dir(slug)/deskrpg_deleted.jsonl` 에 남긴 줄을
   `n > d` 로 읽는다(§5.3). Hermes 는 `task_events` 도 같이 지우므로 여기가 유일한 흔적이다.
3. **크론** — 호스트의 **모든 프로필**에 대해 실행 장부(`<home>/cron/executions.db`)를 최신순으로
   `claimed_at <= t` 까지 읽고, 토큰의 `o`(미완료 실행) 와 비교해 started/finished 를 낸다(E4).
   장부 파일이 없는 프로필은 잡 레코드(`fire_claim`·`last_run_at`)로 폴백한다.
4. **아티팩트** — 아티팩트 레지스트리(`registry.db`)의 `artifact_events` 를 `id > a` 로 tail 한다.
   게이트웨이 전역 사건이라 `board` 로 거르지 않는다(R6) — DeskRPG 가 채널로 거른다.
   `include=artifacts` 로 옵트인한 호출에서만 읽는다(R17). `ts` 는 다른 출처처럼 epoch **초**다.

커서는 `v1.` + base64url(JSON) 의 불투명 토큰이고, **각 출처의 위치는 실제로 응답에 실은 사건까지만
전진**한다(E5·E6). 잘린 사건은 다음 호출에 다시 나온다 — 스트림에 구멍이 나는 것보다 두 번 보이는
편이 낫고, 그마저도 응답 기준으로는 생기지 않는다.

**전부 블로킹(sqlite·파일)이다.** 핸들러는 `run_blocking` 안에서 한 번에 돌린다.
"""

import base64
import binascii
import json
import logging
import time
from pathlib import Path

from aiohttp import web

from . import artifacts_store as _artifacts
from . import cron as _cron
from . import cron_results
from . import deleted_log
from .common import BOARD_SLUG_RE, RequestError, board_conn, guarded, log_event, parse_board_slug, read_json_object, run_blocking

logger = logging.getLogger("deskrpg_plugin")

CURSOR_VERSION = "v1"
CURSOR_PREFIX = CURSOR_VERSION + "."
LIMIT_DEFAULT = 200
LIMIT_MAX = 500

DELETED_LOG_FILENAME = deleted_log.FILENAME

# 병합 tie-break 의 출처 순서(E5): k < d < c < a.
_SOURCE_RANK = {"k": 0, "d": 1, "c": 2, "a": 3}

# ---------------------------------------------------------------------------
# E3 — Hermes task_events.kind → 계약 kind
# ---------------------------------------------------------------------------

RUN_FINISHED_KINDS = frozenset({"completed", "reclaimed", "gave_up", "timed_out", "crashed", "stale"})
STATUS_KINDS = frozenset({
    "status", "promoted", "promoted_manual", "blocked", "unblocked", "review_requested",
    "changes_requested", "review_reopened", "archived", "scheduled", "specified",
})
UPDATED_KINDS = frozenset({"attached", "attachment_removed", "edited", "reprioritized", "assigned"})
IGNORED_KINDS = frozenset({
    "claimed", "claim_rejected", "claim_extended", "dependency_wait", "block_loop_detected",
    "respawn_guarded", "reconciled", "reclaim_deferred", "pr_acceptance",
    "suspected_hallucinated_references", "completion_blocked_hallucination",
})

# payload 에 status 가 없는 kind 의 결과 상태. Hermes 가 그 kind 를 찍을 때 반드시 두는 상태다 —
# `promoted`·`review_reopened` 는 결과가 ready 일 때 payload 를 아예 비우고(kanban_db.py 2061·3330),
# `archived` 는 payload 가 항상 None 이다. 이게 없으면 "현재 tasks.status" 로 떨어져, 그 뒤에 또
# 전이가 있었을 때 엉뚱한 `to` 가 나온다. 표에도 없으면 현재 상태를 쓴다(스펙 E3).
_KIND_DEFAULT_STATUS = {
    "promoted": "ready",
    "promoted_manual": "ready",
    "review_reopened": "ready",
    "unblocked": "ready",
    "archived": "archived",
    "blocked": "blocked",
    "scheduled": "scheduled",
    "review_requested": "review",
    "completed": "done",
}

# `task.updated.payload.fields` — kind 가 곧 바뀐 필드를 말한다. `edited` 는 payload 가 들고 있다.
_UPDATED_FIELDS = {
    "attached": ["attachments"],
    "attachment_removed": ["attachments"],
    "reprioritized": ["priority"],
    "assigned": ["assignee"],
}

# `from` 을 거슬러 찾을 때 "상태를 말해 주는" kind — created 와 status/run.finished 계열.
_STATUS_BEARING_KINDS = STATUS_KINDS | RUN_FINISHED_KINDS | {"created"}

# 칸반 DB 에 직접 던지는 SQL. 대시보드 `_EventTail._fetch` 와 같은 tail 질의다. 테스트의 가짜 연결은
# 이 문자열을 그대로 대조하므로 바꾸면 `tests/fakes_events.py` 도 같이 바꾼다.
SQL_TAIL = (
    "SELECT id, task_id, run_id, kind, payload, created_at "
    "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT ?"
)
SQL_EARLIER_FOR_TASK = (
    "SELECT kind, payload FROM task_events WHERE task_id = ? AND id < ? ORDER BY id DESC"
)
SQL_MAX_ID = "SELECT COALESCE(MAX(id), 0) AS max_id FROM task_events"


# ---------------------------------------------------------------------------
# E2 — 커서
# ---------------------------------------------------------------------------


def encode_cursor(state: dict) -> str:
    """`{"k","d","c"}` → `v1.<base64url(JSON)>`. 패딩은 뗀다(URL 에 그대로 실린다)."""
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token) -> dict:
    """토큰 → 상태. 버전·인코딩·모양이 하나라도 틀리면 400 `unknown_cursor` (DeskRPG 는 커서 없이 다시 부른다)."""
    if not isinstance(token, str) or not token.startswith(CURSOR_PREFIX):
        raise RequestError(400, "unknown_cursor")
    body = token[len(CURSOR_PREFIX):]
    try:
        padded = body + "=" * (-len(body) % 4)
        state = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeError):
        raise RequestError(400, "unknown_cursor")
    if not _valid_state(state):
        raise RequestError(400, "unknown_cursor")
    return state


def _valid_state(state) -> bool:
    if not isinstance(state, dict):
        return False
    if not _is_int(state.get("k")) or not _is_int(state.get("d")):
        return False
    profiles = state.get("c")
    if not isinstance(profiles, dict):
        return False
    for part in profiles.values():
        if not isinstance(part, dict):
            return False
        t = part.get("t")
        if t is not None and not isinstance(t, str):
            return False
        if not isinstance(part.get("o"), dict):
            return False
    if "a" in state and not _is_int(state.get("a")):
        return False
    return True


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_handoff_state(state: dict) -> bool:
    """Handoff is stricter than the legacy GET decoder because it persists copied positions.

    Keep status values as nonempty strings: Hermes can introduce a new status without
    making a cursor produced by this plugin unusable for handoff.
    """
    if any(state[key] < 0 for key in ("k", "d")):
        return False
    if "a" in state and state["a"] < 0:
        return False
    for profile, part in state["c"].items():
        if not isinstance(profile, str) or not profile:
            return False
        if set(part) != {"t", "o"}:
            return False
        for execution, status in part["o"].items():
            if not isinstance(execution, str) or not execution:
                return False
            if not isinstance(status, str) or not status:
                return False
    return True


# ---------------------------------------------------------------------------
# E3 — 칸반 tail
# ---------------------------------------------------------------------------


def _payload_of(raw):
    """task_events.payload(JSON 문자열 또는 이미 dict) → dict. 깨졌으면 `{}`."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _status_of(kind, payload):
    """payload 의 status → kind 기본 상태 → None. 이 순서로 "그 사건이 만든 상태" 를 고른다."""
    status = payload.get("status")
    if isinstance(status, str) and status:
        return status
    return _KIND_DEFAULT_STATUS.get(kind)


def _status_from(conn, task_id, before_id):
    """같은 task 의 더 작은 id 사건 가운데 상태를 말해 주는 마지막 것의 결과 상태. 없으면 None.

    상태를 알 수 없는(payload 도 기본표도 없는) 사건은 건너뛴다 — "모른다" 를 `from` 으로 내는 것보다
    그 전 사건의 상태가 낫다.
    """
    for row in conn.execute(SQL_EARLIER_FOR_TASK, (task_id, before_id)).fetchall():
        kind = row["kind"]
        if kind not in _STATUS_BEARING_KINDS:
            continue
        status = _status_of(kind, _payload_of(row["payload"]))
        if status is not None:
            return status
    return None


def _task_snapshot(api, conn, task_id):
    """현재 카드 (status, title, assignee, parent_count). 이미 지워졌으면 전부 None / 0."""
    task = api.get_task(conn, task_id)
    if task is None:
        return None, None, None, 0
    return (
        getattr(task, "status", None),
        getattr(task, "title", None),
        getattr(task, "assignee", None),
        len(api.parent_ids(conn, task_id) or ()),
    )


def _base_event(slug, row, kind, payload, *, sub=0):
    event_id = f"k:{row['id']}" if sub == 0 else f"k:{row['id']}:status"
    return {
        "id": event_id,
        "ts": row["created_at"],
        "kind": kind,
        "board": slug,
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "payload": payload,
        "_src": "k",
        "_pos": (int(row["id"]), sub),
    }


def _status_payload(api, conn, row, kind, payload):
    """`task.status.payload = {from, to, parent_count, title, assignee}` (E3)."""
    status, title, assignee, parent_count = _task_snapshot(api, conn, row["task_id"])
    to = _status_of(kind, payload) or status
    return {
        "from": _status_from(conn, row["task_id"], row["id"]),
        "to": to,
        "parent_count": parent_count,
        "title": title if title is not None else payload.get("title"),
        "assignee": assignee if assignee is not None else payload.get("assignee"),
    }


def map_kanban_row(api, conn, slug, row) -> list:
    """task_events 한 행 → 계약 사건 0~2개. 무시 목록·모르는 kind 는 빈 목록."""
    kind = row["kind"]
    payload = _payload_of(row["payload"])
    if kind in IGNORED_KINDS:
        return []
    if kind == "created":
        return [_base_event(slug, row, "task.created", payload)]
    if kind == "commented":
        return [_base_event(slug, row, "task.comment", payload)]
    if kind in ("linked", "unlinked"):
        return [_base_event(slug, row, "task.link", {**payload, "action": kind})]
    if kind == "spawned":
        return [_base_event(slug, row, "task.run.started", payload)]
    if kind in STATUS_KINDS:
        return [_base_event(slug, row, "task.status", _status_payload(api, conn, row, kind, payload))]
    if kind in UPDATED_KINDS:
        fields = payload.get("fields") if kind == "edited" else _UPDATED_FIELDS[kind]
        if not isinstance(fields, list):
            fields = []
        return [_base_event(slug, row, "task.updated", {**payload, "fields": list(fields)})]
    if kind in RUN_FINISHED_KINDS:
        events = [_base_event(slug, row, "task.run.finished", {**payload, "outcome": kind})]
        status_payload = _status_payload(api, conn, row, kind, payload)
        # 상태가 바뀌었을 때만 task.status 를 한 건 더 낸다. 카드가 이미 지워졌으면(`to` 없음) 내지 않는다.
        if status_payload["to"] is not None and status_payload["from"] != status_payload["to"]:
            events.append(_base_event(slug, row, "task.status", status_payload, sub=1))
        return events
    return []


def tail_rows(conn, since_id, limit) -> list:
    """`task_events WHERE id > since_id ORDER BY id LIMIT limit` 의 행들(sqlite Row 또는 dict)."""
    return conn.execute(SQL_TAIL, (int(since_id), int(limit))).fetchall()


def kanban_tail(api, conn, slug, since_id, limit) -> list:
    """tail 행들을 계약 사건으로. 한 행이 0~2개의 사건이 되므로 `limit` 보다 많을 수 있다 — 병합이 자른다."""
    out = []
    for row in tail_rows(conn, since_id, limit):
        out.extend(map_kanban_row(api, conn, slug, row))
    return out


def max_event_id(conn) -> int:
    row = conn.execute(SQL_MAX_ID).fetchone()
    if row is None:
        return 0
    value = row["max_id"] if "max_id" in row.keys() else row[0]
    return int(value or 0)


# ---------------------------------------------------------------------------
# §5.3 — 삭제 기록 (`deskrpg_deleted.jsonl`)
# ---------------------------------------------------------------------------


def deleted_log_path(api, slug) -> Path:
    return Path(deleted_log.log_path(api, slug))


def read_deleted_since(api, slug, after_n) -> list:
    """삭제 기록의 `n > after_n` 줄 — `deleted_log.read_deleted_since` 그대로다(파일 형식·줄 번호 규칙의 정본은 거기).

    예전엔 여기 따로 읽기 코드가 있었는데, 쓰는 쪽(`kanban_board.delete_task_handler` → `deleted_log`)과
    읽는 쪽이 갈라지면 줄 번호 해석이 어긋나 커서가 엉뚱한 곳을 가리킨다. 한 모듈만 파일을 안다.
    """
    return deleted_log.read_deleted_since(api, slug, int(after_n))


def deleted_tail(api, slug, after_n) -> list:
    """삭제 기록 → `task.deleted` 사건(`id="d:<n>"`, payload `{task_id, title}`)."""
    out = []
    for record in read_deleted_since(api, slug, after_n):
        ts = cron_results.to_epoch(record.get("ts"))
        out.append(
            {
                "id": f"d:{record['n']}",
                "ts": ts if ts is not None else 0,
                "kind": "task.deleted",
                "board": slug,
                "task_id": record.get("task_id"),
                "payload": {"task_id": record.get("task_id"), "title": record.get("title")},
                "_src": "d",
                "_pos": (record["n"],),
            }
        )
    return out


def deleted_log_position(api, slug) -> int:
    """"지금" 토큰의 `d` — 마지막 줄의 `n`(없으면 줄 수)."""
    records = read_deleted_since(api, slug, -1)
    return records[-1]["n"] if records else 0


# ---------------------------------------------------------------------------
# E4 — 크론
# ---------------------------------------------------------------------------

_TERMINAL = frozenset({"completed", "failed", "unknown"})
_STARTED = frozenset({"running"}) | _TERMINAL


def _profile_names(api) -> list:
    names = []
    for info in api.list_profiles():
        name = getattr(info, "name", None) or (info if isinstance(info, str) else None)
        if name:
            names.append(str(name))
    return names


def _job_name(api, job_id, cache):
    """잡 이름. 잡이 지워졌거나 이름이 없으면 **잡 id** 로 대신한다 — `job_name` 은 null 이 되지 않는다."""
    if job_id not in cache:
        try:
            job = api.get_job(job_id)
        except Exception:
            job = None
        name = job.get("name") if isinstance(job, dict) else None
        cache[job_id] = name if isinstance(name, str) and name.strip() else job_id
    return cache[job_id]


def _open_sdb(api, home):
    if not (Path(home) / cron_results.STATE_DB_FILENAME).is_file():
        return None
    try:
        return cron_results.open_session_db(api, home)
    except Exception as exc:
        logger.debug("[deskrpg] failed to open state.db home=%s: %s", home, exc)
        return None


def _cron_event(profile, exec_id, phase, ts, job_id, payload, *, claimed_at, pos_index):
    return {
        "id": f"c:{profile}:{exec_id}:{phase}",
        "ts": ts if ts is not None else 0,
        "kind": "cron.run.started" if phase == "started" else "cron.run.finished",
        "profile": profile,
        "job_id": job_id,
        "payload": payload,
        "_src": "c",
        "_profile": profile,
        "_exec": exec_id,
        "_claimed": claimed_at,
        "_pos": (pos_index, 0 if phase == "started" else 1),
    }


def _page_executions(api, t) -> list:
    """장부를 최신순으로 `claimed_at <= t` 까지 페이지해 `claimed_at > t` 인 행을 **오래된 순**으로 돌려준다."""
    collected = []
    before = None
    while True:
        rows = api.list_executions(limit=cron_results.EXECUTIONS_PAGE_LIMIT, before_claimed_at=before)
        if not rows:
            break
        stop = False
        for row in rows:
            claimed = row.get("claimed_at")
            if t is not None and claimed is not None and str(claimed) <= str(t):
                stop = True
                break
            collected.append(row)
        if stop or len(rows) < cron_results.EXECUTIONS_PAGE_LIMIT:
            break
        before = rows[-1].get("claimed_at")
        if before is None:
            break
    collected.reverse()
    return collected


def _ledger_events(api, profile, home, part, tz) -> tuple:
    """장부가 있는 프로필. (사건들, 후보 실행 [(claimed_at, exec_id, status, from_o)] 오래된 순)."""
    old_t = part.get("t")
    old_o = dict(part.get("o") or {})
    seen = {}
    ordered = []
    # 토큰의 미완료 실행을 먼저 재조회한다 — 페이지에 다시 나오면 id 로 겹치지 않게 한다.
    for exec_id in old_o:
        try:
            row = api.get_execution(exec_id)
        except Exception:
            row = None
        if row is None:
            # 장부에서 사라진 실행 — 더 지켜볼 것이 없다. `o` 에서도 빠진다.
            continue
        seen[str(row.get("id"))] = row
        ordered.append((str(row.get("id")), True))
    for row in _page_executions(api, old_t):
        exec_id = str(row.get("id"))
        if exec_id in seen:
            continue
        seen[exec_id] = row
        ordered.append((exec_id, False))

    events = []
    candidates = []
    names = {}
    sdb = _open_sdb(api, home)
    try:
        for index, (exec_id, from_o) in enumerate(ordered):
            row = seen[exec_id]
            status = row.get("status")
            prev = old_o.get(exec_id) if from_o else None
            job_id = str(row.get("job_id")) if row.get("job_id") is not None else None
            claimed_at = row.get("claimed_at")
            started_at = row.get("started_at")
            candidates.append((claimed_at, exec_id, status, from_o))
            # 아직 뜨지 않았거나(claimed), 이미 끝난 것으로 본 표식이면 낼 사건이 없다.
            if status not in _STARTED or prev in _TERMINAL:
                continue
            session_id = None
            if sdb is not None and job_id is not None:
                session_id = cron_results.find_session_for_execution(sdb, job_id, started_at or claimed_at, tz=tz)
            base = {
                "job_id": job_id,
                "job_name": _job_name(api, job_id, names),
                "profile": profile,
                "session_id": session_id,
                "started_at": started_at,
            }
            if prev != "running" and prev not in _TERMINAL:
                events.append(
                    _cron_event(
                        profile, exec_id, "started", cron_results.to_epoch(started_at or claimed_at, tz),
                        job_id, dict(base), claimed_at=claimed_at, pos_index=index,
                    )
                )
            if status in _TERMINAL and prev not in _TERMINAL:
                finished_at = row.get("finished_at")
                result_text = cron_results.result_text_for(
                    api, sdb, job_id, started_at or claimed_at,
                    session_id=session_id, finished_at=finished_at, tz=tz,
                ) if job_id is not None else ""
                events.append(
                    _cron_event(
                        profile, exec_id, "finished",
                        cron_results.to_epoch(finished_at or started_at or claimed_at, tz),
                        job_id,
                        {
                            **base,
                            "status": cron_results.run_status_for(row),
                            "ended_at": finished_at,
                            "result_text": result_text,
                        },
                        claimed_at=claimed_at, pos_index=index,
                    )
                )
    finally:
        if sdb is not None:
            sdb.close()
    return events, candidates


def _record_job_name(job: dict) -> str:
    """잡 레코드 폴백의 `job_name` — 이름이 비어 있으면 id(`_job_name` 과 같은 규칙)."""
    name = job.get("name")
    return name if isinstance(name, str) and name.strip() else str(job.get("id"))


def _fallback_events(api, profile, home, part, tz) -> tuple:
    """장부가 없는 프로필 — 잡 레코드 폴백. `fire_claim` 이 생기면 started 한 번, `last_run_at` 이 `t` 를 넘으면 finished.

    실행 id 는 `<job_id>@<시각>` 으로 만든다 — 같은 잡의 다음 실행과 겹치지 않아야 사건 id 가 유일하다.
    """
    old_t = part.get("t")
    old_o = dict(part.get("o") or {})
    try:
        jobs = api.list_jobs(include_disabled=True)
    except Exception as exc:
        logger.debug("[deskrpg] cron job list failed profile=%s: %s", profile, exc)
        return [], []
    jobs = sorted(jobs, key=lambda j: str(j.get("last_run_at") or ""))
    events = []
    candidates = []
    sdb = _open_sdb(api, home)
    try:
        for index, job in enumerate(jobs):
            job_id = str(job.get("id"))
            claim = job.get("fire_claim")
            claim_at = claim.get("at") if isinstance(claim, dict) else None
            last_run_at = job.get("last_run_at")
            if claim_at:
                exec_id = f"{job_id}@{claim_at}"
                if exec_id not in old_o:
                    session_id = cron_results.find_session_for_execution(sdb, job_id, claim_at, tz=tz) if sdb else None
                    events.append(
                        _cron_event(
                            profile, exec_id, "started", cron_results.to_epoch(claim_at, tz), job_id,
                            {"job_id": job_id, "job_name": _record_job_name(job), "profile": profile,
                             "session_id": session_id, "started_at": claim_at},
                            claimed_at=None, pos_index=index,
                        )
                    )
                candidates.append((None, exec_id, "running", exec_id in old_o))
            if last_run_at and (old_t is None or str(last_run_at) > str(old_t)):
                exec_id = f"{job_id}@{last_run_at}"
                # 이 실행의 started 가 앞서 `o` 에 들어 있었다면 그 항목을 finished 와 함께 닫는다.
                started_key = next((k for k in old_o if k.startswith(job_id + "@")), None)
                started_at = started_key.split("@", 1)[1] if started_key else None
                session_id = cron_results.find_session_for_execution(
                    sdb, job_id, started_at or last_run_at, tz=tz
                ) if sdb else None
                result_text = cron_results.result_text_for(
                    api, sdb, job_id, started_at or last_run_at, session_id=session_id, finished_at=last_run_at, tz=tz
                )
                events.append(
                    _cron_event(
                        profile, exec_id, "finished", cron_results.to_epoch(last_run_at, tz), job_id,
                        {"job_id": job_id, "job_name": _record_job_name(job), "profile": profile,
                         "session_id": session_id, "status": "ok" if job.get("last_status") == "ok" else "error",
                         "started_at": started_at, "ended_at": last_run_at, "result_text": result_text},
                        claimed_at=str(last_run_at), pos_index=index,
                    )
                )
                candidates.append((str(last_run_at), exec_id, "completed", False))
                if started_key:
                    candidates.append((None, started_key, "completed", True))
    finally:
        if sdb is not None:
            sdb.close()
    return events, candidates


def cron_tail(api, cursor_c: dict, tz) -> tuple:
    """모든 프로필의 크론 사건. `(events, info)` — info 는 프로필별 `{"t","o","candidates"}` 로 커서 전진에 쓴다.

    프로필 홈은 `cron.resolve_profile_home` 으로 풀고, 장부는 **경로로만** 존재를 본다(열면 파일이
    생긴다). 한 프로필의 실패는 그 프로필만 건너뛴다 — 다른 프로필의 사건까지 막지 않는다.
    """
    events = []
    info = {}
    for profile in _profile_names(api):
        part = cursor_c.get(profile) or {"t": None, "o": {}}
        try:
            home = _cron.resolve_profile_home(api, profile)
        except RequestError:
            continue
        try:
            with _cron.cron_scope(api, profile):
                if cron_results.executions_db_exists(home):
                    profile_events, candidates = _ledger_events(api, profile, home, part, tz)
                else:
                    profile_events, candidates = _fallback_events(api, profile, home, part, tz)
        except Exception as exc:
            logger.warning("[deskrpg] cron event collection failed profile=%s: %s", profile, exc)
            continue
        events.extend(profile_events)
        info[profile] = {"t": part.get("t"), "o": dict(part.get("o") or {}), "candidates": candidates}
    return events, info


def cron_now_positions(api) -> dict:
    """"지금" 토큰의 `c` — 프로필별 `t`(최신 claimed_at)·`o`(미완료 실행). 장부 없는 프로필은 잡 레코드로."""
    out = {}
    for profile in _profile_names(api):
        try:
            home = _cron.resolve_profile_home(api, profile)
        except RequestError:
            continue
        try:
            with _cron.cron_scope(api, profile):
                if cron_results.executions_db_exists(home):
                    rows = api.list_executions(limit=cron_results.EXECUTIONS_PAGE_LIMIT)
                    t = rows[0].get("claimed_at") if rows else None
                    o = {str(r.get("id")): r.get("status") for r in rows if r.get("status") not in _TERMINAL}
                else:
                    jobs = api.list_jobs(include_disabled=True)
                    stamps = [str(j.get("last_run_at")) for j in jobs if j.get("last_run_at")]
                    t = max(stamps) if stamps else None
                    o = {}
                    for job in jobs:
                        claim = job.get("fire_claim")
                        if isinstance(claim, dict) and claim.get("at"):
                            o[f"{job.get('id')}@{claim['at']}"] = "running"
        except Exception as exc:
            logger.warning("[deskrpg] cron cursor lookup failed profile=%s: %s", profile, exc)
            continue
        out[profile] = {"t": t, "o": o}
    return out


# ---------------------------------------------------------------------------
# 아티팩트 사건 (네 번째 출처 `a`) — 게이트웨이 전역이다(R6). `board` 로 거르지 않는다;
# DeskRPG 가 나중에 채널로 거른다.
# ---------------------------------------------------------------------------

SQL_ARTIFACT_TAIL = "SELECT id, ts, kind, payload FROM artifact_events WHERE id > ? ORDER BY id ASC LIMIT ?"
SQL_ARTIFACT_MAX_ID = "SELECT COALESCE(MAX(id), 0) FROM artifact_events"

# 같은 표(`artifact_events`)에 아티팩트와 카드 제안이 함께 쌓인다. 호출자는 종류를 따로 옵트인하므로
# 읽을 때 SQL 로 거른다 — 거르지 않고 나중에 버리면 커서가 그 자리에 멈춘다.
ARTIFACT_EVENT_KINDS = ("artifact.created", "artifact.versioned", "artifact.deleted",
                        "artifact.capture_failed", "artifact.delete_partial")
CARD_PROPOSAL_EVENT_KINDS = ("card_proposal.created",)
APPROVAL_EVENT_KINDS = ("approval.blocked",)


def _artifact_conn(api):
    """레지스트리가 없으면 None — 사건을 읽으려고 저장소를 만들지 않는다."""
    if not _artifacts.registry_path(api).is_file():
        return None
    return _artifacts.open_registry(api)


def read_artifact_events(api, after_id: int, limit: int, kinds=None) -> list:
    """`kinds` 가 오면 그 종류만 SQL 로 걸러 읽는다 — None 은 표 전체(옛 호출자)."""
    conn = _artifact_conn(api)
    if conn is None:
        return []
    try:
        if kinds:
            kinds = tuple(kinds)
            sql = ("SELECT id, ts, kind, payload FROM artifact_events WHERE id > ?"
                   f" AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id ASC LIMIT ?")
            rows = conn.execute(sql, (int(after_id), *kinds, int(limit))).fetchall()
        else:
            rows = conn.execute(SQL_ARTIFACT_TAIL, (int(after_id), int(limit))).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        payload = json.loads(row["payload"]) if row["payload"] else {}
        event = {
            "id": f"a:{row['id']}", "ts": int(row["ts"]), "kind": row["kind"], "payload": payload,
            "_src": "a", "_pos": (int(row["id"]),),
        }
        # 값이 있는 선택 키만 싣는다 — capture_failed 처럼 아티팩트가 없는 사건에 null 키를 내지 않는다.
        for key in ("artifact_id", "profile", "board", "task_id"):
            if payload.get(key) is not None:
                event[key] = payload[key]
        out.append(event)
    return out


def artifact_position(api, state: dict) -> int:
    """커서의 `a`. 구버전 커서(키 없음)는 지금 max(id) — 과거 사건을 폭포처럼 다시 주지 않는다."""
    if "a" in state:
        return int(state["a"])
    conn = _artifact_conn(api)
    if conn is None:
        return 0
    try:
        return int(conn.execute(SQL_ARTIFACT_MAX_ID).fetchone()[0])
    finally:
        conn.close()


def advance_artifacts(old_a: int, artifact_events, emitted_ids) -> int:
    groups = [(e["_pos"][0], [e["id"]]) for e in sorted(artifact_events, key=lambda e: e["_pos"])]
    last = _fully_emitted_prefix(groups, emitted_ids)
    return last if last is not None else old_a


# ---------------------------------------------------------------------------
# E5·E6 — 병합과 커서 전진
# ---------------------------------------------------------------------------


def _sort_key(event):
    return (event["ts"], _SOURCE_RANK[event["_src"]], tuple(event["_pos"]))


def merge(kanban_events, deleted_events, cron_events, artifact_events, limit) -> tuple:
    """ts 오름차순(같으면 k<d<c<a, 그다음 출처 안의 위치)으로 합쳐 `limit` 개까지.

    `(emitted, has_more, emitted_by_source)` — emitted_by_source 는
    `{"k": [...], "d": [...], "c": [...], "a": [...]}` 로 호출자가 출처별 커서를 **실은 것까지만**
    전진시키는 데 쓴다.
    """
    everything = sorted([*kanban_events, *deleted_events, *cron_events, *artifact_events], key=_sort_key)
    limit = max(1, int(limit))
    emitted = everything[:limit]
    # 한 task_events 행에서 나온 쌍(run.finished + status)은 같은 ts·같은 출처라 항상 이웃이다. 그 사이를
    # 자르면 행이 "다 실리지 않은" 것이 되어 다음 호출에 앞쪽이 또 나온다 — 쌍은 통째로 넘기거나 통째로 싣는다.
    # 쌍이 응답의 **첫** 원소라 앞을 비울 수 없으면(limit=1) 쌍을 통째로 싣는다 — 이때만 응답이 `limit+1` 개다.
    # 쌍 뒤에 사건이 더 있어도 마찬가지다(has_more 로 알린다). 이건 의도한 유일한 예외다.
    if len(everything) > limit:
        last, following = emitted[-1], everything[limit]
        if last["_src"] == "k" == following["_src"] and last["_pos"][0] == following["_pos"][0]:
            emitted = emitted[:-1] if len(emitted) > 1 else everything[: limit + 1]
    has_more = len(everything) > len(emitted)
    by_source = {"k": [], "d": [], "c": [], "a": []}
    for event in emitted:
        by_source[event["_src"]].append(event)
    return emitted, has_more, by_source


def _fully_emitted_prefix(groups, emitted_ids):
    """`groups` = [(key, [event_id...])] 순서대로, 모든 사건이 실린 앞부분의 마지막 key. 없으면 None.

    사건이 하나도 없는 그룹(예: 무시된 kind 만 있는 행, claimed 만 된 실행)은 "본 것" 으로 친다.
    """
    last = None
    for key, ids in groups:
        if all(i in emitted_ids for i in ids):
            last = key
        else:
            break
    return last


def advance_kanban(old_k, read_ids, kanban_events, emitted_ids) -> int:
    """읽은 task_events id 를 오름차순으로 훑어 그 행에서 나온 사건이 **전부** 실린 곳까지 `k` 를 옮긴다."""
    per_row = {}
    for event in kanban_events:
        per_row.setdefault(event["_pos"][0], []).append(event["id"])
    groups = [(row_id, per_row.get(row_id, [])) for row_id in sorted(read_ids)]
    last = _fully_emitted_prefix(groups, emitted_ids)
    return last if last is not None else old_k


def advance_deleted(old_d, deleted_events, emitted_ids) -> int:
    groups = [(event["_pos"][0], [event["id"]]) for event in sorted(deleted_events, key=lambda e: e["_pos"])]
    last = _fully_emitted_prefix(groups, emitted_ids)
    return last if last is not None else old_d


def advance_cron(info: dict, cron_events, emitted_ids) -> dict:
    """프로필별 `{t, o}` 를 실은 사건 기준으로 옮긴다(E6).

    `t` 는 `claimed_at > t` 인 실행(페이지든 `o` 재조회든)을 claimed_at 순으로 훑어 사건이 전부 실린
    앞부분까지만 옮긴다. 같은 claimed_at 묶음은 하나라도 잘렸으면 넘지 않는다.

    `o` 는 **관측자가 본 상태**다:
    - 전부 실렸고 미완료 → 현재 상태. 전부 실렸고 끝났는데 `t` 가 아직 그 claimed_at 을 못 넘었으면
      끝난 상태 그대로 남긴다 — 페이지가 다시 찾아도 "이미 본 것" 이라 재발행하지 않는다. `t` 가 넘으면 빠진다.
    - started 만 실리고 finished 가 잘림 → `running` (다음 호출이 재조회해 finished 만 낸다).
    - 하나도 못 실림 → `o` 에 있던 값 그대로(없던 것은 페이지가 다시 찾는다).
    재조회와 페이지는 id 로 겹치지 않으므로 어느 쪽에 있어도 두 번 나오지 않는다.
    """
    per_exec = {}
    for event in cron_events:
        per_exec.setdefault((event["_profile"], event["_exec"]), []).append(event["id"])
    out = {}
    for profile, part in info.items():
        old_o = part["o"]
        old_t = part["t"]
        new_o = {}
        ahead = []  # (claimed_at, exec_id, complete) — 아직 t 가 넘지 못한 실행
        terminal_ahead = {}
        for claimed_at, exec_id, status, _from_o in part["candidates"]:
            ids = per_exec.get((profile, exec_id), [])
            shown = [i for i in ids if i in emitted_ids]
            complete = len(shown) == len(ids)
            if complete:
                if status not in _TERMINAL:
                    new_o[exec_id] = status
            elif shown:
                new_o[exec_id] = "running"
            elif exec_id in old_o:
                new_o[exec_id] = old_o[exec_id]
            if claimed_at is not None and (old_t is None or str(claimed_at) > str(old_t)):
                ahead.append((str(claimed_at), exec_id, complete))
                if complete and status in _TERMINAL:
                    terminal_ahead[exec_id] = (str(claimed_at), status)
        groups = []
        for claimed_at, exec_id, complete in sorted(ahead):
            if groups and groups[-1][0] == claimed_at:
                groups[-1][1].append(complete)
            else:
                groups.append((claimed_at, [complete]))
        t = old_t
        for claimed_at, flags in groups:
            if all(flags):
                t = claimed_at
            else:
                break
        for exec_id, (claimed_at, status) in terminal_ahead.items():
            if t is None or claimed_at > str(t):
                new_o[exec_id] = status
        out[profile] = {"t": t, "o": new_o}
    return out


def public_event(event: dict) -> dict:
    """내부 키(`_src`·`_pos`·`_profile`·`_exec`·`_claimed`)를 뗀 계약 모양."""
    return {key: value for key, value in event.items() if not key.startswith("_")}


# ---------------------------------------------------------------------------
# 핸들러
# ---------------------------------------------------------------------------


def clamp_limit(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return LIMIT_DEFAULT
    return max(1, min(value, LIMIT_MAX))


def _timezone_of(api):
    try:
        return api.get_timezone()
    except Exception:
        return None


def _include_tokens(raw) -> set:
    if not raw:
        return set()
    return {token.strip() for token in str(raw).split(",")}


def wants_artifacts(raw) -> bool:
    """`include=artifacts` 옵트인(R17). 쉼표 목록을 받고 모르는 토큰은 무시한다."""
    return "artifacts" in _include_tokens(raw)


def wants_card_proposals(raw) -> bool:
    """`include=card_proposals` 옵트인. 아티팩트와 같은 규약이고 서로 독립이다 —
    이걸 켜지 않은 DeskRPG 는 `card_proposal.created` 를 영영 받지 않는다."""
    return "card_proposals" in _include_tokens(raw)


def wants_approvals(raw) -> bool:
    """`include=approvals` 옵트인(0.18.0) — 무인 실행 막힘 사건. 다른 토큰과 독립이다."""
    return "approvals" in _include_tokens(raw)


def artifact_kind_filter(*, include_artifacts: bool, include_card_proposals: bool,
                         include_approvals: bool = False) -> tuple:
    """`a` 출처에서 읽을 종류. 모두 끄면 빈 튜플 — 호출자는 출처를 아예 읽지 않는다."""
    kinds = []
    if include_artifacts:
        kinds.extend(ARTIFACT_EVENT_KINDS)
    if include_card_proposals:
        kinds.extend(CARD_PROPOSAL_EVENT_KINDS)
    if include_approvals:
        kinds.extend(APPROVAL_EVENT_KINDS)
    return tuple(kinds)


def now_state(api, conn, slug, *, include_artifacts=False, include_card_proposals=False,
              include_approvals=False) -> dict:
    """E1 — 커서가 없을 때의 "지금" 위치. `a` 는 옵트인했을 때만 싣는다(어느 종류든)."""
    state = {
        "k": max_event_id(conn),
        "d": deleted_log_position(api, slug),
        "c": cron_now_positions(api),
    }
    if include_artifacts or include_card_proposals or include_approvals:
        state["a"] = artifact_position(api, {})
    return state


def collect(api, slug, state, limit, *, include_artifacts=False, include_card_proposals=False,
            include_approvals=False) -> dict:
    """E3–E6 을 한 번에: 세 출처를 읽고 병합해 `{events, cursor, has_more}` 를 만든다. 워커 스레드 안에서 부른다."""
    tz = _timezone_of(api)
    with board_conn(api, slug) as conn:
        # 한 행을 더 읽는다 — 딱 `limit` 행만 읽으면 그 뒤에 더 있는지 몰라 has_more 를 못 켠다.
        rows = tail_rows(conn, state["k"], limit + 1)
        read_ids = [int(row["id"]) for row in rows]
        kanban_events = []
        for row in rows:
            kanban_events.extend(map_kanban_row(api, conn, slug, row))
    deleted_events = deleted_tail(api, slug, state["d"])
    cron_events, cron_info = cron_tail(api, state.get("c") or {}, tz)
    # 옵트인하지 않은 호출자는 아티팩트 출처를 읽지도 합치지도 않고, 받은 `a` 를 그대로 돌려준다(없으면 없는 채로).
    kinds = artifact_kind_filter(include_artifacts=include_artifacts,
                                 include_card_proposals=include_card_proposals,
                                 include_approvals=include_approvals)
    if kinds:
        old_a = artifact_position(api, state)
        artifact_events = read_artifact_events(api, old_a, limit + 1, kinds)
    else:
        old_a, artifact_events = state.get("a"), []

    emitted, has_more, _by_source = merge(kanban_events, deleted_events, cron_events, artifact_events, limit)
    # 읽기 상한에 닿았으면 그 뒤에 행이 더 있을 수 있다. 과하게 켜는 쪽이 안전하다 — 다음 호출이 비어 있을 뿐이다.
    has_more = has_more or len(rows) > limit or len(artifact_events) > limit
    emitted_ids = {event["id"] for event in emitted}
    next_state = {
        "k": advance_kanban(state["k"], read_ids, kanban_events, emitted_ids),
        "d": advance_deleted(state["d"], deleted_events, emitted_ids),
        "c": {**{p: v for p, v in (state.get("c") or {}).items() if p not in cron_info},
              **advance_cron(cron_info, cron_events, emitted_ids)},
    }
    if old_a is not None:
        next_state["a"] = advance_artifacts(old_a, artifact_events, emitted_ids)
    return {
        "events": [public_event(event) for event in emitted],
        "cursor": encode_cursor(next_state),
        "has_more": has_more,
    }


def events_handler(api):
    """GET /deskrpg/events?board=&cursor=&limit=&include= → `{events, cursor, has_more}` (E1–E7).

    아티팩트 사건은 `include=artifacts`, 카드 제안 사건은 `include=card_proposals`, 무인 실행 막힘은
    `include=approvals` 일 때만 섞인다(R17) — 구버전 DeskRPG 는 모르는 kind 를 받지 않는다. 셋은 서로 독립이다.
    """

    @guarded
    async def handler(request):
        slug = parse_board_slug(request)
        limit = clamp_limit(request.query.get("limit"))
        token = request.query.get("cursor")
        include_artifacts = wants_artifacts(request.query.get("include"))
        include_card_proposals = wants_card_proposals(request.query.get("include"))
        include_approvals = wants_approvals(request.query.get("include"))

        def work():
            if not api.board_exists(slug):
                raise RequestError(404, "board_not_found", slug)
            if token is None or token == "":
                with board_conn(api, slug) as conn:
                    state = now_state(api, conn, slug, include_artifacts=include_artifacts,
                                      include_card_proposals=include_card_proposals,
                                      include_approvals=include_approvals)
                return {"events": [], "cursor": encode_cursor(state), "has_more": False}
            state = decode_cursor(token)
            result = collect(api, slug, state, limit, include_artifacts=include_artifacts,
                             include_card_proposals=include_card_proposals,
                             include_approvals=include_approvals)
            log_event("events.tail", board=slug, count=len(result["events"]), has_more=result["has_more"])
            return result

        return web.json_response(await run_blocking(work))

    return handler


def handoff_handler(api):
    """Transfer global positions without reading or consuming any events."""

    @guarded
    async def handler(request):
        body = await read_json_object(request)
        if set(body) != {"board", "board_cursor", "carrier_cursor"}:
            raise RequestError(400, "invalid_body")
        slug = body["board"]
        if not isinstance(slug, str) or not BOARD_SLUG_RE.fullmatch(slug):
            raise RequestError(400, "invalid_board")
        target_token = body["board_cursor"]
        source_token = body["carrier_cursor"]
        if target_token is not None and not isinstance(target_token, str):
            raise RequestError(400, "invalid_body")
        if not isinstance(source_token, str):
            raise RequestError(400, "invalid_body")

        def work():
            if not api.board_exists(slug):
                raise RequestError(404, "board_not_found")
            try:
                source = decode_cursor(source_token)
                target = decode_cursor(target_token) if target_token is not None else None
            except RequestError as exc:
                raise RequestError(400, "invalid_handoff_cursor") from exc
            if not _valid_handoff_state(source) or (target is not None and not _valid_handoff_state(target)):
                raise RequestError(400, "invalid_handoff_cursor")
            if "a" not in source:
                raise RequestError(409, "carrier_cursor_incomplete")
            if target is None:
                with board_conn(api, slug) as conn:
                    target = {"k": max_event_id(conn), "d": deleted_log_position(api, slug)}
            return {"cursor": encode_cursor({"k": target["k"], "d": target["d"],
                                             "c": source["c"], "a": source["a"]})}

        return web.json_response(await run_blocking(work))

    return handler

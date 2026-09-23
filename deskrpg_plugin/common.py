"""0.6.0 칸반·크론·사건 핸들러가 공유하는 작은 도구들.

핸들러마다 같은 것을 다시 쓰지 않으려고 모아 둔다 — JSON 오류 응답, 스레드 오프로드,
`?board=` 슬러그 검증, 워커 스레드 안에서 여는 보드 DB 연결, JSON 본문 타입 검사,
그리고 **본문을 절대 찍지 않는** 로깅.

로깅 규칙: 길이·개수·id·슬러그만 남긴다. 카드 본문·프롬프트·결과·시크릿은 어떤 레벨에서도
로그에 넣지 않는다 — 게이트웨이 로그는 오래 남고 누가 보는지 모른다.
"""

import asyncio
import contextlib
import functools
import logging
import re

from aiohttp import web

logger = logging.getLogger("deskrpg_plugin")

# Hermes 0.21.1 `hermes_cli.kanban_db._BOARD_SLUG_RE` 와 같은 정규식. 여기가 더 느슨하면
# Hermes 가 거부할 슬러그를 우리가 400 대신 500 으로 흘린다.
BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def json_error(status: int, code: str, detail=None, **extra) -> web.Response:
    """`{"error": code, "detail": ...}` 모양의 JSON 오류 응답.

    DeskRPG 의 fake-plugin-server 가 내는 모양과 같다(`{ error, detail? }`). `detail` 이
    None 이면 키를 아예 넣지 않는다 — 클라이언트가 `detail in body` 로 분기한다.
    """
    body = {"error": code}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return web.json_response(body, status=status)


class RequestError(Exception):
    """핸들러가 `json_error` 로 바꿔 돌려줄 요청 오류. `status`·`code`·`detail` 을 담는다.

    도우미(`parse_board_slug`, `require_str` …)가 이걸 던지고, 핸들러를 감싼 `guarded` 가
    `response()` 로 바꿔 돌려준다 — 핸들러 안에서 따로 받을 필요가 없다.
    """

    def __init__(self, status: int, code: str, detail=None):
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail

    def response(self) -> web.Response:
        return json_error(self.status, self.code, self.detail)


def guarded(fn):
    """aiohttp 핸들러의 마지막 방어선. **모든 핸들러 팩토리가 이것으로 감싼다.**

    `RequestError` 는 그 응답(`json_error`)으로, 그 밖의 어떤 예외도 500
    `{"error":"internal_error","detail":"<예외 타입 이름>"}` 으로 바꾼다. 응답에는 예외 메시지를 싣지
    않는다 — Hermes 의 예외 문자열에는 카드 본문·경로·시크릿이 섞여 들어올 수 있다. 로그도 타입 이름만
    남기고 트레이스는 `logger.exception` 이 붙인다.

    이게 없으면 aiohttp 가 텍스트 500 을 내고 클라이언트는 `{error}` 모양을 못 받는다.
    """

    @functools.wraps(fn)
    async def wrapper(request):
        try:
            return await fn(request)
        except RequestError as exc:
            return exc.response()
        except Exception as exc:  # noqa: BLE001 — 마지막 방어선. 내용은 로그에만, 그것도 타입만.
            logger.exception("[deskrpg] handler exception: %s", type(exc).__name__)
            return json_error(500, "internal_error", type(exc).__name__)

    return wrapper


async def run_blocking(fn, *args):
    """동기 Hermes 호출(sqlite·파일)을 워커 스레드로 보낸다. 이벤트 루프를 막지 않는다."""
    return await asyncio.to_thread(fn, *args)


def parse_board_slug(request) -> str:
    """`?board=<slug>` 를 읽어 검증한다. 없거나 모양이 틀리면 `RequestError(400)`.

    기본 보드로 조용히 떨어뜨리지 않는다 — 어느 보드인지 모호한 채로 카드를 만들면
    다른 보드에 카드가 생기는 사고가 난다.
    """
    slug = request.query.get("board")
    if slug is None or slug == "":
        raise RequestError(400, "board_required", "?board=<slug> is required")
    if not BOARD_SLUG_RE.fullmatch(slug):
        raise RequestError(400, "invalid_board", f"not a valid board slug: {slug!r}")
    return slug


@contextlib.contextmanager
def board_conn(api, slug: str):
    """보드 DB 연결을 열고 닫는 컨텍스트 매니저. **워커 스레드 안에서만** 쓴다.

    `init_db(board=)` 로 스키마 마이그레이션을 매번 돌린 뒤 `connect(board=)` 한다 —
    `connect` 의 첫-연결 자동 init 은 프로세스당 한 번만 도는 캐시라, 다른 프로세스가
    파일을 갈아 끼운 뒤엔 놓친다. sqlite3 연결의 `with` 는 커밋/롤백만 하고 fd 를 닫지
    않으므로 `contextlib.closing` 으로 반드시 닫는다.

    sqlite 호출은 전부 블로킹이다. 이벤트 루프 스레드에서 열면 게이트웨이 전체가 멈추므로
    호출은 이렇게 감싼다:

        def work():
            with board_conn(api, slug) as conn:
                return api.list_tasks(conn)
        rows = await run_blocking(work)
    """
    api.init_db(board=slug)
    with contextlib.closing(api.connect(board=slug)) as conn:
        yield conn


# ---------------------------------------------------------------------------
# JSON 본문 타입 검사 — 값이 있으면 타입을 강제하고, 없으면 `default` 를 돌려준다.
# 틀리면 RequestError(400, "invalid_field") 로 필드 이름을 알려준다.
# ---------------------------------------------------------------------------

_MISSING = object()


def _field(body: dict, key: str, required: bool):
    """값이 있으면 그 값, 없으면(키 부재·null) `_MISSING`. 필수인데 없으면 400."""
    if not isinstance(body, dict):
        raise RequestError(400, "invalid_body", "a JSON object is required")
    value = body.get(key, _MISSING)
    if value is _MISSING or value is None:
        if required:
            raise RequestError(400, "missing_field", key)
        return _MISSING
    return value


def require_str(body: dict, key: str, *, required: bool = True, default=None, allow_empty: bool = False):
    value = _field(body, key, required)
    if value is _MISSING:
        return default
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise RequestError(400, "invalid_field", f"{key} must be a non-empty string")
    return value


def require_int(body: dict, key: str, *, required: bool = True, default=None, minimum=None):
    value = _field(body, key, required)
    if value is _MISSING:
        return default
    # bool 은 int 의 하위 타입이라 따로 걸러야 한다 — `true` 가 1 로 통과하면 안 된다.
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestError(400, "invalid_field", f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise RequestError(400, "invalid_field", f"{key} must be {minimum} or greater")
    return value


def require_bool(body: dict, key: str, *, required: bool = True, default=None):
    value = _field(body, key, required)
    if value is _MISSING:
        return default
    if not isinstance(value, bool):
        raise RequestError(400, "invalid_field", f"{key} must be true or false")
    return value


def require_str_list(body: dict, key: str, *, required: bool = True, default=None):
    value = _field(body, key, required)
    if value is _MISSING:
        return default
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RequestError(400, "invalid_field", f"{key} must be an array of strings")
    return value


async def read_json_object(request) -> dict:
    """본문을 JSON 객체로 읽는다. 파싱 실패·객체가 아니면 RequestError(400)."""
    try:
        body = await request.json()
    except Exception:
        raise RequestError(400, "invalid_json", "body is not JSON")
    if not isinstance(body, dict):
        raise RequestError(400, "invalid_body", "a JSON object is required")
    return body


def log_event(action: str, **fields) -> None:
    """`[deskrpg] action key=value …` 한 줄. **본문·프롬프트·결과·시크릿 금지.**

    문자열 값은 길이로 바꿔 남긴다(`title_len=12`) — 실수로 본문을 넘겨도 내용이 새지 않는다.
    id·슬러그·개수·불리언은 그대로 둔다(`task_id`, `board`, `count`, `*_id`, `*_len` 으로 끝나는 키).
    """
    parts = []
    for key, value in fields.items():
        if isinstance(value, str) and not (key.endswith("_id") or key in ("board", "profile", "status", "kind", "action")):
            parts.append(f"{key}_len={len(value)}")
        elif isinstance(value, (bytes, bytearray)):
            parts.append(f"{key}_bytes={len(value)}")
        elif isinstance(value, (list, tuple, set, dict)):
            parts.append(f"{key}_count={len(value)}")
        else:
            parts.append(f"{key}={value}")
    logger.info("[deskrpg] %s %s", action, " ".join(parts))

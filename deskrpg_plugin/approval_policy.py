"""무인 실행 정책(0.18.0) — 크론·칸반 워커가 위험 명령을 만났을 때의 승인 모드와 허용 목록.

사람이 없는 실행에서 Hermes 는 `approvals.cron_mode`(크론)·`approvals.single_query_mode`(칸반 워커 `-q`)가
`deny` 면 위험 명령을 막고, `command_allowlist`(위험 패턴 키 목록)에 든 패턴은 통과시킨다
(`tools/approval.py` `_unattended_deny`·`_is_permanently_approved`). 이 모듈은 그 세 값만 프로필 config.yaml 에서
읽고 바꾼다. `unattended_mode` 는 DeskRPG 대화 경로(api_server)에 영향을 주므로 다루지 않는다.

- 읽기는 Hermes 와 같은 규칙: approve·off·allow·yes → approve, 그 밖(YAML 불리언 포함) → deny. 기본 deny.
- "항상 허용" 같은 다른 모드는 받지 않는다 — 값은 `approve`/`deny` 뿐이다.
- 크론·칸반은 매번 새 프로세스라 다음 실행부터 반영된다. 게이트웨이 프로세스(대화)는 허용 목록을 프로필별로
  처음 한 번 읽어 두므로, 목록에서 뺀 항목은 게이트웨이를 다시 띄울 때까지 대화에서는 계속 통과될 수 있다.
"""

from __future__ import annotations

import json
import os
import threading

import yaml
from aiohttp import web

from . import config as _config
from . import worker_plugin as _worker_plugin
from .common import RequestError, guarded, read_json_object, run_blocking
from .cron import resolve_profile_home
from .mcp_state import now_iso
from .skills_common import actor_of

_MODES = ("deny", "approve")
_APPROVE_WORDS = frozenset({"approve", "off", "allow", "yes"})  # Hermes `_binary_approval_mode` 와 같다
_DEFAULT_TIMEOUT = 300
_ENTRY_MAX = 200
# 본문 필드 → config `approvals.<키>`
_MODE_FIELDS = {"cronMode": "cron_mode", "singleQueryMode": "single_query_mode"}
_AUDIT = ("plugin-data", "deskrpg", "policy-audit.jsonl")
_LOCK = threading.Lock()


def _load(home) -> dict:
    try:
        return _config._load(home / _config.CONFIG_FILENAME)
    except _config.ConfigUnreadable:
        # 사유를 싣지 않는다 — YAML 오류 문구는 망가진 줄(값)을 인용한다.
        raise RequestError(409, "config_unreadable", "config.yaml cannot be parsed") from None


def _mode(value) -> str:
    return "approve" if str(value).lower().strip() in _APPROVE_WORDS else "deny"


def _allowlist(data: dict) -> list[str]:
    raw = data.get("command_allowlist")
    if isinstance(raw, str):  # 옛 config-set 이 목록을 문자열로 남긴 경우 — Hermes 처럼 되살린다
        try:
            raw = yaml.safe_load(raw)
        except yaml.YAMLError:
            return []
    if raw is None or not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        return []  # Hermes 도 형식이 틀린 목록은 통째로 무시한다
    seen: list[str] = []
    for item in raw:
        if item not in seen:
            seen.append(item)
    return seen


def _timeout(approvals: dict) -> int:
    try:
        value = int(approvals.get("timeout", _DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    return value if value > 0 else _DEFAULT_TIMEOUT


def _propagation(api):
    try:
        return bool(_worker_plugin.propagation_enabled(api))
    except Exception:  # noqa: BLE001 — 판정 실패는 "모른다"(null)
        return None


def _view(api, data: dict) -> dict:
    approvals = data.get("approvals") if isinstance(data.get("approvals"), dict) else {}
    return {
        "cronMode": _mode(approvals.get("cron_mode", "deny")),
        "singleQueryMode": _mode(approvals.get("single_query_mode", "deny")),
        "allowlist": _allowlist(data),
        "timeoutSeconds": _timeout(approvals),
        "workerPropagation": _propagation(api),
    }


def _entry(body: dict) -> str:
    raw = body.get("entry")
    if not isinstance(raw, str):
        raise RequestError(400, "invalid_allowlist_entry", "entry must be a string")
    entry = raw.strip()
    if not entry or len(entry) > _ENTRY_MAX or any(ord(ch) < 32 or ord(ch) == 127 for ch in entry):
        raise RequestError(400, "invalid_allowlist_entry",
                           f"entry must be 1-{_ENTRY_MAX} characters without control characters")
    return entry


def _audit(home, actor, action: str, **fields) -> None:
    path = home.joinpath(*_AUDIT)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": now_iso(), "actor": actor, "action": action, **fields}
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def _update(api, home, mutate) -> dict:
    """config 를 읽어 `mutate(data)` 로 바꾼 뒤 원자 저장(백업 포함)하고 새 뷰를 돌려준다. 워커 스레드 안에서 부른다."""
    path = home / _config.CONFIG_FILENAME
    with _LOCK:
        data = _load(home)
        if mutate(data):
            _config._save(path, data)
        return _view(api, data)


def get_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        return web.json_response(await run_blocking(lambda: _view(api, _load(home))))

    return handler


def put_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        body = await read_json_object(request)
        if not body or set(body) - set(_MODE_FIELDS):
            raise RequestError(400, "invalid_field", "only cronMode and singleQueryMode can be changed")
        for key, value in body.items():
            if value not in _MODES:
                raise RequestError(400, "invalid_field", f"{key} must be deny or approve")
        actor = actor_of(request)

        def mutate(data: dict) -> bool:
            approvals = dict(data.get("approvals") or {}) if isinstance(data.get("approvals"), dict) else {}
            for field, key in _MODE_FIELDS.items():
                if field in body:
                    approvals[key] = body[field]
            data["approvals"] = approvals
            return True

        def work():
            view = _update(api, home, mutate)
            _audit(home, actor, "modes", **body)
            return view

        return web.json_response(await run_blocking(work))

    return handler


def allowlist_add_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        entry = _entry(await read_json_object(request))
        actor = actor_of(request)

        def mutate(data: dict) -> bool:
            current = _allowlist(data)
            if entry in current:
                return False
            data["command_allowlist"] = [*current, entry]
            return True

        def work():
            view = _update(api, home, mutate)
            _audit(home, actor, "allowlist_add", entry=entry)
            return view

        return web.json_response(await run_blocking(work))

    return handler


def allowlist_delete_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        entry = _entry(await read_json_object(request))
        actor = actor_of(request)

        def mutate(data: dict) -> bool:
            current = _allowlist(data)
            if entry not in current:
                return False
            data["command_allowlist"] = [item for item in current if item != entry]
            return True

        def work():
            view = _update(api, home, mutate)
            _audit(home, actor, "allowlist_remove", entry=entry)
            return view

        return web.json_response(await run_blocking(work))

    return handler

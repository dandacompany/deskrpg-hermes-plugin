"""MCP 커넥터 관리(0.17.0) — 이름 검증·revision·확인 결과·감사 기록·응답 뷰.

**MCP 설정 정본은 Hermes 다**(config.yaml·.env·mcp-tokens). 여기에 두는 것은 Hermes 가 보관하지 않는
마지막 연결 확인 결과(`mcp-checks.json`)와 변경 감사 기록(`mcp-audit.jsonl`)뿐이다. 둘 다 비밀값은 담지 않는다.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

from . import envfile
from .common import RequestError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_STATE_DIR = ("plugin-data", "deskrpg")
_CHECKS = "mcp-checks.json"
_AUDIT = "mcp-audit.jsonl"
_LOCK = threading.Lock()
# 같은 프로세스 안의 config.yaml 쓰기를 직렬화한다 — Hermes `_save_mcp_server` 는 스스로 잠그지 않는다.
CONFIG_LOCK = threading.Lock()


def require_name(raw) -> str:
    name = str(raw or "")
    if not NAME_RE.fullmatch(name):
        raise RequestError(400, "invalid_name", "server name must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    return name


def revision(entry: dict) -> str:
    text = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state_dir(home: Path) -> Path:
    path = Path(home).joinpath(*_STATE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_checks(home: Path) -> dict:
    path = Path(home).joinpath(*_STATE_DIR, _CHECKS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_check(home: Path, name: str, check: dict | None) -> None:
    """`check=None` 이면 그 서버의 기록을 지운다. 파일은 0600, 원자 교체."""
    with _LOCK:
        data = read_checks(home)
        if check is None:
            data.pop(name, None)
        else:
            data[name] = check
        folder = _state_dir(home)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".mcp-checks-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, folder / _CHECKS)


def audit(home: Path, actor: str | None, action: str, name: str, **extra) -> None:
    """변경 감사 한 줄. 호출자는 값(비밀·URL 쿼리)을 `extra` 에 넣지 않는다."""
    row = {"at": now_iso(), "actor": actor, "action": action, "name": name, **extra}
    with _LOCK:
        fd = os.open(_state_dir(home) / _AUDIT, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


@contextlib.contextmanager
def profile_scope(api, home: Path):
    """home + 시크릿(.env) 스코프 — `${ENV}` 치환·OAuth·재적재가 이 프로필 값을 읽게 한다."""
    with api._profile_runtime_scope(Path(home)):
        yield


def env_refs(entry: dict) -> list[str]:
    """항목의 `env`·`headers` 값에 든 `${KEY}` 참조 이름(등장 순서, 중복 없이)."""
    found: list[str] = []
    blobs = [*(entry.get("env") or {}).values(), *(entry.get("headers") or {}).values()]
    for blob in blobs:
        for key in ENV_REF_RE.findall(str(blob)):
            if key not in found:
                found.append(key)
    return found


def _endpoint_summary(entry: dict) -> str:
    if entry.get("url"):
        parsed = urlparse(str(entry["url"]))
        return f"{parsed.hostname or ''}{parsed.path or ''}"
    command = os.path.basename(str(entry.get("command") or ""))
    return f"{command} ({len(entry.get('args') or [])} args)"


def _auth_kind(entry: dict) -> str:
    if entry.get("auth") == "oauth":
        return "oauth"
    if "Authorization" in (entry.get("headers") or {}):
        return "bearer"
    return "env" if env_refs(entry) else "none"


def server_view(api, home: Path, name: str, entry: dict, checks: dict, *, kind: str = "custom") -> dict:
    """목록 응답 한 행(spec §3.2). 비밀값·URL 쿼리·명령 인자는 싣지 않는다.

    `_oauth_tokens_present` 는 현재 home 을 본다 — 호출자가 그 프로필 home 스코프 안에서 부른다.
    """
    present = envfile.read_assignments(Path(home) / ".env")
    last = checks.get(name)
    tools = None
    if last and isinstance(last.get("tools"), list):
        tools = {"total": len(last["tools"]), "enabled": sum(1 for t in last["tools"] if t.get("on"))}
    return {
        "name": name,
        "kind": kind,
        "transport": "http" if entry.get("url") else "stdio",
        "endpointSummary": _endpoint_summary(entry),
        "enabled": entry.get("enabled", True) is not False,
        "trust": "full" if entry.get("trust", "full") == "full" else "untrusted",
        "auth": _auth_kind(entry),
        "secrets": [{"key": k, "hasValue": k in present} for k in env_refs(entry)],
        "oauthTokenPresent": bool(api._oauth_tokens_present(name)) if entry.get("auth") == "oauth" else False,
        "tools": tools,
        "lastCheck": {k: last[k] for k in ("at", "ok", "error") if k in last} if last else None,
        "revision": revision(entry),
    }


def server_detail(api, home: Path, name: str, entry: dict, checks: dict, *, kind: str = "custom") -> dict:
    """소유자용 상세 — 명령·인자·작업 폴더, 환경변수·헤더 **이름**. 값은 여전히 싣지 않는다."""
    view = server_view(api, home, name, entry, checks, kind=kind)
    view.update({
        "url": str(entry["url"]).split("?", 1)[0] if entry.get("url") else None,
        "command": entry.get("command"),
        "args": [str(a) for a in (entry.get("args") or [])],
        "cwd": entry.get("cwd"),
        "envKeys": sorted((entry.get("env") or {}).keys()),
        "headerKeys": sorted((entry.get("headers") or {}).keys()),
        "toolFilter": {k: v for k, v in dict(entry.get("tools") or {}).items() if k in ("include", "exclude")},
    })
    return view

"""칸반 워커·크론 실행에서도 이 플러그인이 뜨게 한다 — 프로필 홈마다 링크와 활성화 항목.

Hermes 는 사용자 플러그인을 **로드하는 홈의** `plugins/` 한 곳에서만 찾고(`plugins_discovery.
collect_directory_manifests`), 활성화도 **그 홈의** `config.yaml` `plugins.enabled` 에서 읽는다
(`plugins.py`). 게이트웨이는 루트 홈으로 뜨지만 칸반 워커는 `hermes -p <담당 프로필>` 로
(`kanban_db_dispatch.py`), 크론 실행은 `HERMES_HOME=<프로필 홈>` 으로(`cron/scheduler.py`) 뜬다.
그래서 루트에만 설치·활성화하면 채팅에는 훅과 `artifact_save` 가 있는데 워커에는 둘 다 없다 —
워커가 만든 결과 파일이 결과물에 하나도 안 쌓이고, 오류도 로그도 남지 않는다.

프로필마다 둘이 있어야 한다.

1. `<프로필>/plugins/deskrpg` → 루트 설치로 가는 **심볼릭 링크**. 사본이 아니다 — 루트를 올리면
   모든 워커가 같은 버전을 쓴다. Hermes 의 탐색은 `Path.is_dir()` 로 링크를 따라가고, 보안 스캔은
   설치·갱신 때만 돌므로(로드 때는 없다) 링크된 디렉터리가 걸리지 않는다.
2. `<프로필>/config.yaml` 의 `plugins.enabled` 에 `deskrpg`.

**운영자가 켤 때만 전파한다(0.16.0).** 루트(게이트웨이) `config.yaml` 의
`plugins.entries.deskrpg.worker_propagation: true` 또는 환경변수 `DESKRPG_WORKER_PROPAGATION=1`.
기본은 꺼짐이고 플러그인은 이 값을 **읽기만** 한다 — 스스로 켜는 경로는 없다. 꺼져 있으면 소유자 라우트는
409 이고 프로필 생성은 적용을 건너뛴다. 이미 걸린 링크·활성화 항목은 지우지 않는다(`report` 는 계속 보고).

**자동으로 고치지 않는다.** 게이트웨이 기동 때 몰래 설정을 바꾸면 원인이 숨는다. 상태는 `report` 가
보이고(`/deskrpg/info`), 고치는 것은 명시 호출(`ensure`)뿐이다 — 새 프로필 생성과 소유자 라우트.
운영자가 `plugins.disabled` 에 넣었으면 그 뜻을 존중해 켜지 않는다. 이미 있는 실제 디렉터리(사본)는
지우지 않는다 — 누군가의 설치다.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import yaml

from . import config as _config
from . import envfile

PLUGIN_KEY = "deskrpg"
PROPAGATION_ENV = "DESKRPG_WORKER_PROPAGATION"
PROPAGATION_CONFIG_KEY = "worker_propagation"
DISABLED_ERROR = "worker_propagation_disabled"
_TRUTHY = {"1", "true", "yes", "on"}

DISABLED_DETAIL = (
    "Worker propagation is off. When enabled, this plugin symlinks itself into every profile's "
    "plugins/deskrpg and adds 'deskrpg' to that profile's plugins.enabled, so kanban workers and cron "
    "runs load it too. Enable it on the gateway host with "
    "'hermes config set plugins.entries.deskrpg.worker_propagation true' "
    "(or set DESKRPG_WORKER_PROPAGATION=1) and restart the gateway."
)


class EnsureFailed(Exception):
    """한 프로필을 준비하지 못했다. `reason` 은 값이 없는 고정 코드다."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def plugin_root() -> Path:
    """이 플러그인이 설치된 디렉터리(`plugin.yaml` 이 있는 곳) — 링크의 대상."""
    return Path(__file__).resolve().parent.parent


def _root_home(api) -> Path:
    """게이트웨이 루트 홈. 프로필 홈 스코프 안에서 불려도 루트를 가리킨다(`artifacts_store` 와 같은 규칙)."""
    home = Path(api.get_hermes_home())
    if home.parent.name == "profiles":
        home = home.parent.parent
    return home


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in _TRUTHY


def propagation_enabled(api) -> bool:
    """운영자가 워커 전파를 켰는가. 환경변수 또는 루트 config 의 플러그인 항목. 읽기만 한다.

    루트 config 를 읽을 수 없으면 꺼진 것으로 본다 — 모르면 쓰지 않는 쪽이 안전하다.
    """
    if _truthy(os.environ.get(PROPAGATION_ENV, "")):
        return True
    try:
        data = _config._load(_root_home(api) / _config.CONFIG_FILENAME)
    except (_config.ConfigUnreadable, OSError):
        return False
    node = data
    for key in ("plugins", "entries", PLUGIN_KEY, PROPAGATION_CONFIG_KEY):
        if not isinstance(node, dict):
            return False
        node = node.get(key)
    return _truthy(node)


def _link_path(api, name: str) -> Path:
    return Path(api.get_profile_dir(name)) / "plugins" / PLUGIN_KEY


def _link_state(link: Path) -> str:
    """`linked`(루트를 가리킨다) · `missing`(없거나 끊어진 링크) · `other`(다른 것이 있다)."""
    if not link.exists():
        # 끊어진 링크도 여기로 온다 — `exists()` 는 링크를 따라가 대상이 없으면 False 다.
        return "missing"
    try:
        if link.resolve() == plugin_root():
            return "linked"
    except (OSError, RuntimeError):
        pass
    return "other"


def _plugin_lists(data: dict) -> tuple[list, list]:
    section = data.get("plugins")
    if section is None:
        return [], []
    if not isinstance(section, dict):
        raise EnsureFailed("config_unreadable")
    enabled = section.get("enabled") or []
    disabled = section.get("disabled") or []
    if not isinstance(enabled, list) or not isinstance(disabled, list):
        raise EnsureFailed("config_unreadable")
    return enabled, disabled


def _read_config(api, name: str) -> tuple[Path, dict]:
    path = Path(api.get_profile_dir(name)) / _config.CONFIG_FILENAME
    try:
        return path, _config._load(path)
    except _config.ConfigUnreadable:
        raise EnsureFailed("config_unreadable") from None


def status(api, name: str) -> dict:
    """한 프로필의 상태. 읽기만 한다."""
    link = _link_state(_link_path(api, name))
    try:
        _path, data = _read_config(api, name)
        enabled, disabled = _plugin_lists(data)
    except EnsureFailed:
        enabled, disabled = [], []
    return {
        "profile": name,
        "link": link,
        "enabled": PLUGIN_KEY in enabled,
        "disabled": PLUGIN_KEY in disabled,
    }


def report(api) -> dict:
    """워커에서 이 플러그인이 뜨지 않을 프로필 목록 — `/deskrpg/info` 가 싣는다."""
    missing = []
    for info in api.list_profiles():
        name = getattr(info, "name", str(info))
        st = status(api, name)
        if st["link"] != "linked" or not st["enabled"]:
            missing.append(st)
    return {"missing": missing, "propagation": "enabled" if propagation_enabled(api) else "disabled"}


def _ensure_link(link: Path) -> str:
    state = _link_state(link)
    if state == "linked":
        return "present"
    if state == "other":
        return "other"
    if link.is_symlink():  # 끊어진 링크 — 다시 건다
        link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(plugin_root(), link, target_is_directory=True)
    return "created"


def _ensure_enabled(path: Path, data: dict) -> str:
    enabled, disabled = _plugin_lists(data)
    if PLUGIN_KEY in disabled:
        return "disabled_by_operator"
    if PLUGIN_KEY in enabled:
        return "present"
    section = data.get("plugins")
    if section is None:
        section = {}
        data["plugins"] = section
    section["enabled"] = [*enabled, PLUGIN_KEY]
    if path.is_file():
        # config.py 의 PUT 과 같은 이름 규칙 — 같은 초에 두 번 써도 백업이 서로 덮지 않는다.
        backup = path.with_name(
            f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}"
        )
        envfile.write_text_atomic(backup, path.read_text(encoding="utf-8"))
    envfile.write_text_atomic(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return "added"


def ensure(api, name: str) -> dict:
    """한 프로필을 워커에서도 이 플러그인이 뜨게 만든다. 멱등이다.

    설정을 먼저 읽고(깨졌으면 아무것도 바꾸지 않고 실패) 그다음 링크, 마지막에 활성화를 쓴다.
    """
    if not api.profile_exists(name):
        raise EnsureFailed("not_found")
    path, data = _read_config(api, name)
    _plugin_lists(data)  # 쓰기 전에 모양부터 거른다 — 링크만 만들고 설정에서 실패하지 않게
    link = _ensure_link(_link_path(api, name))
    enabled = _ensure_enabled(path, data)
    return {"profile": name, "link": link, "enabled": enabled}


def ensure_handler(api):
    from aiohttp import web

    from .common import run_blocking

    async def handler(request):
        if not await run_blocking(propagation_enabled, api):
            # 아무것도 쓰지 않는다 — 켜는 것은 운영자 설정뿐이다.
            return web.json_response({"error": DISABLED_ERROR, "detail": DISABLED_DETAIL}, status=409)
        names = None
        if request.can_read_body:
            try:
                payload = await request.json()
            except Exception:
                raise web.HTTPBadRequest(reason="body must be JSON")
            if not isinstance(payload, dict):
                raise web.HTTPBadRequest(reason="body must be a JSON object")
            names = payload.get("profiles")
            if names is not None and (
                not isinstance(names, list) or not all(isinstance(n, str) and n for n in names)
            ):
                raise web.HTTPBadRequest(reason="profiles must be a list of profile names")
        if names is None:
            names = [getattr(p, "name", str(p)) for p in api.list_profiles()]

        def run():
            results = []
            for name in names:
                try:
                    results.append(ensure(api, name))
                except EnsureFailed as exc:
                    results.append({"profile": name, "error": exc.reason})
                except OSError as exc:
                    results.append({"profile": name, "error": type(exc).__name__})
            return results

        return web.json_response({"results": await run_blocking(run)})

    return handler

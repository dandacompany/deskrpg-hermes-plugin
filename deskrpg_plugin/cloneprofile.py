"""기본 프로필에서 새 프로필로 **모델 설정과 모델 키만** 물려준다.

Hermes 의 `create_profile(clone_config=True)` 를 쓰지 않는다 — `.env` 전체(텔레그램·디스코드 봇 토큰,
`API_SERVER_KEY`)와 SOUL.md·메모리까지 복사해서, 새 직원이 같은 봇에 붙거나 같은 인격으로 태어난다.
auth.json(OAuth 로그인)도 복사하지 않는다 — 리프레시 토큰을 공유하면 회전 시 한쪽이 죽는다. OAuth 는
직원마다 로그인한다(`needsLogin` 으로 알린다).

**값을 다루지만 말하지 않는다.** 반환값·로그·예외 사유에는 키 **이름**만 있다. 예외 체인도 끊는다
(`from None`) — YAML 파서 오류는 원문 줄 조각을 메시지에 담으므로, 체인이 남으면 트레이스백에 값이 실릴 수 있다.
"""

from __future__ import annotations

import logging

import yaml

from . import config as _config
from . import envfile

logger = logging.getLogger(__name__)

SOURCE_PROFILE = "default"
ENV_FILENAME = ".env"
CONFIG_KEYS = ("model", "providers", "custom_providers", "fallback_providers", "reasoning_effort", "auxiliary")


class CloneFailed(RuntimeError):
    """복제하지 못했다. 프로필은 이미 존재한다. `reason` 에 값이 없다."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _provider_env_names(api) -> set[str]:
    """모델 프로바이더가 읽는 환경변수 이름 — 이 목록 밖의 키는 복사하지 않는다."""
    names: set[str] = set()
    for cfg in (getattr(api, "PROVIDER_REGISTRY", None) or {}).values():
        names.update(str(n) for n in (getattr(cfg, "api_key_env_vars", None) or ()) if n)
        base = getattr(cfg, "base_url_env_var", "") or ""
        if base:
            names.add(str(base))
    return names


def _needs_login(api, model_block) -> list[str]:
    provider = model_block.get("provider") if isinstance(model_block, dict) else None
    if not isinstance(provider, str) or not provider:
        return []
    cfg = (getattr(api, "PROVIDER_REGISTRY", None) or {}).get(provider)
    if cfg is None or getattr(cfg, "auth_type", "api_key") == "api_key":
        return []
    return [provider]


def clone_from_default(api, target_name: str) -> dict:
    source = api.get_profile_dir(SOURCE_PROFILE)
    target = api.get_profile_dir(target_name)

    try:
        source_cfg = _config._load(source / _config.CONFIG_FILENAME)
        target_cfg = _config._load(target / _config.CONFIG_FILENAME)
    except _config.ConfigUnreadable:
        raise CloneFailed("config.yaml 을 해석할 수 없다") from None

    config_keys = sorted(k for k in CONFIG_KEYS if k in source_cfg and source_cfg[k] not in (None, "", {}, []))
    try:
        if config_keys:
            merged = {**target_cfg, **{k: source_cfg[k] for k in config_keys}}
            path = target / _config.CONFIG_FILENAME
            path.write_text(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8")

        wanted = _provider_env_names(api)
        found = envfile.read_assignments(source / ENV_FILENAME)
        lines = {key: found[key] for key in sorted(found) if key in wanted}
        if lines:
            envfile.upsert_lines(target / ENV_FILENAME, lines)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CloneFailed(f"복제 중 파일 오류: {type(exc).__name__}") from None

    logger.info("[deskrpg] 프로필 복제: %s ← default (config %d개, env %d개)",
                target_name, len(config_keys), len(lines))
    return {"configKeys": config_keys, "envKeys": sorted(lines),
            "needsLogin": _needs_login(api, source_cfg.get("model"))}

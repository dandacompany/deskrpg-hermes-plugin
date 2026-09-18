"""기본 프로필에서 새 프로필로 **모델 설정과 모델 키만** 물려준다.

키는 범위를 고른다(`key_scope`): `referenced`(기본)는 복제한 설정이 가리키는 프로바이더의 키만,
`api_keys` 는 api_key 방식 프로바이더 키 전부(범용·OAuth 토큰 이름은 빼고) + `referenced` 몫이다.

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


# 키 복제 범위 — 사용자가 고른다(2026-09-19 결정). `referenced` 가 기본이다.
KEY_SCOPES = ("referenced", "api_keys")
# `api_keys` 여도 옮기지 않는 이름 — 모델 키가 아니라 계정 전체를 여는 범용·OAuth 토큰이다
# (`GITHUB_TOKEN` 은 copilot 의 키 목록에도 있지만 저장소 권한까지 딸려 간다). 설정이 그 프로바이더를
# **가리키면** `referenced` 몫으로는 옮긴다 — 그래야 복제한 copilot 모델이 실제로 돈다.
_TOKEN_CLASS_NAMES = frozenset({"GITHUB_TOKEN", "GH_TOKEN", "HF_TOKEN", "ANTHROPIC_TOKEN"})


def _env_names_of(pcfg) -> set[str]:
    names = {str(n) for n in (getattr(pcfg, "api_key_env_vars", None) or ()) if n}
    base = getattr(pcfg, "base_url_env_var", "") or ""
    if base:
        names.add(str(base))
    return names


def _is_token_class(name: str) -> bool:
    return name in _TOKEN_CLASS_NAMES or name.endswith("_OAUTH_TOKEN")


def _aliases(api) -> dict:
    fn = getattr(api, "_plugin_aliases", None)
    if fn is None:
        return {}
    try:
        return dict(fn() or {})
    except Exception as exc:  # noqa: BLE001 — 별칭을 못 읽으면 소문자 맞춤만 한다
        logger.warning("[deskrpg] 프로바이더 별칭 읽기 실패: %s", type(exc).__name__)
        return {}


def _provider_id(value, aliases: dict) -> str | None:
    """Hermes `resolve_provider`(hermes_cli/auth.py:1339-1340)처럼 소문자·별칭을 풀어 정식 id 로."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    value = aliases.get(value, value)
    return value if value and value != "auto" else None


def _referenced_providers(cfg: dict, aliases: dict) -> set[str]:
    """복제한 설정이 실제로 부르는 프로바이더 id.

    - `model.provider`
    - `fallback_providers` — dict 하나 또는 dict 목록의 `provider`. 문자열 항목은 Hermes 도 버린다
      (`hermes_cli/fallback_config.py:_iter_fallback_entries`).
    - `auxiliary.<task>.provider` — `"auto"` 는 "주 모델을 따른다" 라 따로 세지 않는다
      (`hermes_cli/config_defaults.py` 의 `_aux`).
    """
    ids: set[str] = set()
    model = cfg.get("model")
    if isinstance(model, dict):
        ids.add(_provider_id(model.get("provider"), aliases))
    fallbacks = cfg.get("fallback_providers")
    for entry in [fallbacks] if isinstance(fallbacks, dict) else fallbacks if isinstance(fallbacks, list) else []:
        if isinstance(entry, dict):
            ids.add(_provider_id(entry.get("provider"), aliases))
    aux = cfg.get("auxiliary")
    if isinstance(aux, dict):
        for task in aux.values():
            if isinstance(task, dict):
                ids.add(_provider_id(task.get("provider"), aliases))
    ids.discard(None)
    return ids


def _wanted_env_names(api, cfg: dict, key_scope: str) -> set[str]:
    """복사할 환경변수 **이름**. 레지스트리에 없는 프로바이더 id 는 건너뛴다."""
    registry = getattr(api, "PROVIDER_REGISTRY", None) or {}
    names: set[str] = set()
    for pid in _referenced_providers(cfg, _aliases(api)):
        if pid in registry:
            names |= _env_names_of(registry[pid])
    if key_scope == "api_keys":
        for pcfg in registry.values():
            if getattr(pcfg, "auth_type", "api_key") == "api_key":
                names |= {n for n in _env_names_of(pcfg) if not _is_token_class(n)}
    return names


def _needs_login(api, model_block) -> list[str]:
    raw = model_block.get("provider") if isinstance(model_block, dict) else None
    provider = _provider_id(raw, _aliases(api))
    if not provider:
        return []
    cfg = (getattr(api, "PROVIDER_REGISTRY", None) or {}).get(provider)
    if cfg is None or getattr(cfg, "auth_type", "api_key") == "api_key":
        return []
    return [provider]


def clone_from_default(api, target_name: str, *, key_scope: str = "referenced") -> dict:
    if key_scope not in KEY_SCOPES:
        raise ValueError(f"key_scope must be one of: {', '.join(KEY_SCOPES)}")
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
            # `providers`·`custom_providers` 에 인라인 api_key 가 있을 수 있다 — 0600·원자적으로 쓴다.
            envfile.write_text_atomic(target / _config.CONFIG_FILENAME,
                                      yaml.safe_dump(merged, allow_unicode=True, sort_keys=False))

        wanted = _wanted_env_names(api, source_cfg, key_scope)
        found = envfile.read_assignments(source / ENV_FILENAME)
        lines = {key: found[key] for key in sorted(found) if key in wanted}
        if lines:
            envfile.upsert_lines(target / ENV_FILENAME, lines)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CloneFailed(f"복제 중 파일 오류: {type(exc).__name__}") from None

    logger.info("[deskrpg] 프로필 복제: %s ← default (config %d개, env %d개, 범위 %s)",
                target_name, len(config_keys), len(lines), key_scope)
    return {"configKeys": config_keys, "envKeys": sorted(lines),
            "needsLogin": _needs_login(api, source_cfg.get("model")), "keyScope": key_scope}

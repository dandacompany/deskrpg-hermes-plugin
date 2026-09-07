"""모델·프로바이더·추론 강도 목록을 화면에 넘긴다.

**목록을 우리가 만들지 않는다.** Hermes 자신의 카탈로그·프로바이더 레지스트리·인증
상태를 그대로 읽어 옮긴다. 그래야 Hermes 가 모델을 추가하거나 models.dev 가 갱신될 때
(20분 TTL) DeskRPG 가 자동으로 따라간다 — 우리가 목록을 복제해 두면 반드시 낡는다.

**프로필 스코프로 둔다 — 지금 갈리지 않더라도.** `_auth_file_path()` 는 `HERMES_HOME` 을
따라 `~/.hermes/profiles/<name>/auth.json` 을 가리키므로 프로필별 인증이 **가능한** 구조다.
다만 실측(2026-09-07, 이 서버)에서는 sophie·mia·oliver 가 **같은 4개**(bedrock, copilot,
openai-codex, opencode-free)를 준다 — 프로필 `auth.json` 의 `providers` 가 전부 비어 있고
`auth.py:467` 의 루트 폴백("read-only fallback")과 credential_pool·환경변수가 채우기
때문이다. 즉 **현재는 사실상 전역이다.**

그래도 전역 라우트로 두지 않는다: 프로필이 자기 자격증명을 갖는 순간 갈리는데, 그때
라우트를 옮기면 이미 그 값을 쓰던 화면이 깨진다. 스코프를 넓히는 것은 쉽고 좁히는 것은
어렵다.

**인증 여부를 숨기지 않고 함께 보낸다.** 인증되지 않은 프로바이더를 목록에서 지우면
사용자는 "왜 내가 쓰는 모델이 없지" 를 알 수 없다. Hermes 의 CLI 피커도 못 쓰는 것을
회색으로 보여주지 지우지 않는다 — 같은 태도를 따른다.
"""

from __future__ import annotations

import logging

from aiohttp import web

logger = logging.getLogger(__name__)

# `hermes_cli/models.py:73` 의 COPILOT_REASONING_EFFORTS_GPT5 와
# `config_defaults.py:1244` 의 서브에이전트 effort 주석을 합친 값.
# Hermes 가 이 목록을 상수로 export 하지 않아 여기 적는다 — 그래서 **출처를 함께**
# 남긴다. 늘어나면 두 곳을 보고 갱신해야 한다.
REASONING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


def _provider_rows(api) -> list[dict]:
    """프로바이더 목록 + 이 프로필의 인증 상태."""
    from hermes_cli.auth import PROVIDER_REGISTRY, get_auth_status

    rows = []
    for pid, cfg in PROVIDER_REGISTRY.items():
        authed = False
        try:
            st = get_auth_status(pid) or {}
            # `configured` 와 `logged_in` 은 프로바이더 종류마다 채워지는 쪽이 다르다
            # (API 키형은 configured, OAuth 형은 logged_in). 둘 중 하나면 쓸 수 있다.
            authed = bool(st.get("configured")) or bool(st.get("logged_in"))
        except Exception:
            # 한 프로바이더의 상태 조회 실패가 목록 전체를 죽이면 안 된다.
            # 모르면 "인증 안 됨" 으로 두되, 그 사실이 화면에 보인다.
            pass
        rows.append(
            {
                "id": pid,
                "name": getattr(cfg, "name", pid) or pid,
                "authenticated": authed,
            }
        )
    rows.sort(key=lambda r: (not r["authenticated"], r["id"]))
    return rows


def _models_for(provider_id: str) -> list[str]:
    """그 프로바이더의 모델 목록 — models.dev + 큐레이션 병합."""
    curated: list[str] = []
    try:
        from hermes_cli.model_catalog import get_catalog

        entry = (get_catalog().get("providers") or {}).get(provider_id) or {}
        raw = entry.get("models") or []
        curated = [m.get("id") if isinstance(m, dict) else str(m) for m in raw]
        curated = [m for m in curated if m]
    except Exception:
        pass

    try:
        from hermes_cli.model_setup_flows_common import _models_dev_merged

        merged = _models_dev_merged(provider_id, curated)
        if merged:
            return merged
    except Exception:
        pass

    # models.dev 가 없거나 실패하면 큐레이션만이라도 준다. 빈 목록은 화면이
    # "직접 입력" 으로 떨어뜨릴 신호이지, 오류가 아니다.
    return curated


def get_handler(api):
    """`GET /p/{profile}/deskrpg/catalog`"""

    async def handler(request):
        providers = _provider_rows(api)

        models: dict[str, list[str]] = {}
        for row in providers:
            # 인증된 프로바이더만 모델을 채운다. 79개 전부를 채우면 응답이 거대해지고
            # models.dev 왕복이 그만큼 늘어난다 — 화면이 실제로 고를 수 있는 것만 준다.
            if row["authenticated"]:
                models[row["id"]] = _models_for(row["id"])

        return web.json_response(
            {
                "providers": providers,
                "models": models,
                "reasoningEfforts": REASONING_EFFORTS,
            }
        )

    return handler
